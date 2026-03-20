import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from app.config import get_settings


RESTART_SCRIPT = Path("/opt/llm-gateway/scripts/restart_mi50_backend.sh")
_LAST_CPU_SAMPLE: tuple[float, float] | None = None


class OpsActionError(RuntimeError):
    """Raised when a controlled gateway or MI50 ops task fails."""


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


def _run_local_shell(command: str, timeout_seconds: int = 120, require_root: bool = False) -> str:
    settings = get_settings()
    clean_command = (command or "").strip()
    if not clean_command:
        raise OpsActionError("Lokaler Shell-Befehl ist leer.")

    # Root-capable gateway tasks still go through a fixed prefix such as
    # "sudo -n" so the web/admin layer never exposes a free local root shell.
    if require_root and os.geteuid() == 0:
        wrapped_command = clean_command
    elif require_root:
        root_prefix = (settings.gateway_local_root_prefix or "").strip()
        if not root_prefix:
            raise OpsActionError("GATEWAY_LOCAL_ROOT_PREFIX ist nicht gesetzt.")
        prefix_binary = shlex.split(root_prefix)[0] if shlex.split(root_prefix) else ""
        if prefix_binary and shutil.which(prefix_binary) is None:
            raise OpsActionError(
                f"Root-Task braucht '{root_prefix}', aber '{prefix_binary}' ist auf diesem Host nicht installiert. "
                "Entweder den Prefix anpassen, sudo/doas installieren oder den Dienst mit passenden Rechten starten."
            )
        wrapped_command = f"{root_prefix} bash -lc {shlex.quote(clean_command)}"
    else:
        wrapped_command = clean_command

    completed = subprocess.run(
        ["bash", "-lc", wrapped_command],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise OpsActionError(output or f"Gateway-Task fehlgeschlagen: {clean_command}")
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


def _prepare_remote_activation_command(command: str, ngl_layers: str = "") -> str:
    clean_command = (command or "").strip()
    clean_ngl = (ngl_layers or "").strip()
    if not clean_ngl:
        return clean_command

    if "{ngl}" in clean_command:
        return clean_command.replace("{ngl}", shlex.quote(clean_ngl))

    return f"KAI_NGL={shlex.quote(clean_ngl)} {clean_command}"


def _normalize_rocm_smi_command(command: str) -> str:
    clean_command = (command or "").strip()
    lowered = clean_command.lower()
    if not lowered.startswith("rocm-smi"):
        return clean_command
    if "--showuse" in lowered or "--show-use" in lowered:
        return clean_command
    return f"{clean_command} --showuse"


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


def switch_mi50_service(service_name: str, known_service_names: list[str], timeout_seconds: int = 180) -> dict[str, str]:
    clean_service = (service_name or "").strip()
    if not clean_service:
        raise RuntimeError("Kein MI50-Service fuer das Profil gesetzt.")

    others = [item for item in known_service_names if item and item != clean_service]
    script_parts: list[str] = []
    if others:
        quoted_others = " ".join(shlex.quote(item) for item in others)
        script_parts.append(f"sudo systemctl stop {quoted_others} >/dev/null 2>&1 || true")
    script_parts.append(f"sudo systemctl restart {shlex.quote(clean_service)}")
    script_parts.append(f"sudo systemctl status {shlex.quote(clean_service)} --no-pager")
    script = "; ".join(script_parts)
    output = _run_remote_command(f"bash -lc {shlex.quote(script)}", timeout_seconds=timeout_seconds)
    return {
        "status": "ok",
        "output": output,
    }


def stop_mi50_service(service_name: str, timeout_seconds: int = 120) -> dict[str, str]:
    clean_service = (service_name or "").strip()
    if not clean_service:
        raise RuntimeError("Kein MI50-Service fuer das Profil gesetzt.")

    script = "; ".join(
        [
            f"systemctl --user stop {shlex.quote(clean_service)} >/dev/null 2>&1 || sudo systemctl stop {shlex.quote(clean_service)} >/dev/null 2>&1 || true",
            f"systemctl --user is-active {shlex.quote(clean_service)} >/dev/null 2>&1 && echo ACTIVE || echo INACTIVE",
        ]
    )
    output = _run_remote_command(f"bash -lc {shlex.quote(script)}", timeout_seconds=timeout_seconds)
    return {
        "status": "ok",
        "output": output,
    }


def run_remote_backend_activation(command: str, ngl_layers: str = "", timeout_seconds: int = 180) -> dict[str, str]:
    clean_command = (command or "").strip()
    if not clean_command:
        raise RuntimeError("Kein Aktivierungsbefehl fuer das Backend-Profil gesetzt.")
    prepared_command = _prepare_remote_activation_command(clean_command, ngl_layers=ngl_layers)
    output = _run_remote_command(prepared_command, timeout_seconds=timeout_seconds)
    return {
        "status": "ok",
        "output": output,
    }


def gateway_status() -> dict[str, str]:
    return {"status": "ok", "output": _run_local_command(["systemctl", "status", "llm-gateway", "--no-pager"])}


def gateway_logs() -> dict[str, str]:
    return {"status": "ok", "output": _run_local_command(["journalctl", "-u", "llm-gateway", "-n", "80", "--no-pager"])}


def restart_gateway() -> dict[str, str]:
    _run_local_command(["systemctl", "restart", "llm-gateway"], timeout_seconds=60)
    return gateway_status()


def gateway_tools() -> dict[str, str]:
    command = dedent_command(
        """
        for tool in bash python3 git curl gh rg htop tmux; do
          if command -v "$tool" >/dev/null 2>&1; then
            printf "%-10s %s\n" "$tool" "$("$tool" --version 2>/dev/null | head -n 1 || echo vorhanden)"
          else
            printf "%-10s %s\n" "$tool" "nicht installiert"
          fi
        done
        """
    )
    return {"status": "ok", "output": _run_local_shell(command)}


def gateway_skills() -> dict[str, str]:
    command = dedent_command(
        """
        if [ -d /root/.codex/skills ]; then
          find /root/.codex/skills -mindepth 1 -maxdepth 3 -name SKILL.md -printf '%h\n' | sort -u
        else
          echo "Kein /root/.codex/skills Verzeichnis gefunden."
        fi
        """
    )
    return {"status": "ok", "output": _run_local_shell(command, require_root=True)}


def gateway_apt_update() -> dict[str, str]:
    return {
        "status": "ok",
        "output": _run_local_shell("DEBIAN_FRONTEND=noninteractive apt-get update", require_root=True, timeout_seconds=300),
    }


def gateway_install_package(package_name: str) -> dict[str, str]:
    clean_package = (package_name or "").strip()
    if not clean_package:
        raise OpsActionError("Kein Paketname fuer die Installation gesetzt.")
    command = f"DEBIAN_FRONTEND=noninteractive apt-get install -y {shlex.quote(clean_package)}"
    return {"status": "ok", "output": _run_local_shell(command, require_root=True, timeout_seconds=600)}


def dedent_command(command: str) -> str:
    return "\n".join(line.strip() for line in command.strip().splitlines() if line.strip())


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


def _read_cpu_times() -> tuple[float, float] | None:
    try:
        first_line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None

    parts = first_line.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None

    values = [float(item) for item in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    total = sum(values)
    return total, idle


def gateway_cpu_usage_percent() -> float | None:
    global _LAST_CPU_SAMPLE

    current = _read_cpu_times()
    if current is None:
        return None

    previous = _LAST_CPU_SAMPLE
    _LAST_CPU_SAMPLE = current
    if previous is None:
        return None

    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None

    usage = 100.0 * (1.0 - (idle_delta / total_delta))
    return round(max(0.0, min(usage, 100.0)), 1)


def gateway_cpu_temp_c() -> float | None:
    thermal_candidates = sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    for temp_file in thermal_candidates:
        try:
            raw_value = temp_file.read_text(encoding="utf-8").strip()
            temperature = float(raw_value)
        except (OSError, ValueError):
            continue

        if temperature > 1000:
            temperature /= 1000.0
        if 0.0 < temperature < 150.0:
            return round(temperature, 1)

    hwmon_candidates = sorted(Path("/sys/class/hwmon").glob("hwmon*/temp*_input"))
    for temp_file in hwmon_candidates:
        try:
            raw_value = temp_file.read_text(encoding="utf-8").strip()
            temperature = float(raw_value)
        except (OSError, ValueError):
            continue

        if temperature > 1000:
            temperature /= 1000.0
        if 0.0 < temperature < 150.0:
            return round(temperature, 1)
    return None


def gateway_system_telemetry() -> dict[str, object]:
    # Compact header telemetry combines local gateway signals and MI50
    # telemetry so the admin UI can refresh the top status line in one request.
    telemetry = {
        "status": "ok",
        "cpu_usage_percent": gateway_cpu_usage_percent(),
        "cpu_temp_c": gateway_cpu_temp_c(),
        "process_loadavg_1m": None,
        "gpu_status": "n/a",
        "gpu_usage_percent": None,
        "temperature_c": None,
        "power_w": None,
        "vram_used_gib": None,
        "vram_total_gib": None,
        "vram_percent": None,
    }

    try:
        load1, _load5, _load15 = os.getloadavg()
        telemetry["process_loadavg_1m"] = round(load1, 2)
    except OSError:
        telemetry["process_loadavg_1m"] = None

    try:
        gpu = kai_telemetry()
        telemetry.update(gpu)
        telemetry["gpu_status"] = str(gpu.get("status") or "ok")
    except RuntimeError as exc:
        telemetry["gpu_status"] = "error"
        telemetry["gpu_message"] = str(exc)

    return telemetry


def kai_telemetry() -> dict[str, object]:
    settings = get_settings()
    command = _normalize_rocm_smi_command(
        (
        getattr(settings, "mi50_rocm_smi_command", None)
        or "rocm-smi --showtemp --showpower --showmemuse --json"
        )
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

    gpu_usage_percent = _find_metric_value(gpu_payload, ["gpu", "use"], ["vram"])
    if gpu_usage_percent is None:
        gpu_usage_percent = _find_metric_value(gpu_payload, ["gpu", "percent"], ["vram"])

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
    if gpu_usage_percent is None:
        gpu_usage_percent = _extract_percent_from_text(output, "GPU%")

    if all(metric is None for metric in [temperature_c, gpu_usage_percent, power_w, vram_used_gib, vram_total_gib, vram_percent]):
        return {
            "status": "degraded",
            "message": "rocm-smi JSON erkannt, aber keine bekannten Kennzahlen gefunden.",
            "raw_output": output,
        }

    return {
        "status": "ok",
        "gpu_usage_percent": round(gpu_usage_percent, 1) if gpu_usage_percent is not None else None,
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
            "tools": gateway_tools,
            "skills": gateway_skills,
            "apt_update": gateway_apt_update,
            "install_git": lambda: gateway_install_package("git"),
            "install_curl": lambda: gateway_install_package("curl"),
            "install_gh": lambda: gateway_install_package("gh"),
            "install_ripgrep": lambda: gateway_install_package("ripgrep"),
            "install_htop": lambda: gateway_install_package("htop"),
            "install_tmux": lambda: gateway_install_package("tmux"),
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
