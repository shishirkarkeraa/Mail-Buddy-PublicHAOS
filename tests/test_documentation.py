from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_start_guide_covers_every_example_environment_key() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    guide = (ROOT / "START_HERE.txt").read_text(encoding="utf-8")
    environment_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", env_example, flags=re.MULTILINE))

    assert environment_keys
    assert environment_keys <= set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", guide))
    assert "There is no DATABASE_URL because SQLite is always local." in guide


def test_start_guide_keeps_platform_and_oauth_contracts_explicit() -> None:
    guide = (ROOT / "START_HERE.txt").read_text(encoding="utf-8")

    assert "macOS with Docker Desktop" in guide
    assert "Raspberry Pi OS 64-bit" in guide
    assert "docker compose --profile auth run --rm --service-ports auth" in guide
    assert "ssh -L 8765:127.0.0.1:8765" in guide
    assert "https://www.googleapis.com/auth/gmail.modify" in guide
    assert "Application type: Desktop app" in guide
    assert "MAIL_BUDDY_BIND_ADDRESS=127.0.0.1" in guide
    assert "MAIL_BUDDY_BIND_ADDRESS=192.168.1.50" in guide
    assert "network_mode: host" not in guide
    assert "root:10001" not in guide
    assert "mode 0644" in guide  # It must explicitly warn against this.


def test_documented_setup_scripts_exist_and_are_executable() -> None:
    guide = (ROOT / "START_HERE.txt").read_text(encoding="utf-8")
    expected_scripts = {
        "create-secrets.sh",
        "configure-firewall.sh",
        "deployment-preflight.sh",
        "restore-backup.sh",
    }

    for filename in expected_scripts:
        script = ROOT / "scripts" / filename
        assert filename in guide
        assert script.is_file()
        assert script.stat().st_mode & 0o111


def test_readme_points_to_canonical_plain_text_guide() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "[START_HERE.txt](START_HERE.txt)" in readme
