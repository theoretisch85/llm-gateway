from __future__ import annotations

import json
import shlex
import subprocess
from textwrap import dedent


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


def run_device_bootstrap_over_ssh(profile: dict[str, object], timeout_seconds: int = 900) -> dict[str, str]:
    host = str(profile.get("ssh_host") or "").strip()
    user = str(profile.get("ssh_user") or "").strip()
    port = str(profile.get("ssh_port") or "22").strip() or "22"
    if not host or not user:
        raise RuntimeError("PI_SSH_HOST und PI_SSH_USER muessen fuer den Bootstrap gesetzt sein.")

    script = build_device_bootstrap_script(profile)
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            port,
            f"{user}@{host}",
            "bash -s",
        ],
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
