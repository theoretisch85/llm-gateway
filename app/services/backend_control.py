import json
import re
import subprocess
from pathlib import Path

from app.config import get_settings


RESTART_SCRIPT = Path("/opt/llm-gateway/scripts/restart_mi50_backend.sh")


def _run_local_command(command: list[str], timeout_seconds: int = 30) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"Befehl fehlgeschlagen: {' '.join(command)}")
    return output or "OK"


def _run_remote_command(command: str, timeout_seconds: int = 45) -> str:
    settings = get_settings()
    if not settings:
        raise RuntimeError("Settings nicht geladen.")

    host = getattr(settings, "mi50_ssh_host", None)
    user = getattr(settings, "mi50_ssh_user", None)
    port = getattr(settings, "mi50_ssh_port", 22)
    if not host or not user:
        raise RuntimeError("MI50_SSH_HOST und MI50_SSH_USER sind nicht gesetzt.")

    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(port),
            f"{user}@{host}",
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"Remote-Befehl fehlgeschlagen: {command}")
    return output or "OK"


def restart_mi50_backend(timeout_seconds: int = 120) -> dict[str, str]:
    if not RESTART_SCRIPT.exists():
        raise RuntimeError(f"Restart-Skript fehlt: {RESTART_SCRIPT}")

    completed = subprocess.run(
        [str(RESTART_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or f"MI50-Restart fehlgeschlagen mit Exit-Code {completed.returncode}.")

    return {
        "status": "ok",
        "output": output or "MI50-Backend erfolgreich neu gestartet.",
    }


def gateway_status() -> dict[str, str]:
    return {"status": "ok", "output": _run_local_command(["systemctl", "status", "llm-gateway", "--no-pager"])}


def gateway_logs() -> dict[str, str]:
    return {"status": "ok", "output": _run_local_command(["journalctl", "-u", "llm-gateway", "-n", "80", "--no-pager"])}


def restart_gateway() -> dict[str, str]:
    _run_local_command(["systemctl", "restart", "llm-gateway"], timeout_seconds=60)
    return gateway_status()


def kai_status() -> dict[str, str]:
    settings = get_settings()
    if getattr(settings, "mi50_status_command", None):
        return {"status": "ok", "output": _run_remote_command(settings.mi50_status_command)}
    return {"status": "ok", "output": "Kein MI50_STATUS_COMMAND gesetzt."}


def kai_logs() -> dict[str, str]:
    settings = get_settings()
    command = getattr(settings, "mi50_logs_command", "")
    if not command:
        raise RuntimeError("MI50_LOGS_COMMAND ist nicht gesetzt.")
    return {"status": "ok", "output": _run_remote_command(command)}


def _extract_number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", "."))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _walk_pairs(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key, value
            yield from _walk_pairs(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_pairs(item)


def _find_metric_value(payload, include_terms: list[str], exclude_terms: list[str] | None = None):
    lowered_exclude = [term.lower() for term in (exclude_terms or [])]
    for key, value in _walk_pairs(payload):
        key_lower = str(key).lower()
        if all(term.lower() in key_lower for term in include_terms):
            if any(term in key_lower for term in lowered_exclude):
                continue
            number = _extract_number(value)
            if number is not None:
                return number
    return None


def _extract_percent_from_text(output: str, label: str) -> float | None:
    pattern = rf"{re.escape(label)}\s*[:=]?\s*(\d+(?:\.\d+)?)\s*%"
    match = re.search(pattern, output, flags=re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None

    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) >= 2:
        header = lines[-2].split()
        values = lines[-1].split()
        normalized_header = [item.strip().lower() for item in header]
        normalized_label = label.strip().lower().replace("%", "")
        for index, item in enumerate(normalized_header):
            item_label = item.replace("%", "")
            if item_label == normalized_label and index < len(values):
                return _extract_number(values[index])
    return None


def _first_gpu_payload(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, dict) and ("card" in str(key).lower() or "gpu" in str(key).lower()):
                return value
        for value in payload.values():
            if isinstance(value, dict):
                nested = _first_gpu_payload(value)
                if nested is not None:
                    return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _first_gpu_payload(item)
            if nested is not None:
                return nested
    return payload if isinstance(payload, dict) else None


def _bytes_to_gib(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value / (1024**3), 2)


def kai_telemetry() -> dict[str, object]:
    settings = get_settings()
    command = (
        getattr(settings, "mi50_rocm_smi_command", None)
        or "rocm-smi --showtemp --showpower --showmemuse --json"
    )
    output = _run_remote_command(command, timeout_seconds=30)

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {
            "status": "degraded",
            "message": "rocm-smi lieferte kein parsebares JSON.",
            "raw_output": output,
        }

    gpu_payload = _first_gpu_payload(payload) or payload
    temperature_c = _find_metric_value(gpu_payload, ["temp"], ["junction", "mem"])
    if temperature_c is None:
        temperature_c = _find_metric_value(gpu_payload, ["temperature"], ["junction", "mem"])

    power_w = _find_metric_value(gpu_payload, ["power"], ["cap", "max"])
    vram_used = _find_metric_value(gpu_payload, ["vram", "used"])
    vram_total = _find_metric_value(gpu_payload, ["vram", "total"])
    if vram_total is None:
        vram_total = _find_metric_value(gpu_payload, ["vram", "usable"])
    vram_percent = _find_metric_value(gpu_payload, ["vram", "percent"])
    if vram_percent is None:
        vram_percent = _find_metric_value(gpu_payload, ["vram", "use"])
    if vram_percent is None:
        vram_percent = _find_metric_value(gpu_payload, ["allocated", "vram"])
    if vram_percent is None:
        vram_percent = _find_metric_value(gpu_payload, ["vram"])

    vram_used_gib = _bytes_to_gib(vram_used) if (vram_used or 0) > 4096 else round(vram_used, 2) if vram_used is not None else None
    vram_total_gib = _bytes_to_gib(vram_total) if (vram_total or 0) > 4096 else round(vram_total, 2) if vram_total is not None else None
    if vram_percent is None and vram_used is not None and vram_total not in (None, 0):
        vram_percent = round((vram_used / vram_total) * 100, 1) if vram_total > 4096 else round((vram_used / vram_total) * 100, 1)

    if vram_percent is None:
        vram_percent = _extract_percent_from_text(output, "VRAM%")

    if all(metric is None for metric in [temperature_c, power_w, vram_used_gib, vram_total_gib, vram_percent]):
        return {
            "status": "degraded",
            "message": "rocm-smi JSON erkannt, aber keine bekannten Kennzahlen gefunden.",
            "raw_output": output,
        }

    return {
        "status": "ok",
        "temperature_c": round(temperature_c, 1) if temperature_c is not None else None,
        "power_w": round(power_w, 1) if power_w is not None else None,
        "vram_used_gib": vram_used_gib,
        "vram_total_gib": vram_total_gib,
        "vram_percent": vram_percent,
    }


def run_ops_command(target: str, command_name: str) -> dict[str, str]:
    normalized_target = target.strip().lower()
    normalized_command = command_name.strip().lower()

    if normalized_target == "gateway":
        handlers = {
            "status": gateway_status,
            "logs": gateway_logs,
            "restart": restart_gateway,
            "uptime": lambda: {"status": "ok", "output": _run_local_command(["uptime"])},
            "health": lambda: {"status": "ok", "output": _run_local_command(["curl", "-sS", "http://127.0.0.1:8000/health"])},
        }
    elif normalized_target == "kai":
        handlers = {
            "status": kai_status,
            "logs": kai_logs,
            "restart": restart_mi50_backend,
            "health": lambda: {"status": "ok", "output": _run_remote_command("curl -sS http://127.0.0.1:8080/health")},
            "models": lambda: {"status": "ok", "output": _run_remote_command("curl -sS http://127.0.0.1:8080/v1/models")},
            "telemetry": kai_telemetry,
        }
    else:
        raise RuntimeError("Unbekanntes Ops-Ziel.")

    handler = handlers.get(normalized_command)
    if handler is None:
        raise RuntimeError("Unbekannter Ops-Befehl.")
    return handler()
