from pathlib import Path
import subprocess


RESTART_SCRIPT = Path("/opt/llm-gateway/scripts/restart_mi50_backend.sh")


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
