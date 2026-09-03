from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any

import pytest

from mail_buddy.config import Settings
from mail_buddy.contracts import (
    CATEGORY_LABELS,
    BackfillState,
    Category,
    ClassificationResult,
    DecisionSource,
    EmailMetadata,
    MessageOrigin,
    MessageState,
    ParsedEmail,
    ReasonCode,
)
from mail_buddy.db import Database
from mail_buddy.gmail import GmailError, GmailErrorCode, HistoryExpiredError
from mail_buddy.security import SecretBox
from mail_buddy.service import MailBuddyService, ServiceUnavailableError


class Extractor:
    def parse_metadata(self, message: dict[str, Any]) -> EmailMetadata:
        return EmailMetadata(
            message_id=str(message["id"]),
            thread_id=str(message.get("threadId", message["id"])),
            internal_date=int(message.get("internalDate", 0)),
            sender=str(message.get("sender", "sender@example.com")),
            sender_domain="example.com",
            subject=str(message.get("subject", "withheld from persistence")),
            headers={},
            label_ids=set(message.get("labelIds", [])),
            snippet=str(message.get("snippet", "")),
            had_inbox="INBOX" in message.get("labelIds", []),
        )

    async def parse_full(
        self,
        message: dict[str, Any],
        attachment_loader: Any,
    ) -> ParsedEmail:
        metadata = self.parse_metadata(message)
        return ParsedEmail(metadata=metadata, body_text=str(message.get("body", "")))


class Classifier:
    def __init__(self, decision: ClassificationResult) -> None:
        self.decision = decision

    def classify_metadata(
        self,
        metadata: EmailMetadata,
        *,
        college_domains: set[str],
    ) -> ClassificationResult:
        return self.decision


class Gmail:
    def __init__(self) -> None:
        self.profile = {"emailAddress": "owner@example.com", "historyId": "20"}
        self.messages: dict[str, dict[str, Any]] = {}
        self.received_pages: list[tuple[list[dict[str, Any]], str | None]] = []
        self.modifications: list[tuple[str, set[str], set[str]]] = []
        self.trashed: list[str] = []
        self.untrashed: list[str] = []
        self.history_error: BaseException | None = None
        self.revoke_error: BaseException | None = None
        self.revoke_calls = 0

    def get_profile(self) -> dict[str, Any]:
        return dict(self.profile)

    def ensure_labels(self) -> dict[str, str]:
        result = {category.value: f"label-{category.value}" for category in Category}
        result["needs_review"] = "label-needs-review"
        return result

    def list_received_page(
        self,
        *,
        page_token: str | None = None,
        max_results: int = 500,
    ) -> tuple[list[dict[str, Any]], str | None]:
        del page_token, max_results
        return self.received_pages.pop(0)

    def get_metadata(self, message_id: str) -> dict[str, Any]:
        return dict(self.messages[message_id])

    def get_full_message(self, message_id: str) -> dict[str, Any]:
        return dict(self.messages[message_id])

    def has_sent_to(self, sender: str) -> bool:
        del sender
        return False

    def modify_message_labels(
        self,
        message_id: str,
        *,
        add_label_ids: set[str],
        remove_label_ids: set[str],
    ) -> dict[str, Any]:
        current = set(self.messages[message_id].get("labelIds", []))
        current.difference_update(remove_label_ids)
        current.update(add_label_ids)
        self.messages[message_id]["labelIds"] = sorted(current)
        self.modifications.append((message_id, set(add_label_ids), set(remove_label_ids)))
        return dict(self.messages[message_id])

    def trash_message(self, message_id: str) -> dict[str, Any]:
        self.messages[message_id]["labelIds"] = ["TRASH"]
        self.trashed.append(message_id)
        return dict(self.messages[message_id])

    def untrash_message(self, message_id: str) -> dict[str, Any]:
        self.messages[message_id]["labelIds"] = []
        self.untrashed.append(message_id)
        return dict(self.messages[message_id])

    def list_history_added(
        self,
        history_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        del history_id
        if self.history_error:
            raise self.history_error
        return [], str(self.profile["historyId"])

    def revoke(self) -> None:
        self.revoke_calls += 1
        if self.revoke_error is not None:
            raise self.revoke_error


def make_service(
    tmp_path: Path,
    gmail: Gmail,
    decision: ClassificationResult,
) -> tuple[MailBuddyService, Database]:
    settings = Settings(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        encryption_key=SecretBox.generate_key(),
        demo_mode=True,
        disable_worker=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    database.connect_account("owner@example.com", "10")
    service = MailBuddyService(
        settings,
        database,
        gmail_client=gmail,
        extractor=Extractor(),
        classifier=Classifier(decision),
    )
    return service, database


@pytest.mark.asyncio
async def test_backfill_stages_without_mutation_then_approval_and_undo(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.messages["m1"] = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "100",
        "sender": "bank@example.com",
        "labelIds": ["INBOX", "STARRED"],
    }
    gmail.received_pages.append(([{"id": "m1", "threadId": "t1", "internalDate": "100"}], None))
    decision = ClassificationResult(
        primary_category=Category.FINANCE_BANK_TRANSACTION,
        source=DecisionSource.RULE,
        reason_codes=[ReasonCode.TRANSACTION_INTENT],
    )
    service, database = make_service(tmp_path, gmail, decision)

    await service.start_backfill()
    assert await service.scan_backfill_once() is True
    assert await service.process_one_job() is True

    staged = database.get_message("m1")
    assert staged is not None
    assert staged["state"] == MessageState.STAGED.value
    assert gmail.modifications == []
    assert set(gmail.messages["m1"]["labelIds"]) == {"INBOX", "STARRED"}

    batch_id = await service.approve_category(Category.FINANCE_BANK_TRANSACTION)
    assert await service.process_one_job() is True
    category_label = f"label-{Category.FINANCE_BANK_TRANSACTION.value}"
    assert set(gmail.messages["m1"]["labelIds"]) == {
        "STARRED",
        category_label,
    }
    assert database.get_message("m1")["state"] == MessageState.APPLIED.value

    await service.undo_batch(batch_id)
    assert set(gmail.messages["m1"]["labelIds"]) == {"INBOX", "STARRED"}
    assert database.list_audit_batch(batch_id) == []


@pytest.mark.asyncio
async def test_history_expiration_starts_safe_full_rescan(tmp_path: Path) -> None:
    gmail = Gmail()
    gmail.profile["historyId"] = "99"
    gmail.history_error = HistoryExpiredError()
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)

    assert await service.poll_history_once() == 0

    assert database.get_account()["history_id"] == "99"
    backfill = database.get_backfill()
    assert backfill["status"] == BackfillState.RUNNING.value
    assert backfill["captured_history_id"] == "99"


@pytest.mark.asyncio
async def test_live_review_files_message_under_needs_review_and_reconciles_single_app_label(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.messages["m2"] = {
        "id": "m2",
        "threadId": "t2",
        "internalDate": "200",
        "sender": "alerts@example.com",
        "labelIds": ["INBOX", "old-app-label"],
    }
    decision = ClassificationResult(
        primary_category=Category.SECURITY_ACCOUNT_ALERT,
        source=DecisionSource.FALLBACK,
        review_required=True,
        reason_codes=[ReasonCode.AUTHENTICATION_FAILURE],
    )
    service, database = make_service(tmp_path, gmail, decision)
    await service.reconcile_labels()
    database.enqueue_message("m2", "t2", MessageOrigin.LIVE)

    assert await service.process_one_job() is True

    labels = set(gmail.messages["m2"]["labelIds"])
    assert "INBOX" not in labels
    assert "label-needs-review" in labels
    assert database.get_message("m2")["state"] == MessageState.NEEDS_REVIEW.value
    assert all(f"label-{category.value}" not in labels for category in CATEGORY_LABELS)


@pytest.mark.asyncio
async def test_live_forwarded_mail_uses_original_content_and_applies_destination_label(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    forwarded_body = (
        "---------- Forwarded message ---------\n"
        "From: alerts@bank.example\n"
        "Date: Monday\n"
        "To: old-address@example.com\n"
        "Subject: Transaction alert\n\n"
        "Your bank account was debited for INR 500."
    )
    gmail.messages["forwarded"] = {
        "id": "forwarded",
        "threadId": "forwarded-thread",
        "internalDate": "225",
        "labelIds": ["INBOX"],
        "snippet": "Forwarded message",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": "forwarder@example.com"},
                {"name": "Subject", "value": "Fwd: please see"},
            ],
            "body": {
                "data": base64.urlsafe_b64encode(forwarded_body.encode()).decode().rstrip("="),
            },
        },
    }
    settings = Settings(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        encryption_key=SecretBox.generate_key(),
        demo_mode=True,
        disable_worker=True,
    )
    database = Database(settings.database_path)
    database.initialize()
    database.connect_account("destination@example.com", "20")
    service = MailBuddyService(settings, database, gmail_client=gmail)
    await service.reconcile_labels()
    database.approve_category(
        Category.FINANCE_BANK_TRANSACTION,
        "1",
        settings.ollama_model,
    )
    database.enqueue_message("forwarded", "forwarded-thread", MessageOrigin.LIVE)

    assert await service.process_one_job() is True
    assert await service.process_one_job() is True

    labels = set(gmail.messages["forwarded"]["labelIds"])
    assert f"label-{Category.FINANCE_BANK_TRANSACTION.value}" in labels
    assert "INBOX" not in labels
    stored = database.get_message("forwarded")
    assert stored is not None
    assert stored["state"] == MessageState.APPLIED.value
    assert stored["primary_category"] == Category.FINANCE_BANK_TRANSACTION.value


@pytest.mark.asyncio
async def test_backfill_review_item_is_staged_for_individual_review_only(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.messages["review-backfill"] = {
        "id": "review-backfill",
        "threadId": "review-thread",
        "internalDate": "250",
        "sender": "alerts@example.com",
        "labelIds": ["INBOX"],
    }
    gmail.received_pages.append(
        (
            [
                {
                    "id": "review-backfill",
                    "threadId": "review-thread",
                    "internalDate": "250",
                }
            ],
            None,
        )
    )
    decision = ClassificationResult(
        primary_category=Category.SECURITY_ACCOUNT_ALERT,
        source=DecisionSource.FALLBACK,
        review_required=True,
        reason_codes=[ReasonCode.INSUFFICIENT_EVIDENCE],
    )
    service, database = make_service(tmp_path, gmail, decision)

    await service.start_backfill()
    assert await service.scan_backfill_once() is True
    assert await service.process_one_job() is True

    stored = database.get_message("review-backfill")
    assert stored is not None
    assert stored["state"] == MessageState.NEEDS_REVIEW.value
    assert gmail.modifications == []
    assert gmail.messages["review-backfill"]["labelIds"] == ["INBOX"]


@pytest.mark.asyncio
async def test_manual_correction_cancels_stale_classification(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.messages["corrected"] = {
        "id": "corrected",
        "threadId": "corrected-thread",
        "internalDate": "275",
        "sender": "friend@example.com",
        "subject": "Dinner tomorrow",
        "labelIds": ["INBOX"],
    }
    original = ClassificationResult(
        primary_category=Category.PROMOTION_GENERAL,
        source=DecisionSource.LLAMA,
        model="llama3.2:3b-instruct-q4_K_M",
    )
    service, database = make_service(tmp_path, gmail, original)
    database.enqueue_message("corrected", "corrected-thread", MessageOrigin.LIVE)

    batch_id = await service.correct_message("corrected", Category.DIRECT_PERSONAL)
    await service._classify_message("corrected")
    assert await service.process_one_job() is True

    stored = database.get_message("corrected")
    assert stored is not None
    assert stored["primary_category"] == Category.DIRECT_PERSONAL.value
    assert stored["decision_source"] == DecisionSource.MANUAL.value
    examples = database.list_training_examples()
    assert len(examples) == 1
    assert examples[0]["message_id"] == "corrected"
    assert examples[0]["category"] == Category.DIRECT_PERSONAL.value
    assert examples[0]["source"] == "correction"
    assert "Dinner tomorrow" in service.secret_box.decrypt(str(examples[0]["content_ciphertext"]))
    assert database.list_audit_batch(str(batch_id))[0]["message_id"] == "corrected"
    with database.connect() as connection:
        classify_job = connection.execute(
            "SELECT status FROM jobs WHERE message_id = ? AND kind = 'classify'",
            ("corrected",),
        ).fetchone()
    assert classify_job is not None
    assert classify_job["status"] == "done"
    assert f"label-{Category.DIRECT_PERSONAL.value}" in set(gmail.messages["corrected"]["labelIds"])

    await service.undo_batch(str(batch_id))

    assert set(gmail.messages["corrected"]["labelIds"]) == {"INBOX"}


@pytest.mark.asyncio
async def test_message_preview_is_untruncated_and_not_persisted(tmp_path: Path) -> None:
    gmail = Gmail()
    gmail.messages["preview"] = {
        "id": "preview",
        "threadId": "preview-thread",
        "internalDate": "300",
        "sender": "s" * 400,
        "subject": "u" * 600,
        "snippet": "n" * 1_200,
        "body": "Full unredacted email body" + "x" * 5_000,
        "labelIds": ["INBOX"],
    }
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)

    preview = await service.get_message_preview("preview")

    assert preview == {
        "message_id": "preview",
        "sender": "s" * 400,
        "subject": "u" * 600,
        "body": "Full unredacted email body" + "x" * 5_000,
        "attachment_text": "",
        "content": "Full unredacted email body" + "x" * 5_000,
        "internal_date": 300,
    }
    assert database.get_message("preview") is None
    assert gmail.modifications == []


@pytest.mark.asyncio
async def test_live_promotional_mail_is_moved_to_trash_and_can_be_undone(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.messages["promotion"] = {
        "id": "promotion",
        "threadId": "promotion-thread",
        "internalDate": "400",
        "sender": "offers@example.com",
        "labelIds": ["INBOX"],
    }
    decision = ClassificationResult(
        primary_category=Category.SHOPPING_PROMOTION,
        source=DecisionSource.RULE,
    )
    service, database = make_service(tmp_path, gmail, decision)
    await service.reconcile_labels()
    database.approve_category(
        Category.SHOPPING_PROMOTION,
        "1",
        service.settings.ollama_model,
    )
    database.enqueue_message("promotion", "promotion-thread", MessageOrigin.LIVE)

    assert await service.process_one_job() is True
    assert await service.process_one_job() is True
    assert gmail.trashed == ["promotion"]
    assert gmail.messages["promotion"]["labelIds"] == ["TRASH"]
    stored = database.get_message("promotion")
    assert stored is not None
    assert stored["state"] == MessageState.APPLIED.value
    batches = database.list_recent_batches()
    assert batches[0]["action"] == "trash_promotion"

    await service.undo_batch(str(batches[0]["batch_id"]))

    assert gmail.untrashed == ["promotion"]
    assert set(gmail.messages["promotion"]["labelIds"]) == {"INBOX"}


@pytest.mark.asyncio
async def test_personalized_training_promotes_only_evaluated_owner_labels(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)
    for index in range(15):
        for prefix, category, text in (
            (
                "job",
                Category.JOB_RELATED,
                "alpha sprint roadmap deliverable milestone",
            ),
            (
                "shop",
                Category.SHOPPING_PROMOTION,
                "retail coupon shopping sale discount",
            ),
        ):
            message_id = f"{prefix}-{index}"
            database.enqueue_message(message_id, message_id, MessageOrigin.LIVE)
            database.upsert_training_example(
                message_id=message_id,
                sender_key=f"{prefix}-sender-{index}",
                content_ciphertext=service.secret_box.encrypt(text),
                category=category,
                predicted_category=category,
                source="accuracy_review",
            )

    promoted = await service.train_personalized_model(force=True)

    active = database.get_active_personalized_model()
    assert promoted is True
    assert active is not None
    assert active["example_count"] == 30
    assert active["evaluated_count"] == 30
    assert active["accuracy"] == 1.0


@pytest.mark.asyncio
async def test_undo_translates_label_id_after_recreation(tmp_path: Path) -> None:
    gmail = Gmail()
    gmail.messages["alias-message"] = {
        "id": "alias-message",
        "threadId": "alias-thread",
        "internalDate": "400",
        "sender": "sender@example.com",
        "labelIds": [f"label-{Category.OTHER.value}"],
    }
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.MANUAL,
    )
    service, database = make_service(tmp_path, gmail, decision)
    await service.reconcile_labels()
    database.upsert_label(
        Category.SUBSCRIPTION.value,
        CATEGORY_LABELS[Category.SUBSCRIPTION],
        "old-subscription-id",
    )
    database.upsert_label(
        Category.SUBSCRIPTION.value,
        CATEGORY_LABELS[Category.SUBSCRIPTION],
        "new-subscription-id",
    )
    database.enqueue_message(
        "alias-message",
        "alias-thread",
        MessageOrigin.BACKFILL,
    )
    database.save_decision(
        "alias-message",
        decision,
        sender_key="sender-key",
        internal_date=400,
        had_inbox=False,
        state=MessageState.APPLIED,
    )
    database.create_audit(
        batch_id="alias-batch",
        message_id="alias-message",
        action="apply",
        before_app_label_ids=["old-subscription-id"],
        before_had_inbox=True,
        after_category=Category.OTHER,
    )

    await service.undo_batch("alias-batch")

    assert set(gmail.messages["alias-message"]["labelIds"]) == {
        "INBOX",
        "new-subscription-id",
    }
    stored = database.get_message("alias-message")
    assert stored is not None
    assert stored["primary_category"] == Category.SUBSCRIPTION.value


@pytest.mark.asyncio
async def test_failed_revocation_preserves_account_for_retry(
    tmp_path: Path,
) -> None:
    gmail = Gmail()
    gmail.revoke_error = GmailError(
        GmailErrorCode.TRANSIENT,
        retryable=True,
    )
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)
    database.set_setting("oauth_token", "encrypted-token")
    database.enqueue_message("retained", "thread", MessageOrigin.LIVE)

    with pytest.raises(ServiceUnavailableError, match="oauth_revoke_failed"):
        await service.disconnect()

    assert database.get_account()["email"] == "owner@example.com"
    assert database.get_setting("oauth_token") == "encrypted-token"
    assert database.get_message("retained") is not None


@pytest.mark.asyncio
async def test_disconnect_waits_for_active_operation_and_blocks_new_work(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingGmail(Gmail):
        def get_full_message(self, message_id: str) -> dict[str, Any]:
            started.set()
            assert release.wait(timeout=2)
            return super().get_full_message(message_id)

    gmail = BlockingGmail()
    gmail.messages["preview"] = {
        "id": "preview",
        "threadId": "thread",
        "internalDate": "500",
        "sender": "sender@example.com",
        "labelIds": ["INBOX"],
    }
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)
    preview_task = asyncio.create_task(service.get_message_preview("preview"))
    assert await asyncio.to_thread(started.wait, 1)

    disconnect_task = asyncio.create_task(service.disconnect())
    await asyncio.sleep(0)
    assert gmail.revoke_calls == 0
    with pytest.raises(ServiceUnavailableError, match="service_disconnecting"):
        await service.get_message_preview("preview")

    release.set()
    await preview_task
    await disconnect_task

    assert gmail.revoke_calls == 1
    assert database.get_account()["email"] is None
    assert database.get_message("preview") is None


@pytest.mark.asyncio
async def test_service_can_retry_failed_jobs(tmp_path: Path) -> None:
    gmail = Gmail()
    decision = ClassificationResult(
        primary_category=Category.OTHER,
        source=DecisionSource.FALLBACK,
    )
    service, database = make_service(tmp_path, gmail, decision)
    database.enqueue_message("failed", "thread", MessageOrigin.LIVE)
    job = database.claim_job()
    assert job is not None
    database.fail_job(int(job["id"]), "failed", "gmail_transient")

    assert await service.retry_failed_jobs() == 1

    retried = database.claim_job()
    assert retried is not None
    assert retried["message_id"] == "failed"
    assert retried["attempts"] == 1
