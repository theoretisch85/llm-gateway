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
        ["ssh", "-p", str(port), f"{user}@{host}", command],
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
        }
    else:
        raise RuntimeError("Unbekanntes Ops-Ziel.")

    handler = handlers.get(normalized_command)
    if handler is None:
        raise RuntimeError("Unbekannter Ops-Befehl.")
    return handler()
