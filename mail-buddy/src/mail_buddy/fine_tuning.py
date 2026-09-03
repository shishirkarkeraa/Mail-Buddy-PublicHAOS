"""Deterministic export and promotion checks for laptop MLX fine-tuning."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mail_buddy.classification import (
    CLASSIFICATION_SYSTEM_PROMPT,
    build_classification_messages,
)
from mail_buddy.contracts import (
    MODEL_NAME,
    TAXONOMY_VERSION,
    Category,
    ClassificationResult,
    DecisionSource,
)
from mail_buddy.db import Database
from mail_buddy.security import SecretBox

LORA_MIN_EXAMPLES = 200
LORA_MIN_CATEGORY_EXAMPLES = 10
SENSITIVE_CATEGORIES = frozenset(
    {
        Category.SECURITY_OTP.value,
        Category.SECURITY_PASSWORD_RESET.value,
        Category.SECURITY_ACCOUNT_ALERT.value,
        Category.FINANCE_BANK_TRANSACTION.value,
    }
)
REQUIRED_RUNTIME_GATES = frozenset(
    {"structured_output", "prompt_injection", "primary_fallback", "both_hosts_down"}
)

@dataclass(frozen=True)
class ExportedBundle:
    manifest: dict[str, Any]
    train: list[dict[str, Any]]
    valid: list[dict[str, Any]]
    test: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "train": self.train,
            "valid": self.valid,
            "test": self.test,
        }


def schedule_state(
    *,
    now: datetime,
    last_finished_at: str | None,
    interval_days: int,
    hour_local: int,
    timezone: str,
) -> dict[str, Any]:
    """Return a timezone-aware due state; an overdue run remains due after sleep."""

    if now.tzinfo is None:
        raise ValueError("Schedule time must be timezone-aware")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
        timezone = "UTC"
    local_now = now.astimezone(zone)
    if last_finished_at:
        try:
            last = datetime.fromisoformat(last_finished_at).astimezone(zone)
        except ValueError:
            last = local_now - timedelta(days=interval_days)
        next_date = last.date() + timedelta(days=interval_days)
    else:
        next_date = local_now.date()
    scheduled_local = datetime.combine(next_date, time(hour=hour_local), tzinfo=zone)
    return {
        "due": local_now >= scheduled_local,
        "timezone": timezone,
        "next_eligible_at": scheduled_local.astimezone(UTC).isoformat(),
    }


def readiness(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(row["category"]) for row in rows)
    eligible = sorted(
        category for category, count in counts.items() if count >= LORA_MIN_CATEGORY_EXAMPLES
    )
    eligible_rows = [row for row in rows if str(row["category"]) in eligible]
    sender_splits = _split_sender_groups(eligible_rows)
    split_counts = Counter(
        sender_splits[str(row.get("sender_key") or row["message_id"])]
        for row in eligible_rows
    )
    split_ready = all(split_counts.get(name, 0) > 0 for name in ("train", "valid", "test"))
    return {
        "ready": (
            len(rows) >= LORA_MIN_EXAMPLES and len(eligible) >= 2 and split_ready
        ),
        "example_count": len(rows),
        "required_examples": LORA_MIN_EXAMPLES,
        "eligible_categories": eligible,
        "category_counts": dict(sorted(counts.items())),
        "split_ready": split_ready,
        "split_counts": dict(sorted(split_counts.items())),
    }


def _split_sender_groups(rows: list[dict[str, Any]]) -> dict[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("sender_key") or row["message_id"])
        grouped.setdefault(key, []).append(row)
    by_category: dict[str, list[str]] = {}
    for sender_key, items in grouped.items():
        category = Counter(str(item["category"]) for item in items).most_common(1)[0][0]
        by_category.setdefault(category, []).append(sender_key)
    result: dict[str, str] = {}
    for category, keys in sorted(by_category.items()):
        ordered = sorted(
            keys,
            key=lambda key: hashlib.sha256(f"{category}:{key}".encode()).hexdigest(),
        )
        for index, key in enumerate(ordered):
            remaining = len(ordered) - index
            if len(ordered) >= 10:
                slot = index % 10
                result[key] = "train" if slot < 7 else "valid" if slot == 7 else "test"
            elif len(ordered) >= 3 and remaining == 2:
                result[key] = "valid"
            elif len(ordered) >= 2 and remaining == 1:
                result[key] = "test"
            else:
                result[key] = "train"
    return result


def _sender_domain(feature_text: str) -> str:
    prefix = "sender_domain "
    for line in feature_text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()[:253]
    return ""


def _training_context(feature_text: str) -> dict[str, object]:
    return {
        "sender_domain": _sender_domain(feature_text),
        "two_way_history": False,
        "has_list_headers": False,
        "gmail_category_labels": [],
        "college_domains": [],
        "attachment_incomplete": False,
        "personalized_model_hint": None,
    }


def export_bundle(database: Database, secret_box: SecretBox) -> ExportedBundle:
    rows = database.list_training_examples()
    state = readiness(rows)
    if not state["ready"]:
        raise ValueError(
            "LoRA training requires 200 examples, two eligible categories, and "
            "sender-isolated train/validation/test splits"
        )
    eligible = set(state["eligible_categories"])
    rows = [row for row in rows if str(row["category"]) in eligible]
    sender_splits = _split_sender_groups(rows)
    datasets: dict[str, list[dict[str, Any]]] = {"train": [], "valid": [], "test": []}
    fingerprint_input: list[str] = []
    for row in rows:
        message_id = str(row["message_id"])
        sender_key = str(row.get("sender_key") or message_id)
        category = Category(str(row["category"]))
        text = secret_box.decrypt(str(row["content_ciphertext"]))
        split = sender_splits[sender_key]
        completion = ClassificationResult(
            primary_category=category,
            source=DecisionSource.LLAMA,
            model=MODEL_NAME,
        ).model_dump_json()
        messages = build_classification_messages(text, _training_context(text))
        messages.append({"role": "assistant", "content": completion})
        datasets[split].append(
            {
                "messages": messages,
                "message_id_hash": hashlib.sha256(message_id.encode()).hexdigest(),
                "category": category.value,
            }
        )
        fingerprint_input.append(f"{message_id}:{category.value}:{sender_key}")
    for items in datasets.values():
        items.sort(key=lambda item: str(item["message_id_hash"]))
    digest = hashlib.sha256("\n".join(sorted(fingerprint_input)).encode()).hexdigest()
    manifest = {
        "schema": 1,
        "dataset_version": int(digest[:12], 16),
        "dataset_sha256": digest,
        "taxonomy_version": TAXONOMY_VERSION,
        "system_prompt_sha256": hashlib.sha256(
            CLASSIFICATION_SYSTEM_PROMPT.encode()
        ).hexdigest(),
        "structured_output_schema": ClassificationResult.model_json_schema(),
        "base_model": MODEL_NAME,
        "example_count": len(rows),
        "eligible_categories": sorted(eligible),
        "split_counts": {key: len(value) for key, value in datasets.items()},
        "contains_full_bodies": False,
        "contains_attachments": False,
    }
    return ExportedBundle(manifest=manifest, **datasets)


def promotion_metrics_pass(
    candidate: dict[str, Any],
    production: dict[str, Any] | None,
) -> tuple[bool, str]:
    if int(candidate.get("evaluated_count", 0)) < 5:
        return False, "held_out_sample_too_small"
    gates = dict(candidate.get("gates") or {})
    for gate in REQUIRED_RUNTIME_GATES:
        if gates.get(gate) is not True:
            return False, f"runtime_gate_failed:{gate}"
    if float(candidate.get("accuracy", 0)) < 0.70:
        return False, "accuracy_below_floor"
    baseline = production or {"accuracy": 0.0, "macro_f1": 0.0, "category_recall": {}}
    if float(candidate.get("accuracy", 0)) < float(baseline.get("accuracy", 0)):
        return False, "accuracy_regression"
    if float(candidate.get("macro_f1", 0)) < float(baseline.get("macro_f1", 0)):
        return False, "macro_f1_regression"
    candidate_recall = dict(candidate.get("category_recall") or {})
    baseline_recall = dict(baseline.get("category_recall") or {})
    for category in SENSITIVE_CATEGORIES:
        if float(candidate_recall.get(category, 0)) < float(baseline_recall.get(category, 0)):
            return False, f"sensitive_recall_regression:{category}"
    return True, "passed"
