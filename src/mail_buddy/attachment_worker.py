"""Constrained attachment text extraction subprocess.

This module intentionally has no dependency on the rest of the application.  The
parent process sends one small JSON request on stdin and receives one JSON response
on stdout.  Attachment bytes and extracted text never appear in command line
arguments, environment variables, or logs.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import socket
import sys
import unicodedata
from collections.abc import Callable
from typing import Any

MAX_REQUEST_BYTES = 7_500_000
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_OUTPUT_CHARS = 12_000
MAX_PDF_PAGES = 25


class _NetworkDeniedSocket:
    """Fail closed if a parser unexpectedly attempts to create a socket."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise PermissionError("network access is disabled in the attachment worker")


def _deny_network() -> None:
    def deny(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("network access is disabled in the attachment worker")

    socket.socket = _NetworkDeniedSocket  # type: ignore[assignment]
    socket.create_connection = deny  # type: ignore[assignment]
    socket.getaddrinfo = deny  # type: ignore[assignment]

    def audit_hook(event: str, _args: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            raise PermissionError("network access is disabled in the attachment worker")

    sys.addaudithook(audit_hook)


def _apply_resource_limits() -> None:
    try:
        import resource
    except ImportError:
        return

    def constrain(resource_id: int, requested: int) -> None:
        soft, hard = resource.getrlimit(resource_id)
        finite_hard = hard if hard != resource.RLIM_INFINITY else requested
        value = min(requested, finite_hard)
        resource.setrlimit(resource_id, (value, value))

    limits: tuple[tuple[str, int], ...] = (
        ("RLIMIT_CPU", 6),
        ("RLIMIT_FSIZE", 1 * 1024 * 1024),
        ("RLIMIT_NOFILE", 32),
        ("RLIMIT_CORE", 0),
    )
    for name, requested in limits:
        resource_id = getattr(resource, name, None)
        if resource_id is not None:
            try:
                constrain(resource_id, requested)
            except (OSError, ValueError):
                pass

    # RLIMIT_AS is reliable on the Linux target, but setting it on macOS can
    # invalidate mappings already established by the interpreter.
    if sys.platform.startswith("linux"):
        resource_id = getattr(resource, "RLIMIT_AS", None)
        if resource_id is not None:
            try:
                constrain(resource_id, 384 * 1024 * 1024)
            except (OSError, ValueError):
                pass


def _clean_text(value: str, max_chars: int) -> tuple[str, bool]:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\x00", "")
    value = re.sub(r"[\u0001-\u0008\u000b\u000c\u000e-\u001f\u007f]", "", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{4,}", "\n\n\n", value).strip()
    truncated = len(value) > max_chars
    return value[:max_chars], truncated


def _decode_plain(data: bytes, max_chars: int) -> dict[str, Any]:
    # Text attachments with a large NUL ratio are usually mislabeled binaries.
    if data and data.count(b"\x00") / len(data) > 0.08:
        return _skipped("binary_content")

    decoders: tuple[Callable[[bytes], str], ...] = (
        lambda raw: raw.decode("utf-8-sig"),
        lambda raw: raw.decode("utf-16"),
        lambda raw: raw.decode("latin-1"),
    )
    text: str | None = None
    for decoder in decoders:
        try:
            text = decoder(data)
            break
        except UnicodeError:
            continue
    if text is None:
        return _skipped("unsupported_encoding")

    cleaned, truncated = _clean_text(text, max_chars)
    if not cleaned:
        return _skipped("empty_text")
    return {"ok": True, "text": cleaned, "truncated": truncated, "reason": None}


def _decode_pdf(data: bytes, max_pages: int, max_chars: int) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return _skipped("pdf_parser_unavailable")

    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return _skipped("encrypted_pdf")
        if len(reader.pages) > max_pages:
            return _skipped("pdf_page_limit")

        chunks: list[str] = []
        remaining = max_chars
        truncated = False
        for page in reader.pages:
            if remaining <= 0:
                truncated = True
                break
            extracted = page.extract_text() or ""
            cleaned, page_truncated = _clean_text(extracted, remaining)
            if cleaned:
                chunks.append(cleaned)
                remaining -= len(cleaned)
            truncated = truncated or page_truncated
        text, final_truncated = _clean_text("\n\n".join(chunks), max_chars)
        if not text:
            return _skipped("image_only_pdf")
        return {
            "ok": True,
            "text": text,
            "truncated": truncated or final_truncated,
            "reason": None,
        }
    except (PdfReadError, OSError, ValueError, TypeError, KeyError, RecursionError):
        return _skipped("malformed_pdf")


def _skipped(reason: str) -> dict[str, Any]:
    return {"ok": False, "text": "", "truncated": False, "reason": reason}


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, min(maximum, value))


def handle_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict):
        return _skipped("invalid_request")
    kind = request.get("kind")
    if kind not in {"pdf", "text"}:
        return _skipped("unsupported_type")

    encoded = request.get("data")
    if not isinstance(encoded, str):
        return _skipped("invalid_request")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return _skipped("invalid_request")
    if len(data) > MAX_ATTACHMENT_BYTES:
        return _skipped("attachment_size_limit")

    max_chars = _bounded_int(
        request.get("max_chars"),
        default=MAX_OUTPUT_CHARS,
        minimum=1,
        maximum=MAX_OUTPUT_CHARS,
    )
    if kind == "text":
        return _decode_plain(data, max_chars)

    max_pages = _bounded_int(
        request.get("max_pages"),
        default=MAX_PDF_PAGES,
        minimum=1,
        maximum=MAX_PDF_PAGES,
    )
    return _decode_pdf(data, max_pages, max_chars)


def main() -> int:
    _deny_network()
    _apply_resource_limits()
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        response = _skipped("request_size_limit")
    else:
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _skipped("invalid_request")
        else:
            response = handle_request(request)
    serialized = json.dumps(response, separators=(",", ":"), ensure_ascii=True)
    sys.stdout.write(serialized)
    return 0


if __name__ == "__main__":
    # Clear proxy variables and prevent accidental child process proxy discovery.
    for proxy_name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
        "no_proxy",
    ):
        os.environ.pop(proxy_name, None)
    raise SystemExit(main())
