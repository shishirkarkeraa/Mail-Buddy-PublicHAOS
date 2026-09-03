from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:otp|code)\D{0,12}\d{4,8}\b"), "[REDACTED_OTP]"),
    (
        re.compile(r"(?i)https?://\S*(?:reset|recover|verify|token)\S*"),
        "[REDACTED_SENSITIVE_URL]",
    ),
    (re.compile(r"\b\d{12,19}\b"), "[REDACTED_NUMBER]"),
    (re.compile(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b"), "[REDACTED_EMAIL]"),
)


class SecretBox:
    def __init__(self, key: str | bytes) -> None:
        encoded = key.encode("ascii") if isinstance(key, str) else key
        self._fernet = Fernet(encoded)
        self._hmac_key = hashlib.sha256(encoded + b":mail-buddy-hmac").digest()

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Encrypted data could not be decrypted") from exc

    def fingerprint(self, value: str) -> str:
        normalized = value.strip().lower().encode("utf-8")
        return hmac.new(self._hmac_key, normalized, hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Dashboard password must contain at least 12 characters")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, candidate)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = tuple(redact_text(str(arg)) for arg in record.args)
        return True


@dataclass
class LoginLimiter:
    max_attempts: int
    window_seconds: int
    _attempts: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._attempts[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket) < self.max_attempts

    def record_failure(self, key: str) -> None:
        self._attempts[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)
