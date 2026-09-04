from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mail_buddy.classification import HybridClassifier
from mail_buddy.config import Settings
from mail_buddy.content import ContentExtractor
from mail_buddy.contracts import (
    CATEGORY_LABELS,
    NEEDS_REVIEW_LABEL,
    TAXONOMY_VERSION,
    BackfillState,
    Category,
    ClassificationResult,
    DashboardStatus,
    DecisionSource,
    JobKind,
    MessageOrigin,
    MessageState,
    ReasonCode,
    RuleKind,
)
from mail_buddy.db import Database, utc_now
from mail_buddy.fine_tuning import readiness as lora_readiness
from mail_buddy.fine_tuning import schedule_state
from mail_buddy.gmail import (
    EXCLUDED_LABEL_IDS,
    GmailClient,
    GmailError,
    GmailErrorCode,
    HistoryExpiredError,
    MessageNotFoundError,
)
from mail_buddy.oauth import OAuthManager, OAuthTokenError
from mail_buddy.personalization import (
    TrainingExample,
    build_feature_text,
    serialize_artifact,
    train_and_evaluate,
)
from mail_buddy.security import SecretBox

_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_MAX_JOB_ATTEMPTS = 5
_COLLEGE_DOMAINS_SETTING = "college_domains"
_LAST_MAINTENANCE_SETTING = "last_maintenance_date"
_LEARNING_ENABLED_SETTING = "learning_enabled"
_TRAINING_INTERVAL_SETTING = "training_interval_days"
_TRAINING_HOUR_SETTING = "training_hour_local"
_LAST_TRAINING_SETTING = "last_training_at"
_TRASH_CATEGORIES = frozenset(
    {Category.PROMOTION_GENERAL, Category.SHOPPING_PROMOTION}
)


class ServiceUnavailableError(RuntimeError):
    pass


def _guard_operation(
    method: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    @wraps(method)
    async def guarded(self: MailBuddyService, *args: Any, **kwargs: Any) -> Any:
        async with self._operation_scope():
            return await method(self, *args, **kwargs)

    return guarded


class MailBuddyService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        gmail_client: GmailClient | Any | None = None,
        oauth_manager: OAuthManager | None = None,
        extractor: ContentExtractor | Any | None = None,
        classifier: HybridClassifier | Any | None = None,
        secret_box: SecretBox | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        encryption_key = settings.resolved_encryption_key
        if secret_box is None:
            if not encryption_key and settings.demo_mode:
                encryption_key = SecretBox.generate_key()
            if not encryption_key:
                raise RuntimeError("Mail-Buddy encryption key is required")
            secret_box = SecretBox(encryption_key)
        self.secret_box = secret_box
        self.oauth = oauth_manager or OAuthManager(
            settings,
            database,
            secret_box=self.secret_box,
        )
        self.extractor = extractor or ContentExtractor(settings)
        self.classifier = classifier or HybridClassifier(
            settings,
            database,
            self.secret_box,
        )
        self._gmail = gmail_client
        self._gmail_lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._disconnecting = False
        self._active_operations = 0
        self._operation_condition = asyncio.Condition()
        self._operation_depth: ContextVar[int] = ContextVar(
            f"mail_buddy_operation_depth_{id(self)}",
            default=0,
        )
        self._lifecycle_lock = asyncio.Lock()

    @asynccontextmanager
    async def _operation_scope(self) -> AsyncIterator[None]:
        depth = self._operation_depth.get()
        if depth:
            token = self._operation_depth.set(depth + 1)
            try:
                yield
            finally:
                self._operation_depth.reset(token)
            return

        async with self._operation_condition:
            if self._disconnecting:
                raise ServiceUnavailableError("service_disconnecting")
            self._active_operations += 1
        token = self._operation_depth.set(1)
        try:
            yield
        finally:
            self._operation_depth.reset(token)
            async with self._operation_condition:
                self._active_operations -= 1
                self._operation_condition.notify_all()

    async def _call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(function, *args, **kwargs)

    async def _get_gmail(self, *, required: bool = True) -> Any | None:
        if self._disconnecting and self._operation_depth.get() == 0:
            raise ServiceUnavailableError("service_disconnecting")
        if self._gmail is not None:
            return self._gmail
        async with self._gmail_lock:
            if self._gmail is None:
                try:
                    self._gmail = await self._call(self.oauth.gmail_client)
                except OAuthTokenError:
                    self.database.set_account_error(GmailErrorCode.AUTH_REQUIRED.value)
                    if required:
                        raise ServiceUnavailableError(GmailErrorCode.AUTH_REQUIRED.value) from None
            if self._gmail is None and required:
                raise ServiceUnavailableError("gmail_not_connected")
        return self._gmail

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_unlocked()

    async def _start_unlocked(self) -> None:
        if self._started:
            return
        if self._disconnecting:
            raise ServiceUnavailableError("service_disconnecting")
        self.settings.ensure_directories()
        self.database.initialize()
        reset_count = self.database.reset_stale_jobs()
        if reset_count:
            self.database.add_event(
                "warning",
                "stale_jobs_reset",
                {"count": reset_count},
            )
        self._started = True
        if self.settings.disable_worker:
            return
        gmail = await self._get_gmail(required=False)
        if gmail is not None:
            try:
                await self.reconcile_labels()
            except GmailError as error:
                self.database.set_account_error(error.code.value)
        self._stop_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._worker_loop(), name="mail-buddy-worker"),
            asyncio.create_task(self._poll_loop(), name="mail-buddy-poller"),
            asyncio.create_task(
                self._maintenance_loop(),
                name="mail-buddy-maintenance",
            ),
            asyncio.create_task(
                self._content_sync_loop(),
                name="mail-buddy-content-sync",
            ),
        ]

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        if not self._started:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._stop_event = None
        self._started = False

    @_guard_operation
    async def reconcile_labels(self) -> dict[str, str]:
        gmail = await self._get_gmail()
        labels = await self._call(gmail.ensure_labels)
        for category, label_name in CATEGORY_LABELS.items():
            self.database.upsert_label(
                category.value,
                label_name,
                labels[category.value],
            )
        self.database.upsert_label(
            "needs_review",
            NEEDS_REVIEW_LABEL,
            labels["needs_review"],
        )
        return labels

    async def get_status(self) -> DashboardStatus:
        account = self.database.get_account()
        counts = self.database.get_counts()
        backfill = self.database.get_backfill()
        content_sync = self.database.get_content_sync_progress()
        model_available = await self._model_available()
        active_main = self.database.get_active_main_model()
        disk_path = self.settings.data_dir
        if not disk_path.exists():
            disk_path = disk_path.parent if disk_path.parent.exists() else Path("/")
        try:
            disk_free = shutil.disk_usage(disk_path).free
        except OSError:
            disk_free = 0
        try:
            backfill_state = BackfillState(str(backfill.get("status", BackfillState.IDLE.value)))
        except ValueError:
            backfill_state = BackfillState.ERROR
        return DashboardStatus(
            connected=bool(account.get("email")),
            account_email=account.get("email"),
            account_status=str(account.get("status", "disconnected")),
            last_sync_at=account.get("last_sync_at"),
            history_id=account.get("history_id"),
            model_available=model_available,
            model_name=str(active_main["name"]) if active_main else self.settings.ollama_model,
            queue_depth=counts.get("queue", 0),
            review_count=counts.get(MessageState.NEEDS_REVIEW.value, 0),
            staged_count=counts.get(MessageState.STAGED.value, 0),
            applied_count=counts.get(MessageState.APPLIED.value, 0),
            backfill_status=backfill_state,
            backfill_scanned=int(backfill.get("total_scanned", 0)),
            backfill_staged=int(backfill.get("total_staged", 0)),
            content_sync_total=int(content_sync.get("total", 0)),
            content_sync_cached=int(content_sync.get("cached", 0)),
            content_sync_last_at=content_sync.get("last_cached_at"),
            disk_free_bytes=disk_free,
        )

    @_guard_operation
    async def get_message_preview(self, message_id: str) -> dict[str, str | int]:
        """Return an encrypted local preview, fetching Gmail only on a cache miss."""

        cached = self.database.get_cached_message_content(message_id)
        if cached is not None:
            try:
                body = self.secret_box.decrypt(str(cached["body_ciphertext"]))
                sender = self.secret_box.decrypt(str(cached["sender_ciphertext"]))
                subject = self.secret_box.decrypt(str(cached["subject_ciphertext"]))
                attachment_text = self.secret_box.decrypt(
                    str(cached["attachment_text_ciphertext"])
                )
            except ValueError:
                # A cache encrypted with an old key must never block access to
                # the email; Gmail remains the source of truth.
                cached = None
            else:
                return {
                    "message_id": message_id,
                    "sender": sender,
                    "subject": subject,
                    "body": body,
                    "attachment_text": attachment_text,
                    "content": body,
                    "internal_date": int(cached["internal_date"]),
                }

        gmail = await self._get_gmail()
        parsed = await self._fetch_and_cache_message(gmail, message_id)
        metadata = parsed.metadata
        return {
            "message_id": metadata.message_id,
            "sender": metadata.sender,
            "subject": metadata.subject,
            "body": parsed.body_text,
            "attachment_text": parsed.attachment_text,
            # Retain this field for dashboard clients from earlier releases.
            "content": parsed.body_text,
            "internal_date": metadata.internal_date,
        }

    async def _fetch_and_cache_message(
        self,
        gmail: GmailClient | Any,
        message_id: str,
        *,
        two_way_history: bool = False,
    ) -> Any:
        full_message = await self._call(gmail.get_full_message, message_id)

        async def attachment_loader(
            attachment_message_id: str,
            attachment_id: str,
        ) -> bytes:
            return await self._call(
                gmail.get_attachment,
                attachment_message_id,
                attachment_id,
            )

        parsed = await self.extractor.parse_full(
            full_message,
            attachment_loader,
            two_way_history=two_way_history,
        )
        metadata = parsed.metadata
        self.database.cache_message_content(
            message_id=metadata.message_id,
            sender_ciphertext=self.secret_box.encrypt(metadata.sender),
            subject_ciphertext=self.secret_box.encrypt(metadata.subject),
            body_ciphertext=self.secret_box.encrypt(parsed.body_text),
            attachment_text_ciphertext=self.secret_box.encrypt(parsed.attachment_text),
            internal_date=metadata.internal_date,
        )
        return parsed

    async def _model_available(self) -> bool:
        health = getattr(self.classifier, "health", None)
        target = getattr(self.classifier, "ollama_client", None)
        if target is None:
            target = getattr(self.classifier, "ollama", None)
        if health is None:
            health = getattr(target, "health", None)
        if health is None:
            return False
        try:
            result = health()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=5)
            return bool(result)
        except (TimeoutError, OSError, RuntimeError, ValueError):
            return False

    def _college_domains(self) -> set[str]:
        persisted = self.database.get_setting(_COLLEGE_DOMAINS_SETTING)
        if persisted is None:
            return self.settings.college_domain_set
        return {item for item in persisted.split(",") if item}

    @_guard_operation
    async def update_college_domains(self, domains: set[str]) -> None:
        normalized = {domain.strip().lower().lstrip("@") for domain in domains if domain.strip()}
        invalid = sorted(domain for domain in normalized if not _DOMAIN_PATTERN.fullmatch(domain))
        if invalid:
            raise ValueError("One or more college domains are invalid")
        serialized = ",".join(sorted(normalized))
        self.database.set_setting(_COLLEGE_DOMAINS_SETTING, serialized)
        self.settings.college_domains = serialized

    @_guard_operation
    async def delete_rule(self, rule_id: int) -> None:
        self.database.delete_rule(rule_id)

    @_guard_operation
    async def start_backfill(self) -> None:
        gmail = await self._get_gmail()
        await self.reconcile_labels()
        profile = await self._call(gmail.get_profile)
        history_id = str(profile.get("historyId", ""))
        if not history_id:
            raise ServiceUnavailableError(GmailErrorCode.INVALID_RESPONSE.value)
        self.database.start_backfill(history_id)
        account = self.database.get_account()
        email = str(profile.get("emailAddress") or account.get("email") or "")
        if email:
            self.database.connect_account(email, history_id)
        else:
            self.database.update_history(history_id)
        self.database.add_event("info", "backfill_started")

    @_guard_operation
    async def pause_backfill(self) -> None:
        backfill = self.database.get_backfill()
        if backfill.get("status") == BackfillState.RUNNING.value:
            self.database.set_backfill_status(BackfillState.PAUSED)
            self.database.add_event("info", "backfill_paused")

    @_guard_operation
    async def resume_backfill(self) -> None:
        backfill = self.database.get_backfill()
        if backfill.get("status") not in {
            BackfillState.PAUSED.value,
            BackfillState.ERROR.value,
        }:
            return
        self.database.set_backfill_status(BackfillState.RUNNING)
        self.database.add_event("info", "backfill_resumed")

    @_guard_operation
    async def scan_backfill_once(self) -> bool:
        backfill = self.database.get_backfill()
        if backfill.get("status") != BackfillState.RUNNING.value:
            return False
        gmail = await self._get_gmail()
        messages, next_token = await self._call(
            gmail.list_received_page,
            page_token=backfill.get("page_token"),
            max_results=self.settings.backfill_page_size,
        )
        for message in messages:
            message_id = str(message.get("id", ""))
            if not message_id:
                continue
            self.database.enqueue_message(
                message_id,
                str(message.get("threadId") or message_id),
                MessageOrigin.BACKFILL,
                internal_date=_to_int(message.get("internalDate")),
            )
        self.database.update_backfill_page(next_token, len(messages))
        if not next_token:
            self.database.add_event(
                "info",
                "backfill_scan_complete",
                {"scanned": len(messages)},
            )
        return True

    @_guard_operation
    async def poll_history_once(self) -> int:
        gmail = await self._get_gmail()
        account = self.database.get_account()
        history_id = str(account.get("history_id") or "")
        if not history_id:
            profile = await self._call(gmail.get_profile)
            history_id = str(profile.get("historyId", ""))
            email = str(profile.get("emailAddress", ""))
            if not history_id or not email:
                raise ServiceUnavailableError(GmailErrorCode.INVALID_RESPONSE.value)
            self.database.connect_account(email, history_id)
            return 0
        try:
            messages, newest_history_id = await self._call(
                gmail.list_history_added,
                history_id,
            )
        except HistoryExpiredError:
            await self._recover_expired_history(gmail)
            return 0
        enqueued = 0
        for message in messages:
            label_ids = GmailClient.label_ids(message)
            if label_ids & EXCLUDED_LABEL_IDS:
                continue
            message_id = str(message.get("id", ""))
            if not message_id:
                continue
            if self.database.enqueue_message(
                message_id,
                str(message.get("threadId") or message_id),
                MessageOrigin.LIVE,
                internal_date=_to_int(message.get("internalDate")),
            ):
                enqueued += 1
        self.database.update_history(newest_history_id)
        return enqueued

    async def _recover_expired_history(self, gmail: Any) -> None:
        profile = await self._call(gmail.get_profile)
        current_history = str(profile.get("historyId", ""))
        if not current_history:
            raise ServiceUnavailableError(GmailErrorCode.INVALID_RESPONSE.value)
        self.database.recover_expired_history(current_history)
        self.database.add_event("warning", "gmail_history_expired")

    @_guard_operation
    async def process_one_job(self) -> bool:
        job = self.database.claim_job()
        if job is None:
            self._maybe_finish_backfill()
            return False
        job_id = int(job["id"])
        message_id = str(job["message_id"])
        try:
            kind = JobKind(str(job["kind"]))
            if kind == JobKind.CLASSIFY:
                await self._classify_message(message_id)
            elif kind == JobKind.APPLY:
                await self._apply_message(message_id)
            else:
                raise ValueError("Unsupported durable job kind")
        except MessageNotFoundError:
            self.database.mark_gone(message_id)
            self.database.complete_job(job_id)
        except GmailError as error:
            if error.retryable and int(job["attempts"]) < _MAX_JOB_ATTEMPTS:
                self.database.retry_job(
                    job_id,
                    error.code.value,
                    min(300, 2 ** int(job["attempts"])),
                )
            else:
                self.database.fail_job(job_id, message_id, error.code.value)
                if error.code in {
                    GmailErrorCode.AUTH_REQUIRED,
                    GmailErrorCode.PERMISSION_DENIED,
                }:
                    self.database.set_account_error(error.code.value)
        except (TimeoutError, ConnectionError, OSError):
            if int(job["attempts"]) < _MAX_JOB_ATTEMPTS:
                self.database.retry_job(
                    job_id,
                    "worker_transient",
                    min(300, 2 ** int(job["attempts"])),
                )
            else:
                self.database.fail_job(job_id, message_id, "worker_transient")
        except Exception:
            if int(job["attempts"]) < 3:
                self.database.retry_job(
                    job_id,
                    "classification_failed",
                    2 ** int(job["attempts"]),
                )
            else:
                self.database.fail_job(
                    job_id,
                    message_id,
                    "classification_failed",
                )
        else:
            self.database.complete_job(job_id)
        self._maybe_finish_backfill()
        return True

    async def _classify_message(self, message_id: str) -> None:
        gmail = await self._get_gmail()
        raw_metadata = await self._call(gmail.get_metadata, message_id)
        metadata = self.extractor.parse_metadata(raw_metadata)
        if metadata.label_ids & EXCLUDED_LABEL_IDS:
            self.database.mark_gone(message_id)
            return

        sender_key = self.secret_box.fingerprint(metadata.sender)
        two_way = self.database.get_correspondent(sender_key)
        if two_way is None:
            two_way = await self._call(gmail.has_sent_to, metadata.sender)
            self.database.set_correspondent(sender_key, two_way)
        metadata.two_way_history = two_way
        domains = self._college_domains()
        decision = self.classifier.classify_metadata(
            metadata,
            college_domains=domains,
        )
        # Cache every processed message, including ones confidently handled by
        # deterministic rules, so dashboard previews are immediate later.
        parsed = await self._fetch_and_cache_message(
            gmail,
            message_id,
            two_way_history=two_way,
        )
        if decision is None:
            decision = await self.classifier.classify(
                parsed,
                college_domains=domains,
            )

        row = self.database.get_message(message_id)
        if row is None:
            raise MessageNotFoundError()
        # A correction may arrive while a slower model request is in flight.
        # Manual intent wins and must not be overwritten by that stale result.
        if row.get("decision_source") == DecisionSource.MANUAL.value:
            return
        origin = MessageOrigin(str(row["origin"]))
        if origin == MessageOrigin.BACKFILL:
            self.database.save_decision(
                message_id,
                decision,
                sender_key=sender_key,
                internal_date=metadata.internal_date,
                had_inbox=metadata.had_inbox,
                state=(
                    MessageState.NEEDS_REVIEW if decision.review_required else MessageState.STAGED
                ),
            )
            return

        if decision.review_required:
            self.database.save_decision(
                message_id,
                decision,
                sender_key=sender_key,
                internal_date=metadata.internal_date,
                had_inbox=metadata.had_inbox,
                state=MessageState.NEEDS_REVIEW,
            )
            await self._apply_needs_review(message_id, metadata.label_ids)
            return

        approval_model = decision.model or self.settings.ollama_model
        approved = self.database.is_category_approved(
            decision.primary_category,
            decision.taxonomy_version,
            approval_model,
        )
        state = MessageState.READY_TO_APPLY if approved else MessageState.STAGED
        self.database.save_decision(
            message_id,
            decision,
            sender_key=sender_key,
            internal_date=metadata.internal_date,
            had_inbox=metadata.had_inbox,
            state=state,
        )
        if approved:
            self.database.enqueue_apply(message_id)

    async def _apply_needs_review(
        self,
        message_id: str,
        current_label_ids: set[str],
    ) -> None:
        gmail = await self._get_gmail()
        labels = await self._label_map()
        review_id = labels["needs_review"]
        app_ids = set(labels.values()) | set(self.database.get_label_aliases())
        before_app = sorted(current_label_ids & app_ids)
        add = {review_id} - current_label_ids
        remove = ((current_label_ids & app_ids) - {review_id}) | (
            {"INBOX"} if "INBOX" in current_label_ids else set()
        )
        pending_key = f"pending_batch:message:{message_id}"
        batch_id = self.database.get_setting(pending_key)
        if not batch_id:
            batch_id = f"live-review:{uuid4()}"
            self.database.set_setting(pending_key, batch_id)
        if add or remove:
            self.database.create_audit(
                batch_id=batch_id,
                message_id=message_id,
                action="needs_review",
                before_app_label_ids=before_app,
                before_had_inbox="INBOX" in current_label_ids,
                after_category=None,
            )
            await self._call(
                gmail.modify_message_labels,
                message_id,
                add_label_ids=add,
                remove_label_ids=remove,
            )
        self.database.mark_needs_review(message_id, review_id)
        self.database.delete_setting(pending_key)

    async def _apply_message(self, message_id: str) -> None:
        gmail = await self._get_gmail()
        row = self.database.get_message(message_id)
        if row is None:
            raise MessageNotFoundError()
        category_value = row.get("primary_category")
        if not category_value:
            raise ValueError("Message has no classification")
        category = Category(str(category_value))
        labels = await self._label_map()
        category_label_id = labels[category.value]
        raw_metadata = await self._call(gmail.get_metadata, message_id)
        current_labels = GmailClient.label_ids(raw_metadata)
        app_ids = set(labels.values()) | set(self.database.get_label_aliases())
        before_app = sorted(current_labels & app_ids)
        if category in _TRASH_CATEGORIES:
            pending_key = f"pending_batch:message:{message_id}"
            batch_id = self.database.get_setting(pending_key)
            if not batch_id and row.get("origin") != MessageOrigin.LIVE.value:
                batch_id = self.database.get_setting(f"pending_batch:{category.value}")
            if not batch_id:
                batch_id = f"live-trash:{uuid4()}"
                self.database.set_setting(pending_key, batch_id)
            self.database.create_audit(
                batch_id=batch_id,
                message_id=message_id,
                action="trash_promotion",
                before_app_label_ids=before_app,
                before_had_inbox="INBOX" in current_labels,
                after_category=category,
            )
            await self._call(gmail.trash_message, message_id)
            self.database.mark_applied(message_id, category_label_id, category)
            self.database.delete_setting(pending_key)
            return
        add = {category_label_id} - current_labels
        remove = ((current_labels & app_ids) - {category_label_id}) | (
            {"INBOX"} if "INBOX" in current_labels else set()
        )
        if add or remove:
            pending_key = f"pending_batch:message:{message_id}"
            batch_id = self.database.get_setting(pending_key)
            if not batch_id and row.get("origin") != MessageOrigin.LIVE.value:
                batch_id = self.database.get_setting(f"pending_batch:{category.value}")
            if not batch_id:
                batch_id = f"live-apply:{uuid4()}"
                self.database.set_setting(pending_key, batch_id)
            self.database.create_audit(
                batch_id=batch_id,
                message_id=message_id,
                action="apply",
                before_app_label_ids=before_app,
                before_had_inbox="INBOX" in current_labels,
                after_category=category,
            )
            await self._call(
                gmail.modify_message_labels,
                message_id,
                add_label_ids=add,
                remove_label_ids=remove,
            )
        self.database.mark_applied(message_id, category_label_id, category)
        self.database.delete_setting(f"pending_batch:message:{message_id}")

    async def _label_map(self) -> dict[str, str]:
        labels = self.database.get_labels()
        expected = {category.value for category in Category} | {"needs_review"}
        if not expected.issubset(labels):
            labels = await self.reconcile_labels()
        return labels

    @_guard_operation
    async def approve_category(self, category: Category) -> str:
        await self._label_map()
        batch_id = self.database.approve_category(
            category,
            TAXONOMY_VERSION,
            self.settings.ollama_model,
        )
        self.database.add_event(
            "info",
            "category_approved",
            {"category": category.value, "batch_id": batch_id},
        )
        return batch_id

    @_guard_operation
    async def correct_message(
        self,
        message_id: str,
        category: Category,
        scope: str = "message",
        rule_kind: RuleKind | None = None,
        rule_pattern: str | None = None,
    ) -> str | None:
        gmail = await self._get_gmail()
        raw_metadata = await self._call(gmail.get_metadata, message_id)
        metadata = self.extractor.parse_metadata(raw_metadata)
        row = self.database.get_message(message_id)
        predicted_category: Category | None = None
        if row and row.get("primary_category"):
            try:
                predicted_category = Category(str(row["primary_category"]))
            except ValueError:
                predicted_category = None
        if row is None:
            self.database.enqueue_message(
                message_id,
                metadata.thread_id,
                MessageOrigin.LIVE,
                internal_date=metadata.internal_date,
            )
        decision = ClassificationResult(
            primary_category=category,
            source=DecisionSource.MANUAL,
            review_required=False,
            reason_codes=[ReasonCode.USER_OVERRIDE],
            model=None,
        )
        self.database.save_decision(
            message_id,
            decision,
            sender_key=self.secret_box.fingerprint(metadata.sender),
            internal_date=metadata.internal_date,
            had_inbox=metadata.had_inbox,
            state=MessageState.READY_TO_APPLY,
        )
        self._save_training_example(
            metadata,
            category,
            predicted_category=predicted_category,
            source="needs_review" if row and bool(row.get("review_required")) else "correction",
        )
        if scope != "message":
            kind, pattern = self._correction_rule(
                scope,
                rule_kind,
                rule_pattern,
                metadata.sender,
                metadata.sender_domain,
                metadata.subject,
            )
            self.database.create_rule(
                kind,
                self.secret_box.encrypt(pattern),
                category,
            )
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'done', updated_at = ?
                WHERE message_id = ?
                  AND kind = ?
                  AND status IN ('pending', 'failed')
                """,
                (utc_now(), message_id, JobKind.CLASSIFY.value),
            )
        batch_id = f"correction:{uuid4()}"
        self.database.set_setting(
            f"pending_batch:message:{message_id}",
            batch_id,
        )
        self.database.enqueue_apply(message_id, priority=5)
        return batch_id

    def _save_training_example(
        self,
        metadata: Any,
        category: Category,
        *,
        predicted_category: Category | None,
        source: str,
    ) -> None:
        content = build_feature_text(metadata)
        self.database.upsert_training_example(
            message_id=metadata.message_id,
            sender_key=self.secret_box.fingerprint(metadata.sender),
            content_ciphertext=self.secret_box.encrypt(content),
            category=category,
            predicted_category=predicted_category,
            source=source,
        )

    @_guard_operation
    async def submit_accuracy_label(self, message_id: str, category: Category) -> str | None:
        """Use an owner's accuracy answer as ground truth and correct Gmail."""

        batch_id = await self.correct_message(message_id, category, scope="message")
        row = self.database.get_message(message_id)
        if row is not None:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE training_examples SET source = ?, updated_at = ? WHERE message_id = ?",
                    ("accuracy_review", utc_now(), message_id),
                )
        return batch_id

    def _learning_setting(self, key: str, default: str) -> str:
        value = self.database.get_setting(key)
        return value if value is not None else default

    def get_learning_status(self) -> dict[str, Any]:
        examples = self.database.list_training_examples()
        active = self.database.get_active_personalized_model()
        active_main = self.database.get_active_main_model()
        category_counts: dict[str, int] = {}
        for example in examples:
            category = str(example["category"])
            category_counts[category] = category_counts.get(category, 0) + 1
        companion_run = self.database.get_latest_training_run("companion")
        lora_run = self.database.get_latest_training_run("lora")
        interval_days = int(
            self._learning_setting(
                _TRAINING_INTERVAL_SETTING,
                str(self.settings.training_interval_days),
            )
        )
        hour_local = int(
            self._learning_setting(
                _TRAINING_HOUR_SETTING,
                str(self.settings.training_hour_local),
            )
        )
        schedule = schedule_state(
            now=datetime.now(UTC),
            last_finished_at=(
                str(lora_run["finished_at"])
                if lora_run and lora_run.get("finished_at")
                else None
            ),
            interval_days=interval_days,
            hour_local=hour_local,
            timezone=os.environ.get("TZ", "UTC"),
        )
        lora_state = lora_readiness(examples)

        running = next(
            (
                run
                for run in (companion_run, lora_run)
                if run and run.get("status") == "running"
            ),
            None,
        )
        latest_failed = next(
            (
                run
                for run in (lora_run, companion_run)
                if run and run.get("status") == "failed"
            ),
            None,
        )
        failure_reason = None
        if latest_failed:
            try:
                failure_reason = json.loads(str(latest_failed.get("details") or "{}")).get(
                    "reason"
                )
            except (json.JSONDecodeError, TypeError):
                failure_reason = "training_failed"

        def public_model(row: dict[str, Any]) -> dict[str, Any]:
            return {
                key: row.get(key)
                for key in (
                    "id",
                    "name",
                    "example_count",
                    "evaluated_count",
                    "accuracy",
                    "macro_f1",
                    "status",
                    "created_at",
                    "promoted_at",
                )
            }

        return {
            "enabled": self._learning_setting(
                _LEARNING_ENABLED_SETTING,
                str(self.settings.personalization_enabled).lower(),
            )
            == "true",
            "interval_days": interval_days,
            "hour_local": hour_local,
            "timezone": schedule["timezone"],
            "next_eligible_at": schedule["next_eligible_at"],
            "lora_due": schedule["due"],
            "current_phase": (
                f"{running['kind']}_training" if running else "ready" if examples else "collecting"
            ),
            "failure_reason": failure_reason,
            "last_training_at": self.database.get_setting(_LAST_TRAINING_SETTING),
            "example_count": len(examples),
            "answer_count": len(examples),
            "candidate_count": len(self.database.list_accuracy_candidates(limit=100)),
            "category_counts": category_counts,
            "companion_ready": len(examples) >= self.settings.training_min_examples,
            "lora_ready": bool(lora_state["ready"]),
            "lora_split_ready": bool(lora_state["split_ready"]),
            "active_model": public_model(active) if active else None,
            "active_main_model": public_model(active_main) if active_main else None,
            "models": [
                public_model(row) for row in self.database.list_personalized_models(limit=10)
            ],
            "main_models": [public_model(row) for row in self.database.list_main_models(limit=10)],
            "companion_run": companion_run,
            "lora_run": lora_run,
        }

    @_guard_operation
    async def update_learning_schedule(
        self,
        *,
        enabled: bool,
        interval_days: int,
        hour_local: int,
    ) -> None:
        if interval_days not in {1, 3, 7, 14, 30} or not 0 <= hour_local <= 23:
            raise ValueError("Invalid learning schedule")
        self.database.set_setting(_LEARNING_ENABLED_SETTING, str(enabled).lower())
        self.database.set_setting(_TRAINING_INTERVAL_SETTING, str(interval_days))
        self.database.set_setting(_TRAINING_HOUR_SETTING, str(hour_local))
        self.database.add_event("info", "learning_schedule_updated")

    @_guard_operation
    async def train_personalized_model(self, *, force: bool = False) -> bool:
        enabled = (
            self._learning_setting(
                _LEARNING_ENABLED_SETTING,
                str(self.settings.personalization_enabled).lower(),
            )
            == "true"
        )
        if not enabled and not force:
            return False
        now = datetime.now(UTC)
        try:
            local_now = now.astimezone(ZoneInfo(os.environ.get("TZ", "UTC")))
        except ZoneInfoNotFoundError:
            local_now = now
        if not force:
            interval = int(
                self._learning_setting(
                    _TRAINING_INTERVAL_SETTING,
                    str(self.settings.training_interval_days),
                )
            )
            hour = int(
                self._learning_setting(
                    _TRAINING_HOUR_SETTING,
                    str(self.settings.training_hour_local),
                )
            )
            last_value = self.database.get_setting(_LAST_TRAINING_SETTING)
            if local_now.hour < hour:
                return False
            if last_value:
                try:
                    last = datetime.fromisoformat(last_value)
                    if now - last < timedelta(days=interval):
                        return False
                except ValueError:
                    pass
        rows = self.database.list_training_examples()
        if len(rows) < self.settings.training_min_examples:
            if force:
                self.database.add_event(
                    "warning", "training_waiting_for_examples", {"count": len(rows)}
                )
            return False
        examples: list[TrainingExample] = []
        for row in rows:
            try:
                examples.append(
                    TrainingExample(
                        message_id=str(row["message_id"]),
                        sender_key=str(row["sender_key"]),
                        text=self.secret_box.decrypt(str(row["content_ciphertext"])),
                        category=Category(str(row["category"])),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        dataset_version = int(now.timestamp())
        run_id = self.database.acquire_training_run("companion", dataset_version=dataset_version)
        if run_id is None:
            return False
        try:
            result = await self._call(train_and_evaluate, examples)
        except Exception:
            self.database.finish_training_run(
                run_id,
                "failed",
                {"reason": "training_failed"},
            )
            raise
        self.database.update_training_folds(result.folds, dataset_version)
        active = self.database.get_active_personalized_model()
        active_accuracy = float(active["accuracy"]) if active else 0.0
        active_macro_f1 = float(active["macro_f1"]) if active else 0.0
        promote = (
            len(examples) >= self.settings.training_min_examples
            and result.evaluated_count >= self.settings.training_min_evaluated
            and result.accuracy >= self.settings.training_min_accuracy
            and result.accuracy >= active_accuracy
            and result.macro_f1 >= active_macro_f1
        )
        name = f"mail-buddy-personal-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:6]}"
        self.database.create_personalized_model(
            name=name,
            artifact_ciphertext=self.secret_box.encrypt(serialize_artifact(result.artifact)),
            example_count=len(examples),
            evaluated_count=result.evaluated_count,
            accuracy=result.accuracy,
            macro_f1=result.macro_f1,
            category_recall=result.category_recall,
            promote=promote,
        )
        self.database.set_setting(_LAST_TRAINING_SETTING, now.isoformat())
        self.database.add_event(
            "info" if promote else "warning",
            "personalized_model_promoted" if promote else "personalized_model_rejected",
            {"count": len(examples)},
        )
        self.database.finish_training_run(
            run_id,
            "complete" if promote else "rejected",
            {
                "count": len(examples),
                "accuracy": round(result.accuracy, 4),
                "macro_f1": round(result.macro_f1, 4),
            },
        )
        return promote

    @_guard_operation
    async def rollback_main_model(self) -> str:
        name = self.database.rollback_main_model()
        self.database.add_event("warning", "main_model_rolled_back")
        return name

    def _correction_rule(
        self,
        scope: str,
        rule_kind: RuleKind | None,
        rule_pattern: str | None,
        sender: str,
        sender_domain: str,
        subject: str,
    ) -> tuple[RuleKind, str]:
        aliases = {
            "sender": RuleKind.SENDER,
            "sender_domain": RuleKind.SENDER_DOMAIN,
            "similar": RuleKind.SIMILAR,
            "future_similar": RuleKind.SIMILAR,
        }
        kind = rule_kind or aliases.get(scope)
        if kind is None:
            raise ValueError("Unsupported correction scope")
        pattern = (rule_pattern or "").strip()
        if not pattern and kind == RuleKind.SENDER:
            pattern = sender.strip().lower()
        elif not pattern and kind == RuleKind.SENDER_DOMAIN:
            pattern = sender_domain.strip().lower()
        elif not pattern and kind == RuleKind.SIMILAR:
            keywords: list[str] = []
            for word in re.findall(r"[a-z]{4,}", subject.lower()):
                if word not in keywords:
                    keywords.append(word)
                if len(keywords) == 4:
                    break
            specification: dict[str, Any] = {}
            if sender_domain:
                specification["sender_domain"] = sender_domain.strip().lower()
            if keywords:
                specification["subject_contains"] = keywords
            elif sender:
                specification["sender"] = sender.strip().lower()
            pattern = json.dumps(specification, separators=(",", ":"))
        if not pattern:
            raise ValueError("A rule pattern is required for this correction scope")
        return kind, pattern

    @_guard_operation
    async def undo_batch(self, batch_id: str) -> None:
        gmail = await self._get_gmail()
        rows = self.database.list_audit_batch(batch_id)
        labels = await self._label_map()
        app_ids = set(labels.values())
        aliases = self.database.get_label_aliases()
        known_app_ids = app_ids | set(aliases)
        reverse_labels = {
            label_id: Category(category)
            for category, label_id in labels.items()
            if category != "needs_review"
        }
        for audit in rows:
            message_id = str(audit["message_id"])
            try:
                if audit["action"] == "trash_promotion":
                    await self._call(gmail.untrash_message, message_id)
                raw_metadata = await self._call(gmail.get_metadata, message_id)
            except MessageNotFoundError:
                self.database.mark_audit_undone(int(audit["id"]))
                self.database.mark_gone(message_id)
                continue
            current = GmailClient.label_ids(raw_metadata)
            recorded_before = {str(item) for item in json.loads(audit["before_app_label_ids"])}
            before: set[str] = set()
            for label_id in recorded_before:
                category = aliases.get(label_id)
                if category in labels:
                    before.add(labels[category])
                elif label_id in app_ids:
                    before.add(label_id)
            add = before - current
            remove = (current & known_app_ids) - before
            if bool(audit["before_had_inbox"]):
                add.add("INBOX")
                remove.discard("INBOX")
            elif "INBOX" in current:
                remove.add("INBOX")
            if add or remove:
                await self._call(
                    gmail.modify_message_labels,
                    message_id,
                    add_label_ids=add,
                    remove_label_ids=remove,
                )
            self._reflect_undo_state(message_id, before, reverse_labels, labels)
            self.database.mark_audit_undone(int(audit["id"]))
        self.database.add_event(
            "info",
            "batch_undone",
            {"batch_id": batch_id, "count": len(rows)},
        )

    def _reflect_undo_state(
        self,
        message_id: str,
        before_labels: set[str],
        reverse_labels: dict[str, Category],
        labels: dict[str, str],
    ) -> None:
        category = next(
            (reverse_labels[label_id] for label_id in before_labels if label_id in reverse_labels),
            None,
        )
        if category is not None:
            self.database.mark_applied(
                message_id,
                labels[category.value],
                category,
            )
        elif labels["needs_review"] in before_labels:
            self.database.mark_needs_review(
                message_id,
                labels["needs_review"],
            )
        else:
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE messages SET state = ?, current_app_label_id = NULL,
                        processed_at = NULL, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (MessageState.STAGED.value, utc_now(), message_id),
                )

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            if self._disconnecting:
                raise ServiceUnavailableError("service_disconnecting")
            was_started = self._started
            async with self._operation_condition:
                self._disconnecting = True
                self._operation_condition.notify_all()

            await self._stop_unlocked()
            async with self._operation_condition:
                await self._operation_condition.wait_for(lambda: self._active_operations == 0)

            gmail = self._gmail
            if gmail is None and self.database.get_setting("oauth_token"):
                try:
                    gmail = await self._call(self.oauth.gmail_client)
                except OAuthTokenError as error:
                    await self._restore_after_failed_disconnect(was_started)
                    raise ServiceUnavailableError("oauth_revoke_failed") from error

            if gmail is not None:
                try:
                    await self._call(gmail.revoke)
                except Exception as error:
                    self.database.add_event("error", "oauth_revoke_failed")
                    await self._restore_after_failed_disconnect(was_started)
                    raise ServiceUnavailableError("oauth_revoke_failed") from error

            self.database.purge_account_data()
            self._gmail = None
            async with self._operation_condition:
                self._disconnecting = False
                self._operation_condition.notify_all()
            if was_started:
                await self._start_unlocked()

    async def _restore_after_failed_disconnect(self, was_started: bool) -> None:
        async with self._operation_condition:
            self._disconnecting = False
            self._operation_condition.notify_all()
        if was_started:
            await self._start_unlocked()

    @_guard_operation
    async def retry_failed_jobs(self) -> int:
        count = self.database.retry_failed_jobs()
        if count:
            self.database.add_event(
                "info",
                "failed_jobs_retried",
                {"count": count},
            )
        return count

    def _maybe_finish_backfill(self) -> None:
        backfill = self.database.get_backfill()
        if backfill.get("status") != BackfillState.SCAN_COMPLETE.value:
            return
        with self.database.connect() as connection:
            pending = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs AS j JOIN messages AS m ON m.message_id = j.message_id
                WHERE j.kind = ? AND j.status IN ('pending', 'running')
                    AND m.origin = ?
                """,
                (JobKind.CLASSIFY.value, MessageOrigin.BACKFILL.value),
            ).fetchone()
        if not pending or int(pending["count"]) == 0:
            self.database.set_backfill_status(BackfillState.COMPLETE)
            self.database.add_event("info", "backfill_complete")

    @_guard_operation
    async def maintenance_once(self) -> bool:
        today = datetime.now(UTC).date().isoformat()
        if self.database.get_setting(_LAST_MAINTENANCE_SETTING) == today:
            return False
        await self._call(self.database.backup, self.settings.backup_dir, 7)
        self.database.cleanup()
        self.database.set_setting(_LAST_MAINTENANCE_SETTING, today)
        self.database.add_event("info", "maintenance_complete")
        return True

    async def _worker_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            did_work = await self.process_one_job()
            if not did_work:
                try:
                    did_work = await self.scan_backfill_once()
                except GmailError as error:
                    self.database.set_backfill_status(
                        BackfillState.ERROR,
                        error_code=error.code.value,
                    )
            if not did_work:
                await self._wait_or_stop(self.settings.worker_idle_seconds)

    async def _poll_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.poll_history_once()
            except ServiceUnavailableError:
                pass
            except GmailError as error:
                self.database.set_account_error(error.code.value)
            await self._wait_or_stop(self.settings.poll_interval_seconds)

    async def _maintenance_loop(self) -> None:
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                await self.maintenance_once()
            except OSError:
                self.database.add_event("error", "maintenance_failed")
            try:
                await self.train_personalized_model()
            except (OSError, RuntimeError, ValueError):
                self.database.add_event("error", "personalized_training_failed")
            await self._wait_or_stop(3600)

    async def _content_sync_loop(self) -> None:
        """Backfill encrypted preview content without delaying mail classification."""

        while self._stop_event is not None and not self._stop_event.is_set():
            candidate = self.database.next_message_without_cached_content()
            if candidate is None:
                await self._wait_or_stop(30)
                continue
            try:
                gmail = await self._get_gmail()
                await self._fetch_and_cache_message(gmail, str(candidate["message_id"]))
            except (GmailError, ServiceUnavailableError, OSError, ValueError) as error:
                self.database.add_event(
                    "warning",
                    "content_cache_sync_failed",
                    {"code": getattr(error, "code", "unavailable")},
                )
                await self._wait_or_stop(30)
                continue
            await self._wait_or_stop(0.2)

    async def _wait_or_stop(self, seconds: float) -> None:
        if self._stop_event is None:
            return
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
