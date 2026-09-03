from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from mail_buddy.classification import CLASSIFICATION_SYSTEM_PROMPT
from mail_buddy.contracts import Category, MessageOrigin
from mail_buddy.db import Database
from mail_buddy.fine_tuning import (
    export_bundle,
    promotion_metrics_pass,
    readiness,
    schedule_state,
)
from mail_buddy.security import SecretBox

ROOT = Path(__file__).resolve().parents[1]


def _dataset(tmp_path: Path) -> tuple[Database, SecretBox]:
    database = Database(tmp_path / "mail-buddy.sqlite3")
    database.initialize()
    secret_box = SecretBox(SecretBox.generate_key())
    categories = (Category.JOB_RELATED, Category.SHOPPING_PROMOTION)
    for index in range(200):
        category = categories[index % 2]
        database.enqueue_message(
            f"message-{index}", f"thread-{index}", MessageOrigin.LIVE
        )
        database.upsert_training_example(
            message_id=f"message-{index}",
            sender_key=f"sender-{index}",
            content_ciphertext=secret_box.encrypt(
                f"sender_domain sender{index}.example\nsubject private sample {index}"
            ),
            category=category,
            predicted_category=Category.OTHER,
            source="accuracy_review",
        )
    return database, secret_box


def test_export_is_reproducible_sender_isolated_and_uses_production_contract(
    tmp_path: Path,
) -> None:
    database, secret_box = _dataset(tmp_path)

    first = export_bundle(database, secret_box)
    second = export_bundle(database, secret_box)

    assert first.as_dict() == second.as_dict()
    assert first.manifest["system_prompt_sha256"] == hashlib.sha256(
        CLASSIFICATION_SYSTEM_PROMPT.encode()
    ).hexdigest()
    assert first.manifest["split_counts"] == {"train": 140, "valid": 20, "test": 40}
    assert first.train[0]["messages"][0]["content"] == CLASSIFICATION_SYSTEM_PROMPT
    completion = json.loads(first.train[0]["messages"][-1]["content"])
    assert set(completion) == {
        "taxonomy_version",
        "primary_category",
        "alternate_category",
        "source",
        "review_required",
        "reason_codes",
        "flags",
        "model",
    }
    splits: dict[str, set[str]] = {}
    for name in ("train", "valid", "test"):
        splits[name] = {row["message_id_hash"] for row in getattr(first, name)}
    assert not splits["train"] & splits["valid"]
    assert not splits["train"] & splits["test"]
    assert not splits["valid"] & splits["test"]


def test_readiness_requires_sender_isolated_validation_and_test_splits() -> None:
    rows = [
        {"message_id": str(index), "sender_key": "one-sender", "category": "job_related"}
        for index in range(200)
    ]
    assert not readiness(rows)["ready"]


def test_schedule_uses_local_hour_and_catches_up_after_sleep() -> None:
    before = schedule_state(
        now=datetime(2026, 9, 2, 21, 0, tzinfo=UTC),
        last_finished_at=None,
        interval_days=7,
        hour_local=2,
        timezone="Asia/Kolkata",
    )
    after = schedule_state(
        now=datetime(2026, 9, 2, 21, 0, tzinfo=UTC),
        last_finished_at="2026-08-26T20:00:00+00:00",
        interval_days=7,
        hour_local=2,
        timezone="Asia/Kolkata",
    )
    assert before["due"]
    assert after["due"]
    assert after["next_eligible_at"] == "2026-09-02T20:30:00+00:00"


def test_promotion_rejects_missing_gates_and_sensitive_regression() -> None:
    candidate = {
        "accuracy": 0.8,
        "macro_f1": 0.8,
        "evaluated_count": 20,
        "category_recall": {Category.SECURITY_OTP.value: 0.8},
        "gates": {
            "structured_output": True,
            "prompt_injection": True,
            "primary_fallback": True,
            "both_hosts_down": True,
        },
    }
    baseline = {
        "accuracy": 0.75,
        "macro_f1": 0.75,
        "category_recall": {Category.SECURITY_OTP.value: 0.9},
    }
    assert promotion_metrics_pass({**candidate, "gates": {}}, baseline)[0] is False
    assert promotion_metrics_pass(candidate, baseline) == (
        False,
        "sensitive_recall_regression:security_otp",
    )


def test_training_bundle_helper_writes_private_mlx_files(tmp_path: Path) -> None:
    database, secret_box = _dataset(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(export_bundle(database, secret_box).as_dict()))
    output = tmp_path / "private-dataset"
    adapter = tmp_path / "adapter"

    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/training-bundle.py"),
            "prepare",
            "--bundle",
            str(bundle_path),
            "--output",
            str(output),
            "--model",
            "test-model",
            "--adapter-path",
            str(adapter),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (output / "train.jsonl").stat().st_mode & 0o777 == 0o600
    config = (output / "lora-config.yaml").read_text()
    for expected in (
        "seed: 42",
        "batch_size: 1",
        "grad_accumulation_steps: 8",
        "num_layers: 8",
        "rank: 8",
        "scale: 20.0",
        "max_seq_length: 1024",
        "grad_checkpoint: true",
        "mask_prompt: true",
    ):
        assert expected in config
