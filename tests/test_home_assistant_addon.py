from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "mail-buddy"


def test_home_assistant_addon_uses_ingress_and_keeps_oauth_opt_in() -> None:
    config = (ADDON / "config.yaml").read_text(encoding="utf-8")
    run_script = (ADDON / "run.sh").read_text(encoding="utf-8")

    assert "ingress: true" in config
    assert "ingress_port: 8099" in config
    assert "aarch64" in config
    assert "amd64" in config
    assert "8765/tcp: null" in config
    assert "oauth_authorize: false" in config
    assert "mail-buddy auth --bind 0.0.0.0 --redirect-host 127.0.0.1 --port 8765" in run_script
    assert "mail-buddy serve --host 0.0.0.0 --port 8099" in run_script

    repository = (ROOT / "repository.yaml").read_text(encoding="utf-8")
    assert "name: Mail-Buddy Home Assistant Add-ons" in repository


def test_home_assistant_addon_persists_state_and_avoids_host_privileges() -> None:
    dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    run_script = (ADDON / "run.sh").read_text(encoding="utf-8")

    assert "OLLAMA_MODELS=/data/ollama/models" in dockerfile
    assert "MAIL_BUDDY_DATA_DIR=/data" in run_script
    assert "MAIL_BUDDY_BACKUP_DIR=\"$backup_dir\"" in run_script
    assert "docker.sock" not in dockerfile
    assert "--privileged" not in dockerfile


def test_private_repository_packager_creates_self_contained_addon() -> None:
    packager = (ROOT / "scripts" / "package-haos-addon.sh").read_text(encoding="utf-8")
    local_dockerfile = (ADDON / "Dockerfile.local").read_text(encoding="utf-8")

    assert "Refusing to overwrite existing output directory" in packager
    assert 'cp -R "$project_dir/src" "$output_dir/src"' in packager
    assert 'Dockerfile.local" "$output_dir/Dockerfile"' in packager
    assert "COPY src ./src" in local_dockerfile
    assert "git clone" not in local_dockerfile


def test_haos_release_workflow_packages_a_self_contained_public_app() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "publish-haos-addon.yml"
    if not workflow_path.exists():
        dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        assert "git clone" not in dockerfile
        return

    workflow = workflow_path.read_text(encoding="utf-8")

    assert "MAIL_BUDDY_PUBLIC_HAOS_TOKEN" in workflow
    assert "mail-buddy/Dockerfile.local" in workflow
    assert "RELEASE_VERSION: 0.3.${{ github.run_number }}" in workflow
    assert "python -m pytest -q" in workflow
    assert "rsync -a --delete" in workflow
    assert "--exclude='.env'" in workflow
    assert "--exclude='mail-buddy-haos-public'" in workflow
