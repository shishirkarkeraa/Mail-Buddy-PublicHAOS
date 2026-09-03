from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SECRET_FILENAMES = (
    "encryption_key",
    "session_secret",
    "password_hash",
    "google_client_secret",
)


def test_compose_uses_cross_platform_auth_and_secret_isolation() -> None:
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text(encoding="utf-8")
    secret_init = compose.split("\n  secret-init:", 1)[1].split("\n  app:", 1)[0]
    auth = compose.split("\n  auth:", 1)[1].split("\n  ollama:", 1)[0]

    assert "network_mode: host" not in compose
    assert '"127.0.0.1:8765:8765/tcp"' in auth
    assert "--redirect-host\n      - 127.0.0.1" in auth
    assert 'network_mode: "none"' in secret_init
    assert "mail_buddy_runtime_secrets:/runtime-secrets" in secret_init
    assert "CHOWN" in secret_init
    assert "DAC_OVERRIDE" in secret_init
    assert "FOWNER" in secret_init


def test_makefile_uses_portable_secret_generator() -> None:
    makefile = (Path(__file__).parents[1] / "Makefile").read_text(encoding="utf-8")
    secrets_target = makefile.split("\nsecrets:", 1)[1].split("\n\n", 1)[0]

    assert "./scripts/create-secrets.sh" in secrets_target
    assert ".venv" not in secrets_target


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    binary_dir = tmp_path / "fake-bin"
    binary_dir.mkdir()
    log_path = tmp_path / "docker-calls.jsonl"
    fake = binary_dir / "docker"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["FAKE_DOCKER_LOG"]).open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")

mode = os.environ.get("FAKE_DOCKER_MODE", "success")
if args and args[0] == "info":
    raise SystemExit(1 if mode == "info-fail" else 0)
if args and args[0] == "build":
    raise SystemExit(0)
if args and args[0] == "run":
    if mode == "run-fail":
        raise SystemExit(9)
    volume = args[args.index("--volume") + 1]
    suffix = ":/output"
    if not volume.endswith(suffix):
        raise SystemExit("unexpected output volume")
    output = Path(volume[:-len(suffix)])
    filenames = ["encryption_key", "session_secret", "password_hash"]
    if mode == "incomplete":
        filenames.pop()
    for index, filename in enumerate(filenames):
        (output / filename).write_text(f"generated-{index}\\n", encoding="utf-8")
    raise SystemExit(0)
raise SystemExit("unexpected docker command")
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return binary_dir, log_path


def _run_secret_generator(
    script: Path,
    secret_dir: Path,
    fake_binary_dir: Path,
    log_path: Path,
    *,
    mode: str = "success",
) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PATH": f"{fake_binary_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(log_path),
        "FAKE_DOCKER_MODE": mode,
    }
    return subprocess.run(
        ["/bin/sh", str(script), str(secret_dir)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _docker_calls(log_path: Path) -> list[list[str]]:
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_create_secrets_uses_hardened_portable_docker_run(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "create-secrets.sh"
    fake_binary_dir, log_path = _fake_docker(tmp_path)
    secret_dir = tmp_path / "secrets with spaces"

    result = _run_secret_generator(
        script,
        secret_dir,
        fake_binary_dir,
        log_path,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(secret_dir.stat().st_mode) == 0o700
    for filename in ("encryption_key", "session_secret", "password_hash"):
        secret = secret_dir / filename
        assert secret.is_file()
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    calls = _docker_calls(log_path)
    assert calls[0] == ["info"]
    assert calls[1][0:3] == ["build", "--tag", "mail-buddy:0.1.0"]
    run = calls[2]
    assert run[0:3] == ["run", "--rm", "-it"]
    assert ["--network", "none"] == run[run.index("--network") : run.index("--network") + 2]
    assert "--read-only" in run
    assert ["--cap-drop", "ALL"] == run[run.index("--cap-drop") : run.index("--cap-drop") + 2]
    assert ["--security-opt", "no-new-privileges"] == run[
        run.index("--security-opt") : run.index("--security-opt") + 2
    ]
    assert ["--user", f"{os.getuid()}:{os.getgid()}"] == run[
        run.index("--user") : run.index("--user") + 2
    ]
    assert ["--volume", f"{secret_dir}:/output"] == run[
        run.index("--volume") : run.index("--volume") + 2
    ]


def test_create_secrets_refuses_overwrite_before_build(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "create-secrets.sh"
    fake_binary_dir, log_path = _fake_docker(tmp_path)
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    existing = secret_dir / "encryption_key"
    existing.write_text("keep-me", encoding="utf-8")

    result = _run_secret_generator(
        script,
        secret_dir,
        fake_binary_dir,
        log_path,
    )

    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    assert existing.read_text(encoding="utf-8") == "keep-me"
    assert _docker_calls(log_path) == [["info"]]


def test_create_secrets_rejects_incomplete_container_output(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "create-secrets.sh"
    fake_binary_dir, log_path = _fake_docker(tmp_path)
    secret_dir = tmp_path / "secrets"

    result = _run_secret_generator(
        script,
        secret_dir,
        fake_binary_dir,
        log_path,
        mode="incomplete",
    )

    assert result.returncode == 1
    assert "did not create a non-empty regular file" in result.stderr


def _run_importer(script: Path, source: Path, target: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "MAIL_BUDDY_SECRET_SOURCE_DIR": str(source),
        "MAIL_BUDDY_SECRET_TARGET_DIR": str(target),
        "MAIL_BUDDY_RUNTIME_UID": str(os.getuid()),
        "MAIL_BUDDY_RUNTIME_GID": str(os.getgid()),
    }
    return subprocess.run(
        ["/bin/sh", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_runtime_secret_import_is_private_and_repeatable(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "import-runtime-secrets.sh"
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    for index, filename in enumerate(SECRET_FILENAMES):
        (source / filename).write_text(f"secret-{index}", encoding="utf-8")

    first = _run_importer(script, source, target)
    assert first.returncode == 0, first.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    for index, filename in enumerate(SECRET_FILENAMES):
        imported = target / filename
        assert imported.read_text(encoding="utf-8") == f"secret-{index}"
        assert stat.S_IMODE(imported.stat().st_mode) == 0o400

    (source / "session_secret").write_text("rotated", encoding="utf-8")
    second = _run_importer(script, source, target)
    assert second.returncode == 0, second.stderr
    assert (target / "session_secret").read_text(encoding="utf-8") == "rotated"


def test_runtime_secret_import_fails_closed_on_missing_secret(tmp_path: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "import-runtime-secrets.sh"
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    for filename in SECRET_FILENAMES[:-1]:
        (source / filename).write_text("present", encoding="utf-8")

    result = _run_importer(script, source, target)
    assert result.returncode == 1
    assert "missing or empty" in result.stderr
    assert not any((target / filename).exists() for filename in SECRET_FILENAMES)


@pytest.mark.parametrize(
    ("runtime_uid", "runtime_gid"),
    (("not-a-number", "10001"), ("10001", "bad")),
)
def test_runtime_secret_import_rejects_invalid_ownership(
    tmp_path: Path,
    runtime_uid: str,
    runtime_gid: str,
) -> None:
    script = Path(__file__).parents[1] / "scripts" / "import-runtime-secrets.sh"
    environment = {
        **os.environ,
        "MAIL_BUDDY_SECRET_SOURCE_DIR": str(tmp_path),
        "MAIL_BUDDY_SECRET_TARGET_DIR": str(tmp_path / "target"),
        "MAIL_BUDDY_RUNTIME_UID": runtime_uid,
        "MAIL_BUDDY_RUNTIME_GID": runtime_gid,
    }

    result = subprocess.run(
        ["/bin/sh", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 2
    assert "must be numeric" in result.stderr
