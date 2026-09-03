from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from mail_buddy.contracts import (
    TAXONOMY_VERSION,
    Category,
    ClassificationResult,
    DecisionSource,
    MessageOrigin,
    MessageState,
    ReasonCode,
)
from mail_buddy.db import Database


def make_db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "mail-buddy.sqlite3")
    database.initialize()
    return database


def test_main_model_requires_both_hosts_and_rolls_back_atomically(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    names = (
        "mail-buddy-llama:20260901T020000Z-aaaaaa",
        "mail-buddy-llama:20260902T020000Z-bbbbbb",
    )
    for index, name in enumerate(names):
        database.register_main_model(
            name=name,
            base_model="base",
            artifact_sha256=str(index) * 64,
            dataset_version=index,
            example_count=200 + index,
            accuracy=0.8 + index / 100,
            macro_f1=0.78 + index / 100,
            category_recall={},
        )
        database.mark_main_model_installed(name, "laptop")
        if index == 0:
            try:
                database.promote_main_model(name)
            except ValueError as exc:
                assert "both hosts" in str(exc)
        database.mark_main_model_installed(name, "pi")
        database.promote_main_model(name)

    assert database.get_active_main_model()["name"] == names[1]
    assert database.rollback_main_model() == names[0]
    assert database.get_active_main_model()["name"] == names[0]


def test_training_run_lock_and_stale_recovery(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    run_id = database.acquire_training_run("lora")
    assert run_id is not None
    assert database.acquire_training_run("lora") is None
    with database.connect() as connection:
        connection.execute(
            "UPDATE training_runs SET started_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(days=2)).isoformat(), run_id),
        )
    assert database.recover_stale_training_runs(timedelta(hours=12)) == 1
    assert database.acquire_training_run("lora") is not None


def test_database_queue_and_decision_lifecycle(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    assert database.enqueue_message("message-1", "thread-1", MessageOrigin.BACKFILL)
    assert not database.enqueue_message("message-1", "thread-1", MessageOrigin.BACKFILL)

    job = database.claim_job()
    assert job is not None
    assert job["message_id"] == "message-1"
    database.save_decision(
        "message-1",
        ClassificationResult(
            primary_category=Category.SECURITY_OTP,
            source=DecisionSource.RULE,
            reason_codes=[ReasonCode.OTP_INTENT],
        ),
        sender_key="hashed-sender",
        internal_date=123,
        had_inbox=True,
        state=MessageState.STAGED,
    )
    database.complete_job(job["id"])

    message = database.get_message("message-1")
    assert message is not None
    assert message["state"] == MessageState.STAGED.value
    assert message["primary_category"] == Category.SECURITY_OTP.value
    assert "OTP_INTENT" in message["reason_codes"]


def test_category_approval_queues_staged_messages(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.enqueue_message("message-1", "thread-1", MessageOrigin.BACKFILL)
    job = database.claim_job()
    assert job is not None
    database.save_decision(
        "message-1",
        ClassificationResult(
            primary_category=Category.SUBSCRIPTION,
            source=DecisionSource.LLAMA,
            model="llama3.2:3b-instruct-q4_K_M",
        ),
        sender_key="sender",
        internal_date=123,
        had_inbox=False,
        state=MessageState.STAGED,
    )
    database.complete_job(job["id"])

    batch_id = database.approve_category(
        Category.SUBSCRIPTION,
        TAXONOMY_VERSION,
        "llama3.2:3b-instruct-q4_K_M",
    )
    assert batch_id
    assert database.is_category_approved(
        Category.SUBSCRIPTION,
        TAXONOMY_VERSION,
        "llama3.2:3b-instruct-q4_K_M",
    )
    apply_job = database.claim_job()
    assert apply_job is not None
    assert apply_job["kind"] == "apply"


def test_live_staging_does_not_inflate_backfill_metrics(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.start_backfill("history-1")
    database.enqueue_message("live-1", "thread-1", MessageOrigin.LIVE)
    database.save_decision(
        "live-1",
        ClassificationResult(
            primary_category=Category.OTHER,
            source=DecisionSource.FALLBACK,
        ),
        sender_key="sender",
        internal_date=123,
        had_inbox=True,
        state=MessageState.STAGED,
    )

    assert database.get_backfill()["total_staged"] == 0


def test_reprocessing_backfill_decision_counts_message_once(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.start_backfill("history-1")
    database.enqueue_message("historical-1", "thread-1", MessageOrigin.BACKFILL)
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )

    for state in (MessageState.NEEDS_REVIEW, MessageState.STAGED):
        database.save_decision(
            "historical-1",
            decision,
            sender_key="sender",
            internal_date=123,
            had_inbox=True,
            state=state,
        )

    assert database.get_backfill()["total_staged"] == 1


def test_category_samples_prioritize_sender_diversity(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.start_backfill("history-1")
    decision = ClassificationResult(
        primary_category=Category.SUBSCRIPTION,
        source=DecisionSource.RULE,
    )
    for index, sender_key in enumerate(("same", "same", "different"), start=1):
        message_id = f"message-{index}"
        database.enqueue_message(
            message_id,
            f"thread-{index}",
            MessageOrigin.BACKFILL,
        )
        database.save_decision(
            message_id,
            decision,
            sender_key=sender_key,
            internal_date=index,
            had_inbox=True,
            state=MessageState.STAGED,
        )

    samples = database.list_category_samples(Category.SUBSCRIPTION, limit=2)

    assert {sample["sender_key"] for sample in samples} == {"same", "different"}


def test_backup_keeps_database_readable(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.set_setting("example", "value")

    backup_path = database.backup(tmp_path / "backups")
    restored = Database(backup_path)

    assert restored.get_setting("example") == "value"


def test_expired_history_recovery_updates_cursor_and_backfill_together(
    tmp_path: Path,
) -> None:
    database = make_db(tmp_path)
    database.connect_account("owner@example.com", "old-history")

    database.recover_expired_history("new-history")

    assert database.get_account()["history_id"] == "new-history"
    backfill = database.get_backfill()
    assert backfill["status"] == "running"
    assert backfill["captured_history_id"] == "new-history"
    assert backfill["page_token"] is None


def test_duplicate_requeues_failed_classification_job(tmp_path: Path) -> None:
    database = make_db(tmp_path)
    database.enqueue_message("message-1", "thread-1", MessageOrigin.LIVE)
    failed = database.claim_job()
    assert failed is not None
    database.fail_job(
        int(failed["id"]),
        "message-1",
        "gmail_transient",
    )

    assert database.enqueue_message(
        "message-1",
        "thread-1",
        MessageOrigin.LIVE,
    )

    retried = database.claim_job()
    assert retried is not None
    assert retried["message_id"] == "message-1"
    assert retried["attempts"] == 1
    message = database.get_message("message-1")
    assert message is not None
    assert message["state"] == MessageState.QUEUED.value
    assert message["error_code"] is None


def test_label_aliases_survive_recreation_and_purge_with_account(
    tmp_path: Path,
) -> None:
    database = make_db(tmp_path)
    label_name = "Subscriptions"
    database.upsert_label(Category.SUBSCRIPTION.value, label_name, "old-id")
    database.upsert_label(Category.SUBSCRIPTION.value, label_name, "new-id")
    database.set_setting("college_domains", "college.example")
    database.set_setting("pending_batch:message:gmail-id", "batch-id")

    assert database.get_label_aliases() == {
        "old-id": Category.SUBSCRIPTION.value,
        "new-id": Category.SUBSCRIPTION.value,
    }

    database.purge_account_data()

    assert database.get_label_aliases() == {}
    assert database.get_setting("college_domains") is None
    assert database.get_setting("pending_batch:message:gmail-id") is None


def test_correspondent_cache_uses_shorter_ttl_for_negative_results(
    tmp_path: Path,
) -> None:
    database = make_db(tmp_path)
    database.set_correspondent("sender", False)
    with database.connect() as connection:
        connection.execute(
            "UPDATE correspondents SET checked_at = ? WHERE address_hash = ?",
            (
                (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                "sender",
            ),
        )
    assert database.get_correspondent("sender") is None

    database.set_correspondent("sender", True)
    with database.connect() as connection:
        connection.execute(
            "UPDATE correspondents SET checked_at = ? WHERE address_hash = ?",
            (
                (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                "sender",
            ),
        )
    assert database.get_correspondent("sender") is True
