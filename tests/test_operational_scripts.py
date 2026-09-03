from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def fake_tools(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    log_path = tmp_path / "commands.log"

    write_executable(
        binary_dir / "docker",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$MAIL_BUDDY_TEST_COMMAND_LOG"
if [ "$*" = "compose ps -q app" ]; then
  printf '%s\\n' "test-app-container"
fi
exit 0
""",
    )
    write_executable(
        binary_dir / "ip",
        """#!/bin/sh
if [ "$1" = "link" ]; then
  exit "${MAIL_BUDDY_TEST_IP_LINK_STATUS:-0}"
fi
if [ "$1 $2 $3 $4" = "-4 -o addr show" ]; then
  printf '%s\\n' "2: eth0 inet ${MAIL_BUDDY_TEST_BIND_ADDRESS:-192.168.50.10}/24"
  exit 0
fi
exit 1
""",
    )
    write_executable(
        binary_dir / "id",
        """#!/bin/sh
if [ "${1:-}" = "-u" ]; then
  printf '%s\\n' "0"
  exit 0
fi
exit 1
""",
    )
    write_executable(
        binary_dir / "stat",
        """#!/bin/sh
format="$2"
path="$3"
case "$format" in
  %u) printf '%s\\n' "0" ;;
  %g) printf '%s\\n' "0" ;;
  %a)
    case "$path" in
      */secrets) printf '%s\\n' "700" ;;
      *) printf '%s\\n' "${MAIL_BUDDY_TEST_SECRET_MODE:-600}" ;;
    esac
    ;;
  *) exit 1 ;;
esac
""",
    )
    write_executable(
        binary_dir / "iptables",
        """#!/bin/sh
printf '%s\\n' "iptables $*" >> "$MAIL_BUDDY_TEST_COMMAND_LOG"
case "$*" in
  *"-n -L DOCKER-USER"*) exit "${MAIL_BUDDY_TEST_DOCKER_CHAIN_STATUS:-0}" ;;
  *"-C DOCKER-USER"*) exit 1 ;;
esac
exit 0
""",
    )
    write_executable(
        binary_dir / "ip6tables",
        """#!/bin/sh
printf '%s\\n' "ip6tables $*" >> "$MAIL_BUDDY_TEST_COMMAND_LOG"
exit 1
""",
    )
    return binary_dir, log_path


def tool_environment(binary_dir: Path, log_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "MAIL_BUDDY_TEST_COMMAND_LOG": str(log_path),
    }


def make_preflight_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    secrets_dir = project / "secrets"
    secrets_dir.mkdir(parents=True)
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (project / ".env").write_text(
        "\n".join(
            [
                "MAIL_BUDDY_BIND_ADDRESS=192.168.50.10",
                "MAIL_BUDDY_SECRETS_DIR=./secrets",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (secrets_dir / "encryption_key").write_text(
        f"{base64.urlsafe_b64encode(b'x' * 32).decode()}\n",
        encoding="utf-8",
    )
    (secrets_dir / "session_secret").write_text("s" * 64, encoding="utf-8")
    (secrets_dir / "password_hash").write_text(
        "$argon2id$v=19$m=65536,t=3,p=4$test$test",
        encoding="utf-8",
    )
    (secrets_dir / "google_client_secret.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )
    firewall_env = tmp_path / "firewall.env"
    firewall_env.write_text(
        "\n".join(
            [
                "MAIL_BUDDY_LAN_SUBNET=192.168.50.0/24",
                "MAIL_BUDDY_LAN_INTERFACE=eth0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return project, firewall_env


def test_deployment_preflight_accepts_locked_down_pi_configuration(
    tmp_path: Path,
) -> None:
    binary_dir, log_path = fake_tools(tmp_path)
    project, firewall_env = make_preflight_project(tmp_path)
    env = tool_environment(binary_dir, log_path)
    env["MAIL_BUDDY_FIREWALL_ENV_FILE"] = str(firewall_env)

    result = subprocess.run(
        [str(ROOT / "scripts/deployment-preflight.sh"), str(project)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "deployment preflight passed" in result.stdout
    assert "192.168.50.10 on eth0" in result.stdout


@pytest.mark.parametrize("bind_address", ["127.0.0.1", "0.0.0.0", "::"])
def test_deployment_preflight_rejects_non_lan_bind(
    tmp_path: Path,
    bind_address: str,
) -> None:
    binary_dir, log_path = fake_tools(tmp_path)
    project, firewall_env = make_preflight_project(tmp_path)
    (project / ".env").write_text(
        f"MAIL_BUDDY_BIND_ADDRESS={bind_address}\nMAIL_BUDDY_SECRETS_DIR=./secrets\n",
        encoding="utf-8",
    )
    env = tool_environment(binary_dir, log_path)
    env["MAIL_BUDDY_FIREWALL_ENV_FILE"] = str(firewall_env)

    result = subprocess.run(
        [str(ROOT / "scripts/deployment-preflight.sh"), str(project)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "fixed LAN IPv4 address" in result.stderr


def test_deployment_preflight_rejects_permissive_secret_mode(
    tmp_path: Path,
) -> None:
    binary_dir, log_path = fake_tools(tmp_path)
    project, firewall_env = make_preflight_project(tmp_path)
    env = tool_environment(binary_dir, log_path)
    env["MAIL_BUDDY_FIREWALL_ENV_FILE"] = str(firewall_env)
    env["MAIL_BUDDY_TEST_SECRET_MODE"] = "644"

    result = subprocess.run(
        [str(ROOT / "scripts/deployment-preflight.sh"), str(project)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 1
    assert "must have mode 0600" in result.stderr


def test_firewall_builds_fail_closed_policy_and_rejects_host_cidr(
    tmp_path: Path,
) -> None:
    binary_dir, log_path = fake_tools(tmp_path)
    env = tool_environment(binary_dir, log_path)
    script = ROOT / "scripts/configure-firewall.sh"

    result = subprocess.run(
        [str(script), "192.168.50.0/24", "eth0"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    commands = log_path.read_text(encoding="utf-8")
    assert "-A MAIL_BUDDY_GUARD -p tcp --dport 443 -j DROP" in commands
    assert "-s 192.168.50.0/24 -p tcp --dport 443 -j RETURN" in commands
    assert "-D DOCKER-USER -i eth0 -j MAIL_BUDDY_GUARD" in commands

    invalid = subprocess.run(
        [str(script), "192.168.50.10/24", "eth0"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert invalid.returncode == 2
    assert "not a canonical IPv4 CIDR" in invalid.stderr


def test_restore_validates_name_and_uses_safe_operation_order(tmp_path: Path) -> None:
    binary_dir, log_path = fake_tools(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    env = tool_environment(binary_dir, log_path)
    env["MAIL_BUDDY_PROJECT_DIR"] = str(project)
    env["TMPDIR"] = str(tmp_path)
    script = ROOT / "scripts/restore-backup.sh"

    invalid = subprocess.run(
        [str(script), "../../mail_buddy.sqlite3"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert invalid.returncode == 2
    assert "Refusing an invalid backup filename" in invalid.stderr

    result = subprocess.run(
        [str(script), "mail_buddy-20260725T010203Z.sqlite3"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr

    commands = log_path.read_text(encoding="utf-8").splitlines()
    stop_index = commands.index("compose stop app")
    safety_index = next(
        index for index, command in enumerate(commands) if "app mail-buddy backup" in command
    )
    commit_index = next(
        index
        for index, command in enumerate(commands)
        if 'destination="/data/mail_buddy.sqlite3"' in command
    )
    start_index = commands.index("compose up -d app")
    assert stop_index < safety_index < commit_index < start_index


def test_runtime_secret_importer_rejects_malformed_uid(tmp_path: Path) -> None:
    env = {**os.environ, "MAIL_BUDDY_RUNTIME_UID": "10001:10001"}
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/import-runtime-secrets.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert "uid must be numeric" in result.stderr


@pytest.mark.parametrize("script", sorted((ROOT / "scripts").glob("*.sh")))
def test_shell_script_syntax(script: Path) -> None:
    result = subprocess.run(
        ["/bin/sh", "-n", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_systemd_units_use_preflight_and_standalone_backup() -> None:
    app_unit = (ROOT / "deploy/mail-buddy.service").read_text(encoding="utf-8")
    backup_unit = (ROOT / "deploy/mail-buddy-backup.service").read_text(encoding="utf-8")
    create_secrets = (ROOT / "scripts/create-secrets.sh").read_text(encoding="utf-8")

    assert "ExecStartPre=/opt/mail-buddy/scripts/deployment-preflight.sh" in app_unit
    assert "Requires=mail-buddy.service" not in backup_unit
    assert "compose run --rm --no-deps -T app mail-buddy backup" in backup_unit
    assert 'chown 0:0 "$secret_dir"' in create_secrets


def test_training_ssh_wrapper_rejects_command_injection(tmp_path: Path) -> None:
    project = tmp_path / "mail-buddy"
    project.mkdir()
    marker = tmp_path / "must-not-exist"
    env = {
        **os.environ,
        "MAIL_BUDDY_PROJECT_DIR": str(project),
        "SSH_ORIGINAL_COMMAND": f"status; touch {marker}",
    }

    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/training-remote.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 2
    assert "rejected" in result.stderr
    assert not marker.exists()


def test_trainer_contract_has_hourly_launchd_and_fail_closed_gates() -> None:
    plist = (ROOT / "scripts/com.mail-buddy.trainer.plist.template").read_text()
    trainer = (ROOT / "scripts/macos-trainer.sh").read_text()
    remote = (ROOT / "scripts/training-remote.sh").read_text()

    assert "<integer>3600</integer>" in plist
    for contract in (
        "AC Power",
        "MAIL_BUDDY_LLAMA_CPP_COMMIT",
        "MAIL_BUDDY_LLAMA_QUANTIZE_SHA256",
        "grad_accumulation_steps",
        "install-pi",
        "promote",
    ):
        assert contract in trainer or contract in (ROOT / "scripts/training-bundle.py").read_text()
    assert "set -f" in remote
    assert "scp -t /opt/mail-buddy/models" in remote
