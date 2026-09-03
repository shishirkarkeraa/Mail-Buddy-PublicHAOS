from __future__ import annotations

import base64
import io
import json
import subprocess
import sys

import pytest
from pypdf import PdfWriter

from mail_buddy import content as content_module
from mail_buddy.attachment_worker import handle_request
from mail_buddy.content import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_TEXT_CHARS,
    MAX_MIME_DEPTH,
    MAX_TOTAL_ATTACHMENT_BYTES,
    ContentExtractor,
    extract_forwarded_message,
    redact_for_model,
    sanitize_html,
    strip_quoted_replies,
)


def gmail_data(value: bytes | str) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def header(name: str, value: str) -> dict[str, str]:
    return {"name": name, "value": value}


def base_message(payload: dict, **overrides: object) -> dict:
    result = {
        "id": "message-1",
        "threadId": "thread-1",
        "internalDate": "1720000000000",
        "labelIds": ["INBOX", "UNREAD"],
        "snippet": "A short preview",
        "payload": payload,
    }
    result.update(overrides)
    return result


def test_parse_metadata_normalizes_headers_sender_and_labels() -> None:
    message = base_message(
        {
            "mimeType": "text/plain",
            "headers": [
                header("From", "Family Member <Person@Example.COM>"),
                header("Subject", "=?utf-8?q?College_Notice?="),
                header("List-Unsubscribe", "<mailto:leave@example.com>"),
                header("X-Private-Body-Like-Header", "must not be retained"),
            ],
            "body": {"data": gmail_data("hello")},
        }
    )

    metadata = ContentExtractor().parse_metadata(message)

    assert metadata.message_id == "message-1"
    assert metadata.thread_id == "thread-1"
    assert metadata.sender == "person@example.com"
    assert metadata.sender_domain == "example.com"
    assert metadata.subject == "College Notice"
    assert metadata.had_inbox is True
    assert metadata.label_ids == {"INBOX", "UNREAD"}
    assert "list-unsubscribe" in metadata.headers
    assert "x-private-body-like-header" not in metadata.headers


def test_html_sanitization_and_quoted_reply_removal() -> None:
    html = """
    <html><body>
      <p>Hello <b>friend</b>.</p>
      <script>stealSecrets()</script>
      <p style="display: none">hidden tracking text</p>
      <!-- private comment -->
      <p>See you soon.</p>
    </body></html>
    """
    sanitized = sanitize_html(html)
    stripped = strip_quoted_replies(
        f"{sanitized}\nOn Wed, Person <person@example.com> wrote:\n> old secret"
    )

    assert "Hello" in stripped
    assert "See you soon" in stripped
    assert "stealSecrets" not in stripped
    assert "hidden tracking" not in stripped
    assert "old secret" not in stripped


def test_forwarded_message_content_is_retained_for_classification() -> None:
    forwarded = extract_forwarded_message(
        "FYI - this was sent to my old address.\n\n"
        "---------- Forwarded message ---------\n"
        "From: My Bank <alerts@bank.example>\n"
        "Date: Monday\n"
        "To: destination@example.com\n"
        "Subject: Transaction alert\n\n"
        "Your account was debited for INR 500."
    )

    assert forwarded is not None
    assert "Transaction alert" in forwarded
    assert "account was debited" in forwarded


@pytest.mark.asyncio
async def test_parse_full_uses_original_content_from_forwarded_mail() -> None:
    message = base_message(
        {
            "mimeType": "text/plain",
            "headers": [
                header("From", "forwarder@example.com"),
                header("Subject", "Fwd: please see"),
            ],
            "body": {
                "data": gmail_data(
                    "---------- Forwarded message ---------\n"
                    "From: alerts@bank.example\n"
                    "Date: Monday\n"
                    "To: old-address@example.com\n"
                    "Subject: Transaction alert\n\n"
                    "Your account was debited for INR 500."
                )
            },
        }
    )

    parsed = await ContentExtractor().parse_full(message, lambda *_: b"")

    assert "Transaction alert" in parsed.body_text
    assert "account was debited" in parsed.body_text


@pytest.mark.asyncio
async def test_parse_full_prefers_plain_body_and_extracts_text_attachment() -> None:
    message = base_message(
        {
            "mimeType": "multipart/mixed",
            "headers": [
                header("From", "person@example.com"),
                header("Subject", "Project update"),
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [header("Content-Type", "text/plain; charset=utf-8")],
                    "body": {
                        "data": gmail_data(
                            "Current update.\nOn Tue, Old Sender wrote:\n> old reply"
                        )
                    },
                },
                {
                    "mimeType": "text/html",
                    "headers": [header("Content-Type", "text/html; charset=utf-8")],
                    "body": {"data": gmail_data("<p>Duplicate HTML body</p>")},
                },
                {
                    "mimeType": "text/plain",
                    "filename": "notes.txt",
                    "headers": [header("Content-Disposition", 'attachment; filename="notes.txt"')],
                    "body": {"attachmentId": "attachment-1", "size": 20},
                },
            ],
        }
    )

    def loader(message_id: str, attachment_id: str) -> bytes:
        assert (message_id, attachment_id) == ("message-1", "attachment-1")
        return b"Safe attachment notes"

    parsed = await ContentExtractor().parse_full(message, loader, two_way_history=True)

    assert parsed.metadata.two_way_history is True
    assert parsed.body_text == "Current update."
    assert "Duplicate HTML body" not in parsed.body_text
    assert parsed.attachment_text == "Safe attachment notes"
    assert parsed.attachment_skipped is False


@pytest.mark.asyncio
async def test_parse_full_supports_async_loader_for_external_body() -> None:
    message = base_message(
        {
            "mimeType": "text/html",
            "headers": [
                header("From", "alerts@example.com"),
                header("Subject", "External body"),
                header("Content-Type", "text/html; charset=utf-8"),
            ],
            "body": {"attachmentId": "body-1", "size": 30},
        }
    )

    async def loader(_message_id: str, _attachment_id: str) -> bytes:
        return b"<p>Visible</p><script>not visible</script>"

    parsed = await ContentExtractor().parse_full(message, loader)

    assert parsed.body_text == "Visible"
    assert parsed.attachment_skipped is False


@pytest.mark.asyncio
async def test_oversized_and_unsupported_attachments_are_never_downloaded() -> None:
    message = base_message(
        {
            "mimeType": "multipart/mixed",
            "headers": [
                header("From", "sender@example.com"),
                header("Subject", "Attachments"),
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": gmail_data("Message body")},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "huge.pdf",
                    "body": {
                        "attachmentId": "too-large",
                        "size": MAX_ATTACHMENT_BYTES + 1,
                    },
                },
                {
                    "mimeType": "application/vnd.ms-word.document.macroEnabled.12",
                    "filename": "macro.docm",
                    "body": {"attachmentId": "macro", "size": 100},
                },
            ],
        }
    )
    calls: list[str] = []

    def loader(_message_id: str, attachment_id: str) -> bytes:
        calls.append(attachment_id)
        return b"not reached"

    parsed = await ContentExtractor().parse_full(message, loader)

    assert calls == []
    assert parsed.body_text == "Message body"
    assert parsed.attachment_text == ""
    assert parsed.attachment_skipped is True


def test_redaction_removes_secrets_and_identifiers() -> None:
    raw = (
        "Email person@example.com. OTP code 123456. "
        "Reset at https://example.com/reset?token=secret "
        "and use account 1234567890123456."
    )
    redacted = redact_for_model(raw, limit=1_000)

    assert "person@example.com" not in redacted
    assert "123456" not in redacted
    assert "token=secret" not in redacted
    assert "1234567890123456" not in redacted
    assert "[REDACTED_" in redacted


@pytest.mark.asyncio
async def test_external_attachments_without_size_are_not_fetched() -> None:
    parts = [
        {
            "mimeType": "text/plain",
            "filename": f"attachment-{index}.txt",
            "headers": [header("Content-Disposition", "attachment")],
            "body": {"attachmentId": f"attachment-{index}", "size": 0},
        }
        for index in range(4)
    ]
    message = base_message(
        {
            "mimeType": "multipart/mixed",
            "headers": [header("From", "sender@example.com")],
            "parts": parts,
        }
    )
    calls: list[str] = []

    def loader(_message_id: str, attachment_id: str) -> bytes:
        calls.append(attachment_id)
        return b"\x00" * (4 * 1024 * 1024)

    parsed = await ContentExtractor().parse_full(message, loader)

    assert calls == []
    assert parsed.attachment_skipped is True


@pytest.mark.asyncio
async def test_actual_cumulative_attachment_bytes_stop_further_fetches() -> None:
    part_size = MAX_TOTAL_ATTACHMENT_BYTES // 2
    parts = [
        {
            "mimeType": "text/plain",
            "filename": f"attachment-{index}.txt",
            "headers": [header("Content-Disposition", "attachment")],
            "body": {
                "attachmentId": f"attachment-{index}",
                "size": part_size,
            },
        }
        for index in range(3)
    ]
    message = base_message(
        {
            "mimeType": "multipart/mixed",
            "headers": [header("From", "sender@example.com")],
            "parts": parts,
        }
    )
    calls: list[str] = []
    binary_like = b"\x00" * part_size

    def loader(_message_id: str, attachment_id: str) -> bytes:
        calls.append(attachment_id)
        return binary_like

    parsed = await ContentExtractor().parse_full(message, loader)

    assert calls == ["attachment-0", "attachment-1"]
    assert sum(len(binary_like) for _ in calls) == MAX_TOTAL_ATTACHMENT_BYTES
    assert parsed.attachment_skipped is True


@pytest.mark.asyncio
async def test_actual_per_attachment_overflow_stops_subsequent_fetches() -> None:
    parts = [
        {
            "mimeType": "text/plain",
            "filename": f"attachment-{index}.txt",
            "headers": [header("Content-Disposition", "attachment")],
            "body": {"attachmentId": f"attachment-{index}", "size": 1},
        }
        for index in range(2)
    ]
    message = base_message(
        {
            "mimeType": "multipart/mixed",
            "headers": [header("From", "sender@example.com")],
            "parts": parts,
        }
    )
    calls: list[str] = []

    def loader(_message_id: str, attachment_id: str) -> bytes:
        calls.append(attachment_id)
        return b"\x00" * (MAX_ATTACHMENT_BYTES + 1)

    parsed = await ContentExtractor().parse_full(message, loader)

    assert calls == ["attachment-0"]
    assert parsed.attachment_skipped is True


@pytest.mark.asyncio
async def test_mime_depth_is_bounded_without_python_recursion() -> None:
    payload: dict = {
        "mimeType": "text/plain",
        "body": {"data": gmail_data("deep body")},
    }
    for _ in range(MAX_MIME_DEPTH + 1_000):
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    message = base_message(payload)

    parsed = await ContentExtractor().parse_full(message, lambda _message_id, _attachment_id: b"")

    assert parsed.body_text == ""
    assert parsed.attachment_skipped is True


@pytest.mark.asyncio
async def test_mime_part_count_is_bounded() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": gmail_data(f"part-{index}")},
            }
            for index in range(250)
        ],
    }

    parsed = await ContentExtractor().parse_full(
        base_message(payload),
        lambda _message_id, _attachment_id: b"",
    )

    assert parsed.attachment_skipped is True
    assert "part-249" not in parsed.body_text


def worker_request(data: bytes, kind: str) -> dict:
    return {
        "kind": kind,
        "data": base64.b64encode(data).decode("ascii"),
        "max_chars": MAX_ATTACHMENT_TEXT_CHARS,
        "max_pages": 25,
    }


def pdf_bytes(*, pages: int = 1, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_attachment_worker_enforces_text_and_pdf_limits() -> None:
    long_text = handle_request(worker_request(b"a" * (MAX_ATTACHMENT_TEXT_CHARS + 1_000), "text"))
    too_many_pages = handle_request(worker_request(pdf_bytes(pages=26), "pdf"))

    assert long_text["ok"] is True
    assert len(long_text["text"]) == MAX_ATTACHMENT_TEXT_CHARS
    assert long_text["truncated"] is True
    assert too_many_pages["ok"] is False
    assert too_many_pages["reason"] == "pdf_page_limit"


@pytest.mark.parametrize(
    ("data", "expected_reason"),
    [
        (pdf_bytes(encrypted=True), "encrypted_pdf"),
        (b"not a PDF", "malformed_pdf"),
        (pdf_bytes(), "image_only_pdf"),
    ],
)
def test_attachment_worker_rejects_unreadable_pdfs(data: bytes, expected_reason: str) -> None:
    result = handle_request(worker_request(data, "pdf"))

    assert result["ok"] is False
    assert result["reason"] == expected_reason


@pytest.mark.asyncio
async def test_attachment_subprocess_timeout_kills_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StuckProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False

        async def communicate(self, _request: bytes) -> tuple[bytes, bytes]:
            await content_module.asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    process = StuckProcess()

    async def create_process(*_args: object, **_kwargs: object) -> StuckProcess:
        return process

    monkeypatch.setattr(content_module.asyncio, "create_subprocess_exec", create_process)
    extracted, incomplete = await content_module._extract_in_subprocess(
        b"safe",
        kind="text",
        max_chars=100,
        timeout_seconds=0.001,
    )

    assert extracted == ""
    assert incomplete is True
    assert process.killed is True


def test_attachment_worker_denies_network_and_constrains_resources() -> None:
    code = """
import json
import resource
import socket
from mail_buddy.attachment_worker import _apply_resource_limits, _deny_network
_deny_network()
denied = False
try:
    socket.socket()
except PermissionError:
    denied = True
_apply_resource_limits()
print(json.dumps({
    "denied": denied,
    "cpu": resource.getrlimit(resource.RLIMIT_CPU)[0],
    "nofile": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["denied"] is True
    assert result["cpu"] <= 6
    assert result["nofile"] <= 32
