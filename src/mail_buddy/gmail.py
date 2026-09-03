from __future__ import annotations

import base64
import json
import random
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
from httplib2 import HttpLib2Error

from mail_buddy.contracts import (
    CATEGORY_LABELS,
    LEGACY_CATEGORY_LABELS,
    LEGACY_NEEDS_REVIEW_LABEL,
    NEEDS_REVIEW_LABEL,
)

try:  # Keep pure unit tests importable before optional runtime dependencies install.
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:  # pragma: no cover - exercised only in an incomplete installation.
    GoogleAuthRequest = None  # type: ignore[assignment,misc]
    build = None  # type: ignore[assignment]

    class HttpError(Exception):  # type: ignore[no-redef]
        pass


GMAIL_USER_ID = "me"
RECEIVED_QUERY = "-in:sent -in:drafts -in:spam -in:trash"
EXCLUDED_LABEL_IDS = frozenset({"SENT", "DRAFT", "SPAM", "TRASH"})
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRYABLE_REASONS = frozenset(
    {
        "backendError",
        "internalError",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    }
)


class GmailErrorCode(StrEnum):
    AUTH_REQUIRED = "gmail_auth_required"
    PERMISSION_DENIED = "gmail_permission_denied"
    NOT_FOUND = "gmail_message_not_found"
    HISTORY_EXPIRED = "gmail_history_expired"
    RATE_LIMITED = "gmail_rate_limited"
    TRANSIENT = "gmail_transient"
    INVALID_RESPONSE = "gmail_invalid_response"
    PERMANENT = "gmail_permanent"


class GmailError(RuntimeError):
    def __init__(
        self,
        code: GmailErrorCode,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
        self.status = status


class HistoryExpiredError(GmailError):
    def __init__(self) -> None:
        super().__init__(GmailErrorCode.HISTORY_EXPIRED, status=404)


class MessageNotFoundError(GmailError):
    def __init__(self) -> None:
        super().__init__(GmailErrorCode.NOT_FOUND, status=404)


@dataclass(frozen=True)
class HistoryPage:
    messages: tuple[dict[str, Any], ...]
    history_id: str
    next_page_token: str | None


class ExecutableRequest(Protocol):
    def execute(self) -> dict[str, Any]: ...


def _http_status(error: BaseException) -> int | None:
    response = getattr(error, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _http_reasons(error: BaseException) -> set[str]:
    raw = getattr(error, "content", b"")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return set()

    reasons: set[str] = set()
    root = payload.get("error", payload) if isinstance(payload, dict) else {}
    if isinstance(root, dict):
        errors = root.get("errors", [])
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, dict) and item.get("reason"):
                    reasons.add(str(item["reason"]))
        details = root.get("details", [])
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict) and item.get("reason"):
                    reasons.add(str(item["reason"]))
    return reasons


def classify_http_error(
    error: BaseException,
    *,
    history_request: bool = False,
    message_request: bool = False,
) -> GmailError:
    status = _http_status(error)
    reasons = _http_reasons(error)

    if status == 404 and history_request:
        return HistoryExpiredError()
    if status == 404 and message_request:
        return MessageNotFoundError()
    if status == 401:
        return GmailError(GmailErrorCode.AUTH_REQUIRED, status=status)
    if status == 403 and reasons & RETRYABLE_REASONS:
        return GmailError(
            GmailErrorCode.RATE_LIMITED,
            retryable=True,
            status=status,
        )
    if status == 403:
        return GmailError(GmailErrorCode.PERMISSION_DENIED, status=status)
    if status == 429:
        return GmailError(
            GmailErrorCode.RATE_LIMITED,
            retryable=True,
            status=status,
        )
    if status in RETRYABLE_HTTP_STATUSES or reasons & RETRYABLE_REASONS:
        return GmailError(
            GmailErrorCode.TRANSIENT,
            retryable=True,
            status=status,
        )
    return GmailError(GmailErrorCode.PERMANENT, status=status)


class GmailClient:
    """Small, retrying Gmail API adapter.

    Every mutating operation is message-level. The adapter accepts a pre-built
    service in tests so no network or Google discovery work is needed.
    """

    def __init__(
        self,
        credentials: Any,
        *,
        service: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        max_attempts: int = 5,
        revoke_transport: Callable[..., Any] = httpx.post,
        sent_cache_ttl_seconds: float = 86_400,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if service is None:
            if build is None:
                raise RuntimeError("google-api-python-client is required to use GmailClient")
            service = build(
                "gmail",
                "v1",
                credentials=credentials,
                cache_discovery=False,
            )
        self.credentials = credentials
        self.service = service
        self._sleep = sleeper
        self._random = random_source
        self._max_attempts = max(1, max_attempts)
        self._sent_cache: dict[str, tuple[bool, float]] = {}
        self._revoke_transport = revoke_transport
        self._sent_cache_ttl_seconds = max(0.0, sent_cache_ttl_seconds)
        self._monotonic = monotonic_clock

    def _refresh_credentials(self) -> bool:
        refresh_token = getattr(self.credentials, "refresh_token", None)
        if not refresh_token or GoogleAuthRequest is None:
            return False
        try:
            self.credentials.refresh(GoogleAuthRequest())
        except Exception:
            return False
        return True

    def _execute(
        self,
        request_factory: Callable[[], ExecutableRequest],
        *,
        history_request: bool = False,
        message_request: bool = False,
    ) -> dict[str, Any]:
        refreshed = False
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = request_factory().execute()
                if not isinstance(response, dict):
                    raise GmailError(GmailErrorCode.INVALID_RESPONSE)
                return response
            except GmailError:
                raise
            except HttpError as error:
                classified = classify_http_error(
                    error,
                    history_request=history_request,
                    message_request=message_request,
                )
                if (
                    classified.code == GmailErrorCode.AUTH_REQUIRED
                    and not refreshed
                    and self._refresh_credentials()
                ):
                    refreshed = True
                    continue
                if not classified.retryable or attempt >= self._max_attempts:
                    raise classified from error
                delay = min(32.0, float(2 ** (attempt - 1)))
                self._sleep(delay * (0.5 + self._random()))
            except (
                TimeoutError,
                ConnectionError,
                OSError,
                HttpLib2Error,
            ) as error:
                if attempt >= self._max_attempts:
                    raise GmailError(
                        GmailErrorCode.TRANSIENT,
                        retryable=True,
                    ) from error
                delay = min(32.0, float(2 ** (attempt - 1)))
                self._sleep(delay * (0.5 + self._random()))
        raise GmailError(GmailErrorCode.TRANSIENT, retryable=True)

    def get_profile(self) -> dict[str, Any]:
        return self._execute(lambda: self.service.users().getProfile(userId=GMAIL_USER_ID))

    def list_labels(self) -> list[dict[str, Any]]:
        response = self._execute(lambda: self.service.users().labels().list(userId=GMAIL_USER_ID))
        labels = response.get("labels", [])
        return [item for item in labels if isinstance(item, dict)]

    def ensure_labels(self) -> dict[str, str]:
        existing = {
            str(label.get("name")): str(label.get("id"))
            for label in self.list_labels()
            if label.get("name") and label.get("id")
        }
        desired: list[tuple[str, str, str]] = [
            (category.value, label_name, LEGACY_CATEGORY_LABELS[category])
            for category, label_name in CATEGORY_LABELS.items()
        ]
        desired.append(("needs_review", NEEDS_REVIEW_LABEL, LEGACY_NEEDS_REVIEW_LABEL))
        resolved: dict[str, str] = {}
        for key, label_name, legacy_name in desired:
            label_id = existing.get(label_name)
            legacy_id = existing.get(legacy_name)
            if label_id and legacy_id and label_id != legacy_id:
                self._merge_legacy_label(legacy_id, label_id)
                existing.pop(legacy_name, None)
            if not label_id and existing.get(legacy_name):
                label_id = existing[legacy_name]
                updated = self._execute(
                    lambda current_id=label_id, name=label_name: self.service.users()
                    .labels()
                    .patch(
                        userId=GMAIL_USER_ID,
                        id=current_id,
                        body={
                            "name": name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    )
                )
                if str(updated.get("id", "")) != label_id:
                    raise GmailError(GmailErrorCode.INVALID_RESPONSE)
                existing.pop(legacy_name, None)
                existing[label_name] = label_id
            if not label_id:
                created = self._execute(
                    lambda name=label_name: self.service.users()
                    .labels()
                    .create(
                        userId=GMAIL_USER_ID,
                        body={
                            "name": name,
                            "labelListVisibility": "labelShow",
                            "messageListVisibility": "show",
                        },
                    )
                )
                label_id = str(created.get("id", ""))
                if not label_id:
                    raise GmailError(GmailErrorCode.INVALID_RESPONSE)
                existing[label_name] = label_id
            resolved[key] = label_id
        return resolved

    def _merge_legacy_label(self, legacy_id: str, direct_id: str) -> None:
        """Move every legacy member to its direct label, then remove the legacy label."""

        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "userId": GMAIL_USER_ID,
                "labelIds": [legacy_id],
                "includeSpamTrash": True,
                "maxResults": 500,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = self._execute(
                lambda request_kwargs=kwargs: self.service.users()
                .messages()
                .list(**request_kwargs)
            )
            for message in response.get("messages", []):
                if isinstance(message, dict) and message.get("id"):
                    self.modify_message_labels(
                        str(message["id"]),
                        add_label_ids={direct_id},
                        remove_label_ids={legacy_id},
                    )
            page_token = (
                str(response["nextPageToken"]) if response.get("nextPageToken") else None
            )
            if not page_token:
                break
        self._execute(
            lambda: self.service.users()
            .labels()
            .delete(userId=GMAIL_USER_ID, id=legacy_id)
        )

    def list_received_page(
        self,
        *,
        page_token: str | None = None,
        max_results: int = 500,
    ) -> tuple[list[dict[str, Any]], str | None]:
        kwargs: dict[str, Any] = {
            "userId": GMAIL_USER_ID,
            "q": RECEIVED_QUERY,
            "includeSpamTrash": False,
            "maxResults": max(1, min(max_results, 500)),
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = self._execute(lambda: self.service.users().messages().list(**kwargs))
        messages = [
            item
            for item in response.get("messages", [])
            if isinstance(item, dict) and item.get("id")
        ]
        token = response.get("nextPageToken")
        return messages, str(token) if token else None

    def get_metadata(self, message_id: str) -> dict[str, Any]:
        return self._execute(
            lambda: self.service.users()
            .messages()
            .get(
                userId=GMAIL_USER_ID,
                id=message_id,
                format="metadata",
                metadataHeaders=[
                    "From",
                    "To",
                    "Subject",
                    "Date",
                    "Authentication-Results",
                    "List-Id",
                    "List-Unsubscribe",
                    "Precedence",
                    "Auto-Submitted",
                ],
            ),
            message_request=True,
        )

    def get_full_message(self, message_id: str) -> dict[str, Any]:
        return self._execute(
            lambda: self.service.users()
            .messages()
            .get(
                userId=GMAIL_USER_ID,
                id=message_id,
                format="full",
            ),
            message_request=True,
        )

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._execute(
            lambda: self.service.users()
            .messages()
            .attachments()
            .get(
                userId=GMAIL_USER_ID,
                messageId=message_id,
                id=attachment_id,
            ),
            message_request=True,
        )
        encoded = response.get("data")
        if not isinstance(encoded, str):
            raise GmailError(GmailErrorCode.INVALID_RESPONSE)
        try:
            return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError) as error:
            raise GmailError(GmailErrorCode.INVALID_RESPONSE) from error

    def list_history_added_page(
        self,
        start_history_id: str,
        *,
        page_token: str | None = None,
    ) -> HistoryPage:
        kwargs: dict[str, Any] = {
            "userId": GMAIL_USER_ID,
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
            "maxResults": 500,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = self._execute(
            lambda: self.service.users().history().list(**kwargs),
            history_request=True,
        )
        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        for history in response.get("history", []):
            if not isinstance(history, dict):
                continue
            for added in history.get("messagesAdded", []):
                message = added.get("message", {}) if isinstance(added, dict) else {}
                if not isinstance(message, dict) or not message.get("id"):
                    continue
                message_id = str(message["id"])
                label_ids = {str(item) for item in message.get("labelIds", []) if item}
                if message_id in seen or label_ids & EXCLUDED_LABEL_IDS:
                    continue
                seen.add(message_id)
                messages.append(message)
        next_token = response.get("nextPageToken")
        response_history = response.get("historyId", start_history_id)
        return HistoryPage(
            messages=tuple(messages),
            history_id=str(response_history),
            next_page_token=str(next_token) if next_token else None,
        )

    def list_history_added(
        self,
        start_history_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        messages: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_token: str | None = None
        newest_history_id = start_history_id
        while True:
            page = self.list_history_added_page(
                start_history_id,
                page_token=page_token,
            )
            newest_history_id = page.history_id
            for message in page.messages:
                message_id = str(message["id"])
                if message_id not in seen:
                    messages.append(message)
                    seen.add(message_id)
            page_token = page.next_page_token
            if not page_token:
                return messages, newest_history_id

    def has_sent_to(self, sender: str) -> bool:
        normalized = sender.strip().lower()
        if not normalized:
            return False
        now = self._monotonic()
        cached = self._sent_cache.get(normalized)
        if cached is not None:
            result, cached_at = cached
            if now - cached_at <= self._sent_cache_ttl_seconds:
                return result
            self._sent_cache.pop(normalized, None)
        escaped = normalized.replace("\\", "\\\\").replace('"', '\\"')
        response = self._execute(
            lambda: self.service.users()
            .messages()
            .list(
                userId=GMAIL_USER_ID,
                q=f'in:sent to:"{escaped}"',
                includeSpamTrash=False,
                maxResults=1,
            )
        )
        result = bool(response.get("messages")) or int(response.get("resultSizeEstimate", 0)) > 0
        self._sent_cache[normalized] = (result, now)
        return result

    def modify_message_labels(
        self,
        message_id: str,
        *,
        add_label_ids: Iterable[str] = (),
        remove_label_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        added = sorted({item for item in add_label_ids if item})
        removed = sorted({item for item in remove_label_ids if item and item not in added})
        if not added and not removed:
            return self.get_metadata(message_id)
        return self._execute(
            lambda: self.service.users()
            .messages()
            .modify(
                userId=GMAIL_USER_ID,
                id=message_id,
                body={"addLabelIds": added, "removeLabelIds": removed},
            ),
            message_request=True,
        )

    def trash_message(self, message_id: str) -> dict[str, Any]:
        """Move a message to Gmail Trash; this is not permanent deletion."""

        return self._execute(
            lambda: self.service.users()
            .messages()
            .trash(userId=GMAIL_USER_ID, id=message_id),
            message_request=True,
        )

    def untrash_message(self, message_id: str) -> dict[str, Any]:
        """Restore a previously trashed message for a batch undo."""

        return self._execute(
            lambda: self.service.users()
            .messages()
            .untrash(userId=GMAIL_USER_ID, id=message_id),
            message_request=True,
        )

    def revoke(self) -> None:
        token = getattr(self.credentials, "token", None) or getattr(
            self.credentials,
            "refresh_token",
            None,
        )
        if not token:
            return
        try:
            response = self._revoke_transport(
                REVOKE_ENDPOINT,
                data={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
        except (httpx.HTTPError, OSError) as error:
            raise GmailError(GmailErrorCode.TRANSIENT, retryable=True) from error
        status = int(getattr(response, "status_code", 200))
        if status not in {200, 400}:
            if status in RETRYABLE_HTTP_STATUSES:
                raise GmailError(
                    GmailErrorCode.TRANSIENT,
                    retryable=True,
                    status=status,
                )
            raise GmailError(GmailErrorCode.PERMANENT, status=status)

    @staticmethod
    def label_ids(message: Mapping[str, Any]) -> set[str]:
        return {str(item) for item in message.get("labelIds", []) if item}
