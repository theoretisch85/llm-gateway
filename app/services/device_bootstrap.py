from __future__ import annotations

import json
import shutil
import shlex
import subprocess
from pathlib import Path
from textwrap import dedent

DEVICE_IDENTITY_FILE = Path("/opt/llm-gateway/.ssh/id_gateway")


def build_device_bootstrap_script(profile: dict[str, object]) -> str:
    # The bootstrap stays intentionally small: prepare Python, .env and a
    # smoke-test client on the Pi so the later voice/avatar stack has a clean base.
    gateway_base_url = str(profile.get("gateway_base_url") or "").strip().rstrip("/")
    device_token = str(profile.get("device_token") or "").strip()
    remote_dir = str(profile.get("remote_dir") or "~/kai-pi").strip() or "~/kai-pi"
    ssh_root_prefix = str(profile.get("ssh_root_prefix") or "sudo -n").strip() or "sudo -n"

    env_text = (
        f"GATEWAY_BASE_URL={gateway_base_url}\n"
        f"DEVICE_TOKEN={device_token}\n"
        "DEFAULT_MODE=auto\n"
        "TTS_ENGINE=espeak-ng\n"
        "TTS_VOICE=de\n"
    )

    requirements_text = "requests>=2.32,<3\npython-dotenv>=1.0,<2\n"

    client_script = dedent(
        """\
        import json
        import os
        import sys
        from pathlib import Path

        import requests
        from dotenv import load_dotenv


        load_dotenv(Path(__file__).resolve().parent / ".env")


        def main() -> int:
            if len(sys.argv) < 2:
                print("Usage: python3 pi_gateway_client.py <message>")
                return 1

            base_url = os.getenv("GATEWAY_BASE_URL", "").rstrip("/")
            token = os.getenv("DEVICE_TOKEN", "").strip()
            mode = os.getenv("DEFAULT_MODE", "auto").strip() or "auto"
            message = " ".join(sys.argv[1:]).strip()
            if not base_url or not token or not message:
                print("GATEWAY_BASE_URL, DEVICE_TOKEN und message muessen gesetzt sein.")
                return 1

            response = requests.post(
                f"{base_url}/api/device/ask",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"message": message, "mode": mode},
                timeout=90,
            )
            response.raise_for_status()
            payload = response.json()
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )

    root_helper = dedent(
        f"""\
        ROOT_PREFIX={shlex.quote(ssh_root_prefix)}
        run_root() {{
          if [ "$(id -u)" -eq 0 ]; then
            "$@"
          else
            $ROOT_PREFIX "$@"
          fi
        }}
        """
    ).strip()

    if remote_dir.startswith("~/"):
        app_dir_assignment = f'APP_DIR="$HOME/{remote_dir[2:]}"'
    else:
        app_dir_assignment = f"APP_DIR={shlex.quote(remote_dir)}"

    script = dedent(
        f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        {app_dir_assignment}
        {root_helper}

        run_root apt-get update
        run_root apt-get install -y python3 python3-venv python3-pip curl git espeak-ng

        mkdir -p "$APP_DIR"
        cd "$APP_DIR"

        python3 - <<'PY'
        from pathlib import Path

        app_dir = Path(".")
        app_dir.joinpath(".env").write_text({json.dumps(env_text)}, encoding="utf-8")
        app_dir.joinpath("requirements.txt").write_text({json.dumps(requirements_text)}, encoding="utf-8")
        app_dir.joinpath("pi_gateway_client.py").write_text({json.dumps(client_script)}, encoding="utf-8")
        app_dir.joinpath("README.md").write_text(
            "Kai Pi Bootstrap\\n\\n"
            "Start:\\n"
            "  source .venv/bin/activate\\n"
            "  python3 pi_gateway_client.py \\"Hallo Kai\\"\\n",
            encoding="utf-8",
        )
        PY

        python3 -m venv .venv
        . .venv/bin/activate
        pip install --upgrade pip
        pip install -r requirements.txt

        echo "Kai Pi Bootstrap fertig in $APP_DIR"
        echo "Test:"
        echo "  cd $APP_DIR && . .venv/bin/activate && python3 pi_gateway_client.py \\"Hallo Kai\\""
        """
    )
    return script


def build_device_install_script(profile: dict[str, object]) -> str:
    # Public alias with clearer intent for the Admin UI wording ("PI installieren").
    return build_device_bootstrap_script(profile)


def run_device_bootstrap_over_ssh(profile: dict[str, object], timeout_seconds: int = 900) -> dict[str, str]:
    script = build_device_bootstrap_script(profile)
    return _run_remote_script(profile, script=script, timeout_seconds=timeout_seconds)


def run_device_install_over_ssh(profile: dict[str, object], timeout_seconds: int = 900) -> dict[str, str]:
    # Public alias for the same SSH install/bootstrap flow.
    return run_device_bootstrap_over_ssh(profile, timeout_seconds=timeout_seconds)


def run_device_probe_over_ssh(profile: dict[str, object], timeout_seconds: int = 60) -> dict[str, str]:
    probe_script = dedent(
        """\
        set -e
        HOSTNAME_VALUE="$(hostname)"
        ACTIVE_STATE=""
        ENABLED_STATE=""
        SERVICE_SCOPE="missing"
        if systemctl --user is-active kai.service >/dev/null 2>&1; then
          ACTIVE_STATE="$(systemctl --user is-active kai.service 2>/dev/null || true)"
          ENABLED_STATE="$(systemctl --user is-enabled kai.service 2>/dev/null || true)"
          SERVICE_SCOPE="user"
        else
          ACTIVE_STATE="$(systemctl is-active kai.service 2>/dev/null || true)"
          ENABLED_STATE="$(systemctl is-enabled kai.service 2>/dev/null || true)"
          if [ -n "${ACTIVE_STATE:-}" ] || [ -n "${ENABLED_STATE:-}" ]; then
            SERVICE_SCOPE="system"
          fi
        fi
        if [ -f /etc/kai-node.json ]; then
          NODE_INFO="$(cat /etc/kai-node.json)"
        else
          NODE_INFO=""
        fi
        printf 'hostname=%s\n' "$HOSTNAME_VALUE"
        printf 'kai_scope=%s\n' "$SERVICE_SCOPE"
        printf 'kai_active=%s\n' "${ACTIVE_STATE:-missing}"
        printf 'kai_enabled=%s\n' "${ENABLED_STATE:-missing}"
        printf 'kai_node=%s\n' "$NODE_INFO"
        """
    )
    return _run_remote_script(profile, script=probe_script, timeout_seconds=timeout_seconds)


def run_device_env_sync_over_ssh(profile: dict[str, object], timeout_seconds: int = 120) -> dict[str, str]:
    gateway_base_url = str(profile.get("gateway_base_url") or "").strip().rstrip("/")
    device_token = str(profile.get("device_token") or "").strip()
    remote_dir = str(profile.get("remote_dir") or "").strip() or "~/kai-pi"

    if remote_dir.startswith("~/"):
        remote_dir_resolved = f"$HOME/{remote_dir[2:]}"
    else:
        remote_dir_resolved = shlex.quote(remote_dir)

    sync_script = dedent(
        f"""\
        set -e
        APP_DIR=""
        SERVICE_SCOPE="missing"

        USER_WORKDIR="$(systemctl --user show -p WorkingDirectory --value kai.service 2>/dev/null || true)"
        if [ -n "$USER_WORKDIR" ] && [ -d "$USER_WORKDIR" ]; then
          APP_DIR="$USER_WORKDIR"
          SERVICE_SCOPE="user"
        fi

        if [ -z "$APP_DIR" ]; then
          if [ -d /home/pi/kai ]; then
            APP_DIR="/home/pi/kai"
          elif [ -d {remote_dir_resolved} ]; then
            APP_DIR={remote_dir_resolved}
          else
            APP_DIR={remote_dir_resolved}
            mkdir -p "$APP_DIR"
          fi
        fi

        ENV_PATH="$APP_DIR/.env"
        export ENV_PATH
        python3 - <<'PY'
        import os
        from pathlib import Path

        env_path = Path(os.environ["ENV_PATH"])
        updates = {{
            "GATEWAY_BASE_URL": {json.dumps(gateway_base_url)},
            "DEVICE_TOKEN": {json.dumps(device_token)},
        }}

        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        lines = []
        pending = dict(updates)

        for raw_line in content.splitlines():
            if not raw_line.strip() or "=" not in raw_line:
                lines.append(raw_line)
                continue
            key, _ = raw_line.split("=", 1)
            key = key.strip()
            if key in pending:
                lines.append(f"{{key}}={{pending.pop(key)}}")
            else:
                lines.append(raw_line)

        for key, value in pending.items():
            lines.append(f"{{key}}={{value}}")

        env_path.write_text("\\n".join(lines) + "\\n", encoding="utf-8")
        print(f"env_path={{env_path}}")
        PY

        if [ "$SERVICE_SCOPE" = "user" ]; then
          systemctl --user restart kai.service
          ACTIVE_STATE="$(systemctl --user is-active kai.service 2>/dev/null || true)"
          ENABLED_STATE="$(systemctl --user is-enabled kai.service 2>/dev/null || true)"
        else
          ACTIVE_STATE="$(systemctl is-active kai.service 2>/dev/null || true)"
          ENABLED_STATE="$(systemctl is-enabled kai.service 2>/dev/null || true)"
          if [ -n "${{ACTIVE_STATE:-}}" ] || [ -n "${{ENABLED_STATE:-}}" ]; then
            systemctl restart kai.service || true
          fi
        fi

        printf 'app_dir=%s\\n' "$APP_DIR"
        printf 'kai_scope=%s\\n' "$SERVICE_SCOPE"
        printf 'kai_active=%s\\n' "${{ACTIVE_STATE:-missing}}"
        printf 'kai_enabled=%s\\n' "${{ENABLED_STATE:-missing}}"
        """
    )
    return _run_remote_script(profile, script=sync_script, timeout_seconds=timeout_seconds)


def run_device_face_apply_over_ssh(
    profile: dict[str, object],
    *,
    style_name: str,
    state: str,
    face_config: dict[str, object],
    timeout_seconds: int = 120,
) -> dict[str, str]:
    """Apply a face style on the Kai Pi via the local style bridge endpoint."""
    style_name_clean = (style_name or "").strip() or "gateway_style"
    state_clean = (state or "").strip().lower() or "idle"
    payload = {
        "name": style_name_clean,
        "state": state_clean,
        "config": face_config or {},
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    apply_script = dedent(
        f"""\
        set -e

        apply_once() {{
          python3 - <<'PY'
import json
import urllib.error
import urllib.request

payload = json.loads({json.dumps(payload_json)})
data = json.dumps(payload).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/style/apply",
    data=data,
    method="POST",
    headers={{"Content-Type": "text/plain;charset=UTF-8"}},
)
try:
    with urllib.request.urlopen(request, timeout=6) as response:
        body = response.read().decode("utf-8", errors="replace")
        print(body)
except urllib.error.URLError as exc:
    raise SystemExit(f"bridge_error={{exc}}")
PY
        }}

        if ! apply_once; then
          systemctl --user restart kai.service || true
          sleep 1
          apply_once
        fi
        """
    )
    return _run_remote_script(profile, script=apply_script, timeout_seconds=timeout_seconds)


def _run_remote_script(profile: dict[str, object], *, script: str, timeout_seconds: int) -> dict[str, str]:
    host = str(profile.get("ssh_host") or "").strip()
    user = str(profile.get("ssh_user") or "").strip()
    port = str(profile.get("ssh_port") or "22").strip() or "22"
    password = str(profile.get("ssh_password") or "").strip()
    if not host or not user:
        raise RuntimeError("PI_SSH_HOST und PI_SSH_USER muessen fuer den Bootstrap gesetzt sein.")

    command = _ssh_command(host=host, user=user, port=port, password=password)
    completed = subprocess.run(
        command,
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part).strip()
    if completed.returncode != 0:
        raise RuntimeError(output or "Pi-Bootstrap ueber SSH fehlgeschlagen.")
    return {"status": "ok", "output": output or "Pi-Bootstrap erfolgreich ausgefuehrt."}


def _ssh_command(*, host: str, user: str, port: str, password: str) -> list[str]:
    ssh_args = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-p",
        port,
    ]
    if DEVICE_IDENTITY_FILE.exists():
        ssh_args.extend(["-i", str(DEVICE_IDENTITY_FILE)])
    ssh_args.extend(
        [
        f"{user}@{host}",
        "bash -s",
        ]
    )
    if not password:
        return ["ssh", "-o", "BatchMode=yes", *ssh_args[1:]]
    if shutil.which("sshpass") is None:
        raise RuntimeError("sshpass ist nicht installiert. Fuer SSH-Passwort-Login bitte erst sshpass nachinstallieren oder SSH-Key-Auth nutzen.")
    return ["sshpass", "-p", password, *ssh_args]
