"""Safe, bounded parsing of Gmail message resources."""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
import os
import re
import sys
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Comment

from mail_buddy.config import Settings
from mail_buddy.contracts import EmailMetadata, ParsedEmail
from mail_buddy.security import redact_text

AttachmentLoader = Callable[[str, str], bytes | Awaitable[bytes]]

MAX_BODY_PART_BYTES = 2 * 1024 * 1024
MAX_BODY_TEXT_CHARS = 50_000
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_TEXT_CHARS = 12_000
MAX_PDF_PAGES = 25
ATTACHMENT_TIMEOUT_SECONDS = 12.0
MAX_HEADER_CHARS = 8_192
MAX_SNIPPET_CHARS = 2_000
MAX_MIME_DEPTH = 30
MAX_MIME_PARTS = 200

_HEADER_ALLOWLIST = frozenset(
    {
        "authentication-results",
        "auto-submitted",
        "content-type",
        "delivered-to",
        "from",
        "list-id",
        "list-unsubscribe",
        "list-unsubscribe-post",
        "precedence",
        "received-spf",
        "reply-to",
        "return-path",
        "subject",
        "to",
        "x-auto-response-suppress",
        "x-gm-message-state",
    }
)
_HTML_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "math",
    "object",
    "embed",
    "iframe",
    "canvas",
)
_QUOTED_BOUNDARIES = (
    re.compile(r"(?i)^on .{1,300} wrote:\s*$"),
    re.compile(r"(?i)^-{2,}\s*original message\s*-{2,}\s*$"),
    re.compile(r"(?i)^begin forwarded message:\s*$"),
    re.compile(r"(?i)^_{5,}\s*$"),
)
_FORWARDED_BOUNDARIES = (
    re.compile(r"(?i)^-{2,}\s*(?:original|forwarded) message\s*-{2,}\s*$"),
    re.compile(r"(?i)^begin forwarded message:\s*$"),
)
_FORWARDED_HEADER = re.compile(r"(?i)^(from|sent|date|to|cc|subject):\s+\S")
_CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
_WHITESPACE = re.compile(r"[ \t]+")
_EXCESS_BLANKS = re.compile(r"\n{4,}")


def _decode_header_value(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        decoded = str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        decoded = value
    return _clean_text(decoded, MAX_HEADER_CHARS, preserve_lines=False)


def _clean_text(value: str, limit: int, *, preserve_lines: bool = True) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL_CHARS.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = _WHITESPACE.sub(" ", value)
    if preserve_lines:
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = _EXCESS_BLANKS.sub("\n\n\n", value)
    else:
        value = re.sub(r"\s+", " ", value)
    return value.strip()[:limit]


def sanitize_html(value: str, *, limit: int = MAX_BODY_TEXT_CHARS) -> str:
    """Return visible text only; active and hidden HTML content is discarded."""

    soup = BeautifulSoup(value, "html.parser")
    for node in soup(_HTML_DROP_TAGS):
        node.decompose()
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for node in soup.find_all(True):
        style = str(node.attrs.get("style", "")).lower().replace(" ", "")
        if (
            node.has_attr("hidden")
            or str(node.attrs.get("aria-hidden", "")).lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            node.decompose()
    return _clean_text(soup.get_text(separator="\n"), limit)


def strip_quoted_replies(value: str) -> str:
    """Remove common quoted-reply and forwarded-message sections."""

    kept: list[str] = []
    for line in value.splitlines():
        normalized = line.strip()
        if any(pattern.match(normalized) for pattern in _QUOTED_BOUNDARIES):
            break
        if normalized.startswith(">"):
            continue
        kept.append(line)

    # Some clients omit the delimiter but emit a compact From/Sent/To/Subject
    # block.  Only trim when at least three consecutive mail headers appear.
    header_run = 0
    trim_at: int | None = None
    for index, line in enumerate(kept):
        if re.match(r"(?i)^(from|sent|to|cc|subject):\s+\S", line.strip()):
            header_run += 1
            if header_run == 3:
                trim_at = max(0, index - 2)
                break
        elif line.strip():
            header_run = 0
    if trim_at is not None:
        kept = kept[:trim_at]
    return _clean_text("\n".join(kept), MAX_BODY_TEXT_CHARS)


def extract_forwarded_message(value: str) -> str | None:
    """Return the original message from a common mail-forwarding wrapper.

    Forwarding often changes the outer sender and subject. The forwarded
    message is still untrusted content, but it is the useful classification
    evidence for a consolidated destination inbox. This deliberately does not
    apply to ordinary quoted replies, which continue through
    strip_quoted_replies.
    """

    lines = value.splitlines()
    for index, line in enumerate(lines):
        if any(pattern.match(line.strip()) for pattern in _FORWARDED_BOUNDARIES):
            forwarded = _clean_text("\n".join(lines[index + 1 :]), MAX_BODY_TEXT_CHARS)
            return forwarded or None

    # Some forwarding clients use only a compact From/Sent/To/Subject block.
    # Require three adjacent headers to avoid mistaking a sentence in a normal
    # email for a forwarded message.
    header_run = 0
    for index, line in enumerate(lines):
        if _FORWARDED_HEADER.match(line.strip()):
            header_run += 1
            if header_run == 3:
                start = max(0, index - 2)
                forwarded = _clean_text("\n".join(lines[start:]), MAX_BODY_TEXT_CHARS)
                return forwarded or None
        elif line.strip():
            header_run = 0
    return None


def redact_for_model(value: str, *, limit: int) -> str:
    """Redact common secrets and identifiers before local model inference."""

    return _clean_text(redact_text(value), limit)


def _part_headers(part: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw_headers = part.get("headers", [])
    if not isinstance(raw_headers, list):
        return result
    for item in raw_headers:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip().lower()
        if not name:
            continue
        value = _decode_header_value(item.get("value", ""))
        if value:
            result[name] = f"{result[name]}\n{value}" if name in result else value
    return result


def _decode_gmail_data(encoded: object, *, max_bytes: int) -> bytes | None:
    if not isinstance(encoded, str) or not encoded:
        return b""
    if len(encoded) > ((max_bytes + 2) // 3) * 4 + 8:
        return None
    try:
        padded = encoded + ("=" * (-len(encoded) % 4))
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (UnicodeEncodeError, binascii.Error, ValueError):
        return None
    return decoded if len(decoded) <= max_bytes else None


def _charset_for(part: Mapping[str, Any]) -> str:
    headers = _part_headers(part)
    content_type = headers.get("content-type", "")
    message = Message()
    if content_type:
        message["content-type"] = content_type
    return message.get_content_charset() or "utf-8"


def _decode_body_bytes(data: bytes, part: Mapping[str, Any]) -> str:
    charset = _charset_for(part)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _walk_leaves(
    part: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], bool]:
    """Walk a bounded MIME tree without using Python recursion."""

    leaves: list[Mapping[str, Any]] = []
    stack: list[tuple[Mapping[str, Any], int]] = [(part, 0)]
    visited = 0
    incomplete = False
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_MIME_PARTS:
            incomplete = True
            break
        children = current.get("parts")
        valid_children = (
            [child for child in children if isinstance(child, Mapping)]
            if isinstance(children, list)
            else []
        )
        if valid_children:
            if depth >= MAX_MIME_DEPTH:
                incomplete = True
                continue
            stack.extend((child, depth + 1) for child in reversed(valid_children))
        else:
            leaves.append(current)
    return leaves, incomplete


def _attachment_kind(filename: str, mime_type: str) -> str | None:
    extension = Path(filename.lower()).suffix if filename else ""
    mime_type = mime_type.lower().split(";", 1)[0].strip()
    if extension == ".pdf":
        return "pdf" if mime_type in {"application/pdf", "application/octet-stream", ""} else None
    if extension in {".txt", ".csv", ".md"}:
        allowed_mimes = {
            "",
            "application/octet-stream",
            "text/csv",
            "text/markdown",
            "text/plain",
        }
        return "text" if mime_type in allowed_mimes else None
    if not filename:
        if mime_type == "application/pdf":
            return "pdf"
        if mime_type in {"text/plain", "text/csv", "text/markdown"}:
            return "text"
    return None


async def _call_loader(loader: AttachmentLoader, message_id: str, attachment_id: str) -> bytes:
    if inspect.iscoroutinefunction(loader):
        result = loader(message_id, attachment_id)
    else:
        result = await asyncio.to_thread(loader, message_id, attachment_id)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, bytes | bytearray | memoryview):
        raise TypeError("attachment loader returned a non-bytes value")
    return bytes(result)


async def _extract_in_subprocess(
    data: bytes,
    *,
    kind: str,
    max_chars: int,
    timeout_seconds: float,
) -> tuple[str, bool]:
    request = json.dumps(
        {
            "kind": kind,
            "data": base64.b64encode(data).decode("ascii"),
            "max_chars": max_chars,
            "max_pages": MAX_PDF_PAGES,
        },
        separators=(",", ":"),
    ).encode("ascii")
    worker_path = Path(__file__).with_name("attachment_worker.py")
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(worker_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(request), timeout=timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return "", True
    except (OSError, RuntimeError):
        return "", True
    if process.returncode != 0 or len(stdout) > 64_000:
        return "", True
    try:
        response = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "", True
    if not isinstance(response, dict) or response.get("ok") is not True:
        return "", True
    text = response.get("text")
    if not isinstance(text, str):
        return "", True
    clean = _clean_text(text, max_chars)
    incomplete = bool(response.get("truncated")) or len(text) > max_chars
    return clean, incomplete


class ContentExtractor:
    """Parse Gmail metadata first, then bounded body/attachment content on demand."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings
        self.attachment_timeout_seconds = ATTACHMENT_TIMEOUT_SECONDS

    def parse_metadata(self, message_resource: Mapping[str, Any]) -> EmailMetadata:
        payload = message_resource.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        all_headers = _part_headers(payload)
        headers = {name: value for name, value in all_headers.items() if name in _HEADER_ALLOWLIST}
        sender_value = headers.get("from", "")
        _, sender_address = parseaddr(sender_value)
        sender = sender_address.strip().lower()
        if not sender and "@" in sender_value and " " not in sender_value:
            sender = sender_value.strip("<>").lower()
        sender_domain = sender.rpartition("@")[2].rstrip(".")
        try:
            sender_domain = sender_domain.encode("idna").decode("ascii")
        except UnicodeError:
            sender_domain = ""

        raw_label_values = message_resource.get("labelIds", [])
        label_values = raw_label_values if isinstance(raw_label_values, list) else []
        label_ids = {str(item) for item in label_values if isinstance(item, str)}
        internal_raw = message_resource.get("internalDate", 0)
        try:
            internal_date = max(0, int(internal_raw))
        except (TypeError, ValueError):
            internal_date = 0
        return EmailMetadata(
            message_id=str(message_resource.get("id", ""))[:256],
            thread_id=str(message_resource.get("threadId", ""))[:256],
            internal_date=internal_date,
            sender=sender[:512],
            sender_domain=sender_domain[:253],
            subject=headers.get("subject", "")[:MAX_HEADER_CHARS],
            headers=headers,
            label_ids=label_ids,
            snippet=_clean_text(
                str(message_resource.get("snippet", "")),
                MAX_SNIPPET_CHARS,
                preserve_lines=False,
            ),
            had_inbox="INBOX" in label_ids,
        )

    async def parse_full(
        self,
        message_resource: Mapping[str, Any],
        attachment_loader: AttachmentLoader,
        two_way_history: bool = False,
    ) -> ParsedEmail:
        metadata = self.parse_metadata(message_resource).model_copy(
            update={"two_way_history": bool(two_way_history)}
        )
        payload = message_resource.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        leaves, mime_incomplete = _walk_leaves(payload)
        plain_bodies: list[str] = []
        html_bodies: list[str] = []
        attachment_parts: list[tuple[Mapping[str, Any], str | None, str]] = []
        incomplete = mime_incomplete

        for part in leaves:
            mime_type = str(part.get("mimeType", "")).lower().split(";", 1)[0]
            filename = _decode_header_value(part.get("filename", ""))
            headers = _part_headers(part)
            disposition = headers.get("content-disposition", "").lower()
            is_attachment = bool(filename) or disposition.startswith("attachment")
            body = part.get("body")
            body = body if isinstance(body, Mapping) else {}
            attachment_id = body.get("attachmentId")

            if is_attachment:
                kind = _attachment_kind(filename, mime_type)
                has_inline_data = isinstance(body.get("data"), str)
                if kind is None or (not isinstance(attachment_id, str) and not has_inline_data):
                    incomplete = True
                    continue
                attachment_parts.append(
                    (
                        part,
                        attachment_id if isinstance(attachment_id, str) else None,
                        kind,
                    )
                )
                continue
            if mime_type not in {"text/plain", "text/html"}:
                continue

            raw: bytes | None
            if body.get("data"):
                raw = _decode_gmail_data(body.get("data"), max_bytes=MAX_BODY_PART_BYTES)
            elif isinstance(attachment_id, str):
                declared_size = _safe_size(body.get("size"))
                if declared_size > MAX_BODY_PART_BYTES:
                    raw = None
                else:
                    try:
                        raw = await _call_loader(
                            attachment_loader, metadata.message_id, attachment_id
                        )
                    except (OSError, RuntimeError, TypeError, ValueError):
                        raw = None
                    if raw is not None and len(raw) > MAX_BODY_PART_BYTES:
                        raw = None
            else:
                raw = b""
            if raw is None:
                incomplete = True
                continue
            decoded = _decode_body_bytes(raw, part)
            if mime_type == "text/plain":
                plain_bodies.append(decoded)
            else:
                html_bodies.append(decoded)

        if plain_bodies:
            body_text = _clean_text("\n\n".join(plain_bodies), MAX_BODY_TEXT_CHARS)
        else:
            body_text = sanitize_html("\n\n".join(html_bodies), limit=MAX_BODY_TEXT_CHARS)
        body_text = extract_forwarded_message(body_text) or strip_quoted_replies(body_text)

        total_downloaded = 0
        attachment_chunks: list[str] = []
        remaining_chars = MAX_ATTACHMENT_TEXT_CHARS
        for part, attachment_id, kind in attachment_parts:
            if remaining_chars <= 0:
                incomplete = True
                break
            if total_downloaded >= MAX_TOTAL_ATTACHMENT_BYTES:
                incomplete = True
                break
            body = part.get("body")
            body = body if isinstance(body, Mapping) else {}
            declared_size = _safe_size(body.get("size"))
            if declared_size > MAX_ATTACHMENT_BYTES:
                incomplete = True
                continue
            remaining_bytes = MAX_TOTAL_ATTACHMENT_BYTES - total_downloaded
            if declared_size > remaining_bytes:
                incomplete = True
                continue
            if body.get("data"):
                raw = _decode_gmail_data(body.get("data"), max_bytes=MAX_ATTACHMENT_BYTES)
                if raw is None:
                    incomplete = True
                    continue
            elif attachment_id is not None:
                # Gmail supplies the decoded attachment size. Refuse an external
                # fetch without it because the byte budget cannot be enforced.
                if declared_size <= 0:
                    incomplete = True
                    continue
                try:
                    raw = await _call_loader(attachment_loader, metadata.message_id, attachment_id)
                except (OSError, RuntimeError, TypeError, ValueError):
                    incomplete = True
                    continue
            else:
                incomplete = True
                continue
            actual_size = len(raw)
            if actual_size > MAX_ATTACHMENT_BYTES or actual_size > remaining_bytes:
                incomplete = True
                break
            total_downloaded += actual_size
            extracted, extraction_incomplete = await _extract_in_subprocess(
                raw,
                kind=kind,
                max_chars=remaining_chars,
                timeout_seconds=self.attachment_timeout_seconds,
            )
            incomplete = incomplete or extraction_incomplete
            if extracted:
                attachment_chunks.append(extracted)
                remaining_chars -= len(extracted)

        attachment_text = _clean_text("\n\n".join(attachment_chunks), MAX_ATTACHMENT_TEXT_CHARS)
        return ParsedEmail(
            metadata=metadata,
            body_text=body_text,
            attachment_text=attachment_text,
            attachment_skipped=incomplete,
        )


def _safe_size(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
