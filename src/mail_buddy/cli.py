from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mail_buddy.config import Settings
from mail_buddy.contracts import EmailMetadata, ParsedEmail
from mail_buddy.db import Database
from mail_buddy.fine_tuning import (
    export_bundle,
    promotion_metrics_pass,
    readiness,
    schedule_state,
)
from mail_buddy.security import RedactingLogFilter, SecretBox, hash_password


def _settings_and_database() -> tuple[Settings, Database]:
    settings = Settings()
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    return settings, database


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingLogFilter())


def _read_password(args: argparse.Namespace, *, confirm: bool) -> str:
    if getattr(args, "password_stdin", False):
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Dashboard password (minimum 12 characters): ")
    if confirm and not getattr(args, "password_stdin", False):
        repeated = getpass.getpass("Confirm dashboard password: ")
        if password != repeated:
            raise ValueError("Passwords did not match")
    return password


def _secure_write(path: Path, value: str, *, force: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_TRUNC if force else os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, f"{value}\n".encode())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _generate_secrets(args: argparse.Namespace) -> int:
    target = args.output_dir.resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    password = _read_password(args, confirm=True)
    values = {
        "encryption_key": SecretBox.generate_key(),
        "session_secret": secrets.token_urlsafe(64),
        "password_hash": hash_password(password),
    }
    created: list[Path] = []
    try:
        for filename, value in values.items():
            path = target / filename
            _secure_write(path, value, force=args.force)
            created.append(path)
    except FileExistsError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise RuntimeError(
            f"{exc.filename} already exists; use --force only when rotating all secrets"
        ) from exc
    print(f"Created three mode-0600 secret files in {target}")
    print("Keep them private, back them up offline, and never commit them.")
    return 0


def _hash_password(args: argparse.Namespace) -> int:
    print(hash_password(_read_password(args, confirm=False)))
    return 0


def _backup(_: argparse.Namespace) -> int:
    settings, database = _settings_and_database()
    destination = database.backup(settings.backup_dir, keep=7)
    print(destination)
    return 0


def _training_context() -> tuple[Settings, Database, SecretBox]:
    settings, database = _settings_and_database()
    settings.validate_runtime_secrets()
    encryption_key = settings.resolved_encryption_key
    if not encryption_key:
        raise RuntimeError("Mail-Buddy encryption key is required")
    return settings, database, SecretBox(encryption_key)


def _training_status(_: argparse.Namespace) -> int:
    settings, database, _ = _training_context()
    rows = database.list_training_examples()
    payload = readiness(rows)
    latest_lora = database.get_latest_training_run("lora")
    interval = int(
        database.get_setting("training_interval_days") or settings.training_interval_days
    )
    hour = int(database.get_setting("training_hour_local") or settings.training_hour_local)
    timezone = os.environ.get("TZ", "UTC")
    schedule = schedule_state(
        now=datetime.now(UTC),
        last_finished_at=(
            str(latest_lora["finished_at"])
            if latest_lora and latest_lora.get("finished_at")
            else None
        ),
        interval_days=interval,
        hour_local=hour,
        timezone=timezone,
    )
    payload.update(
        {
            "timezone": schedule["timezone"],
            "enabled": (
                database.get_setting("learning_enabled")
                or str(settings.personalization_enabled).lower()
            )
            == "true",
            "interval_days": interval,
            "hour_local": hour,
            "lora_due": bool(schedule["due"]),
            "next_eligible_at": schedule["next_eligible_at"],
            "active_companion": database.get_active_personalized_model(),
            "active_main": database.get_active_main_model(),
            "companion_run": database.get_latest_training_run("companion"),
            "lora_run": latest_lora,
        }
    )
    for model_key in ("active_companion", "active_main"):
        model = payload[model_key]
        if isinstance(model, dict):
            model.pop("artifact_ciphertext", None)
    print(json.dumps(payload, sort_keys=True))
    return 0


def _training_export(args: argparse.Namespace) -> int:
    settings, database, secret_box = _training_context()
    if args.if_due:
        enabled = (
            database.get_setting("learning_enabled")
            or str(settings.personalization_enabled).lower()
        ) == "true"
        latest = database.get_latest_training_run("lora")
        due = schedule_state(
            now=datetime.now(UTC),
            last_finished_at=(
                str(latest["finished_at"])
                if latest and latest.get("finished_at")
                else None
            ),
            interval_days=int(
                database.get_setting("training_interval_days")
                or settings.training_interval_days
            ),
            hour_local=int(
                database.get_setting("training_hour_local")
                or settings.training_hour_local
            ),
            timezone=os.environ.get("TZ", "UTC"),
        )["due"]
        if not enabled or not due:
            raise RuntimeError("LoRA training is disabled or not yet due")
    bundle = export_bundle(database, secret_box)
    run_id = database.acquire_training_run(
        "lora", dataset_version=int(bundle.manifest["dataset_version"])
    )
    if run_id is None:
        raise RuntimeError("A LoRA training run is already active")
    bundle.manifest["run_id"] = run_id
    payload = json.dumps(bundle.as_dict(), sort_keys=True)
    if str(args.output) == "-":
        print(payload)
        return 0
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_write(output, payload, force=args.force)
    print(output)
    return 0


def _parse_metrics(value: str) -> dict[str, Any]:
    try:
        metrics = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Metrics must be valid JSON") from exc
    if not isinstance(metrics, dict):
        raise ValueError("Metrics must be a JSON object")
    return metrics


def _register_main_model(args: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    if args.metadata:
        try:
            metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Candidate metadata file is invalid") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Candidate metadata must be a JSON object")
        args.name = metadata.get("name")
        args.base_model = metadata.get("base_model")
        args.sha256 = metadata.get("sha256")
        args.dataset_version = metadata.get("dataset_version")
        args.example_count = metadata.get("example_count")
        args.metrics = json.dumps(metadata.get("metrics"))
    if not all(
        value is not None
        for value in (
            args.name,
            args.base_model,
            args.sha256,
            args.dataset_version,
            args.example_count,
            args.metrics,
        )
    ):
        raise ValueError("Candidate registration fields are incomplete")
    if not re.fullmatch(r"mail-buddy-llama:[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}", args.name):
        raise ValueError("Model name must be a versioned Mail-Buddy tag")
    if not re.fullmatch(r"[a-f0-9]{64}", args.sha256):
        raise ValueError("Artifact SHA-256 must be 64 lowercase hexadecimal characters")
    metrics = _parse_metrics(args.metrics)
    active = database.get_active_main_model()
    baseline = None
    if active:
        baseline = dict(active)
        baseline["category_recall"] = json.loads(str(active["category_recall"]))
    elif isinstance(metrics.get("production"), dict):
        baseline = dict(metrics["production"])
    accepted, reason = promotion_metrics_pass(metrics, baseline)
    if not accepted:
        database.add_event("warning", "main_model_rejected", {"reason": reason})
        database.finish_latest_training_run("lora", "rejected", {"reason": reason})
        raise ValueError(f"Candidate failed promotion metrics: {reason}")
    if "accuracy" not in metrics or "macro_f1" not in metrics:
        raise ValueError("Candidate metrics must include accuracy and macro_f1")
    model_id = database.register_main_model(
        name=args.name,
        base_model=args.base_model,
        artifact_sha256=args.sha256,
        dataset_version=args.dataset_version,
        example_count=args.example_count,
        accuracy=float(metrics["accuracy"]),
        macro_f1=float(metrics["macro_f1"]),
        category_recall={
            str(key): float(value)
            for key, value in dict(metrics.get("category_recall") or {}).items()
        },
    )
    print(json.dumps({"id": model_id, "name": args.name, "status": "candidate"}))
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mark_main_model_installed(args: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    rows = [row for row in database.list_main_models(100) if row["name"] == args.name]
    if not rows:
        raise ValueError("Unknown main-model candidate")
    expected = str(rows[0]["artifact_sha256"])
    actual = args.sha256
    if args.artifact:
        actual = _sha256_file(args.artifact.resolve())
    if actual != expected:
        raise ValueError("Installed artifact checksum does not match the candidate")
    database.mark_main_model_installed(args.name, args.host)
    print(json.dumps({"name": args.name, "host": args.host, "sha256": actual}))
    return 0


async def _run_model_canary(settings: Settings, name: str) -> str:
    from mail_buddy.classification import OllamaClient

    client = OllamaClient(
        settings.ollama_url,
        model=settings.ollama_model,
        timeout_seconds=settings.ollama_timeout_seconds,
        connect_timeout_seconds=settings.ollama_connect_timeout_seconds,
        max_input_chars=settings.max_model_input_chars,
    )
    try:
        result = await client.classify(
            ParsedEmail(
                metadata=EmailMetadata(
                    message_id="deployment-canary",
                    thread_id="deployment-canary",
                    sender="sender@example.test",
                    sender_domain="example.test",
                    subject="Weekly project status update",
                    snippet="Harmless deployment canary",
                ),
                body_text="This is a harmless Mail-Buddy deployment canary.",
            ),
            set(),
            model_override=name,
        )
        return result.primary_category.value
    finally:
        await client.aclose()


def _canary_main_model(args: argparse.Namespace) -> int:
    settings, database, _ = _training_context()
    if not any(row["name"] == args.name for row in database.list_main_models(100)):
        raise ValueError("Unknown main-model candidate")
    category = asyncio.run(_run_model_canary(settings, args.name))
    print(json.dumps({"name": args.name, "structured_category": category}))
    return 0


def _promote_main_model(args: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    pruned = database.promote_main_model(args.name)
    database.add_event("info", "main_model_promoted", {"model": args.name})
    database.finish_latest_training_run(
        "lora", "complete", {"model": args.name}
    )
    print(json.dumps({"name": args.name, "pruned_registry_versions": pruned}))
    return 0


def _rollback_main_model(_: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    name = database.rollback_main_model()
    database.add_event("warning", "main_model_rolled_back", {"model": name})
    print(name)
    return 0


def _fail_training(args: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    database.finish_latest_training_run("lora", "failed", {"reason": args.reason})
    database.add_event("error", "lora_training_failed", {"reason": args.reason})
    return 0


def _recover_stale_training(args: argparse.Namespace) -> int:
    _, database, _ = _training_context()
    count = database.recover_stale_training_runs(timedelta(hours=args.older_than_hours))
    if count:
        database.add_event("warning", "stale_training_recovered", {"count": count})
    print(json.dumps({"recovered": count}))
    return 0


def _authorize(args: argparse.Namespace) -> int:
    from mail_buddy.oauth import OAuthManager

    settings, database = _settings_and_database()
    settings.validate_runtime_secrets()
    OAuthManager(settings, database).authorize(
        host=args.redirect_host,
        bind_addr=args.bind,
        port=args.port,
        open_browser=args.open_browser,
    )
    print("Gmail account connected. Mail-Buddy labels are ready.")
    return 0


async def _disconnect_async() -> None:
    from mail_buddy.service import MailBuddyService

    settings, database = _settings_and_database()
    settings.validate_runtime_secrets()
    service = MailBuddyService(settings=settings, database=database)
    await service.disconnect()


def _disconnect(args: argparse.Namespace) -> int:
    if not args.yes:
        if not sys.stdin.isatty():
            raise RuntimeError("Use --yes for a non-interactive disconnect")
        confirmation = input(
            "Revoke Gmail access and remove local account metadata? Type DISCONNECT: "
        )
        if confirmation != "DISCONNECT":
            print("Cancelled.")
            return 1
    asyncio.run(_disconnect_async())
    print("Gmail access revoked; local account metadata removed.")
    print("Existing Gmail labels and message state were not changed.")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from mail_buddy.web import create_app

    settings = Settings()
    _configure_logging(settings.log_level)
    uvicorn.run(
        create_app(settings=settings),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
        access_log=False,
        log_level=settings.log_level.lower(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-buddy",
        description="Private Gmail sorting on local hardware.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the web dashboard and workers")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container listener
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--forwarded-allow-ips",
        default="127.0.0.1",
        help="Comma-separated trusted reverse-proxy addresses",
    )
    serve.set_defaults(handler=_serve)

    authorize = subparsers.add_parser(
        "auth", help="Authorize one Gmail account over a loopback callback"
    )
    authorize.add_argument("--bind", default="127.0.0.1")
    authorize.add_argument("--redirect-host", default="localhost")
    authorize.add_argument("--port", type=int, default=8765)
    authorize.add_argument("--open-browser", action="store_true")
    authorize.set_defaults(handler=_authorize)

    generate = subparsers.add_parser(
        "generate-secrets", help="Create encryption, session, and password secret files"
    )
    generate.add_argument("--output-dir", type=Path, default=Path("secrets"))
    generate.add_argument("--password-stdin", action="store_true")
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(handler=_generate_secrets)

    password = subparsers.add_parser(
        "hash-password", help="Create an Argon2id dashboard password hash"
    )
    password.add_argument("--password-stdin", action="store_true")
    password.set_defaults(handler=_hash_password)

    backup = subparsers.add_parser("backup", help="Create an atomic SQLite backup")
    backup.set_defaults(handler=_backup)

    training_status = subparsers.add_parser(
        "training-status", help="Print privacy-safe training readiness as JSON"
    )
    training_status.set_defaults(handler=_training_status)

    training_export = subparsers.add_parser(
        "training-export", help="Export the redacted owner-labeled MLX dataset"
    )
    training_export.add_argument("--output", type=Path, default=Path("-"))
    training_export.add_argument("--force", action="store_true")
    training_export.add_argument(
        "--if-due", action="store_true", help="Let the Pi enforce the configured schedule"
    )
    training_export.set_defaults(handler=_training_export)

    register_model = subparsers.add_parser(
        "register-main-model", help="Register an evaluated fine-tuned candidate"
    )
    register_model.add_argument("--metadata", type=Path)
    register_model.add_argument("--name")
    register_model.add_argument("--base-model", default="llama3.2:3b-instruct-q4_K_M")
    register_model.add_argument("--sha256")
    register_model.add_argument("--dataset-version", type=int)
    register_model.add_argument("--example-count", type=int)
    register_model.add_argument("--metrics")
    register_model.set_defaults(handler=_register_main_model)

    installed_model = subparsers.add_parser(
        "mark-main-model-installed", help="Verify and record a candidate installation"
    )
    installed_model.add_argument("--name", required=True)
    installed_model.add_argument("--host", choices=("laptop", "pi"), required=True)
    installed_model.add_argument("--sha256")
    installed_model.add_argument("--artifact", type=Path)
    installed_model.set_defaults(handler=_mark_main_model_installed)

    canary_model = subparsers.add_parser(
        "canary-main-model", help="Require one schema-valid local Ollama response"
    )
    canary_model.add_argument("--name", required=True)
    canary_model.set_defaults(handler=_canary_main_model)

    promote_model = subparsers.add_parser(
        "promote-main-model", help="Atomically activate a dual-host candidate"
    )
    promote_model.add_argument("--name", required=True)
    promote_model.set_defaults(handler=_promote_main_model)

    rollback_model = subparsers.add_parser(
        "rollback-main-model", help="Restore the previous fine-tuned main model"
    )
    rollback_model.set_defaults(handler=_rollback_main_model)

    fail_training = subparsers.add_parser(
        "fail-training", help="Close the active LoRA run without changing production"
    )
    fail_training.add_argument("--reason", default="trainer_failed")
    fail_training.set_defaults(handler=_fail_training)

    recover_training = subparsers.add_parser(
        "recover-stale-training", help="Fail abandoned training locks after a timeout"
    )
    recover_training.add_argument("--older-than-hours", type=int, default=12)
    recover_training.set_defaults(handler=_recover_stale_training)

    disconnect = subparsers.add_parser(
        "disconnect", help="Revoke Gmail and purge local account metadata"
    )
    disconnect.add_argument("--yes", action="store_true")
    disconnect.set_defaults(handler=_disconnect)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(2, f"mail-buddy: {exc}\n")
    return 2
