from __future__ import annotations

import pytest

from mail_buddy.security import (
    LoginLimiter,
    SecretBox,
    hash_password,
    redact_text,
    verify_password,
)


def test_secret_box_round_trip_and_stable_fingerprint() -> None:
    box = SecretBox(SecretBox.generate_key())
    encrypted = box.encrypt("sensitive value")

    assert encrypted != "sensitive value"
    assert box.decrypt(encrypted) == "sensitive value"
    assert box.fingerprint(" User@Example.com ") == box.fingerprint("user@example.com")


def test_password_hashing() -> None:
    password_hash = hash_password("a-long-private-password")

    assert verify_password(password_hash, "a-long-private-password")
    assert not verify_password(password_hash, "wrong-password")
    with pytest.raises(ValueError, match="at least 12"):
        hash_password("too-short")


def test_redaction_hides_sensitive_values() -> None:
    value = (
        "OTP code 748221. Reset at https://example.com/reset?token=abc "
        "for user@example.com and card 4111111111111111"
    )
    result = redact_text(value)

    assert "748221" not in result
    assert "token=abc" not in result
    assert "user@example.com" not in result
    assert "4111111111111111" not in result


def test_login_limiter_blocks_after_configured_failures() -> None:
    limiter = LoginLimiter(max_attempts=2, window_seconds=60)
    assert limiter.is_allowed("127.0.0.1")
    limiter.record_failure("127.0.0.1")
    assert limiter.is_allowed("127.0.0.1")
    limiter.record_failure("127.0.0.1")
    assert not limiter.is_allowed("127.0.0.1")
    limiter.reset("127.0.0.1")
    assert limiter.is_allowed("127.0.0.1")
