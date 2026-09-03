from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mail_buddy.config import Settings
from mail_buddy.contracts import (
    BackfillState,
    Category,
    ClassificationResult,
    DashboardStatus,
    DecisionSource,
    MessageOrigin,
    MessageState,
)
from mail_buddy.db import Database
from mail_buddy.security import SecretBox, hash_password
from mail_buddy.web import create_app

PASSWORD = "correct horse battery"


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.started = False
        self.disconnect_error = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def get_status(self) -> DashboardStatus:
        return DashboardStatus(
            connected=False,
            model_available=True,
            backfill_status=BackfillState.IDLE,
            disk_free_bytes=4_000_000_000,
        )

    async def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    async def start_backfill(self) -> None:
        await self._record("start_backfill")

    async def pause_backfill(self) -> None:
        await self._record("pause_backfill")

    async def resume_backfill(self) -> None:
        await self._record("resume_backfill")

    async def approve_category(self, category: Category) -> None:
        await self._record("approve_category", category)

    async def correct_message(
        self, message_id: str, category: Category, scope: str = "message"
    ) -> None:
        await self._record("correct_message", message_id, category, scope=scope)

    async def undo_batch(self, batch_id: str) -> None:
        await self._record("undo_batch", batch_id)

    async def retry_failed_jobs(self) -> None:
        await self._record("retry_failed_jobs")

    async def disconnect(self) -> None:
        await self._record("disconnect")
        if self.disconnect_error:
            raise RuntimeError("simulated safe disconnect failure")

    async def update_college_domains(self, domains: set[str]) -> None:
        await self._record("update_college_domains", domains)

    async def delete_rule(self, rule_id: int) -> None:
        await self._record("delete_rule", rule_id)

    async def get_message_preview(self, message_id: str) -> dict[str, Any]:
        await self._record("get_message_preview", message_id)
        return {
            "sender": "person@example.test",
            "subject": "Private subject",
            "body": "The full message body",
            "attachment_text": "Extracted attachment text",
            "internal_date": 1_700_000_000_000,
        }

    async def update_learning_schedule(
        self, *, enabled: bool, interval_days: int, hour_local: int
    ) -> None:
        await self._record(
            "update_learning_schedule",
            enabled=enabled,
            interval_days=interval_days,
            hour_local=hour_local,
        )

    async def train_personalized_model(self, *, force: bool = False) -> None:
        await self._record("train_personalized_model", force=force)

    async def submit_accuracy_label(self, message_id: str, category: Category) -> None:
        await self._record("submit_accuracy_label", message_id, category)

    async def rollback_main_model(self) -> str:
        await self._record("rollback_main_model")
        return "mail-buddy-llama:20260901T020000Z-aaaaaa"


def _settings(tmp_path: Path, *, attempts: int = 5) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        backup_dir=tmp_path / "backups",
        encryption_key=SecretBox.generate_key(),
        encryption_key_file=None,
        session_secret="test-session-secret-" * 4,
        session_secret_file=None,
        password_hash=hash_password(PASSWORD),
        password_hash_file=None,
        secure_cookies=False,
        demo_mode=True,
        disable_worker=True,
        login_attempts=attempts,
    )


def _csrf(response: Any) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    page = client.get("/login")
    token = _csrf(page)
    response = client.post(
        "/login",
        data={"password": PASSWORD, "csrf_token": token, "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/"))


def _build(tmp_path: Path, *, attempts: int = 5) -> tuple[Any, Database, FakeService]:
    settings = _settings(tmp_path, attempts=attempts)
    database = Database(settings.database_path)
    service = FakeService()
    return (
        create_app(settings=settings, database=database, service=service),
        database,
        service,
    )


def test_login_protects_routes_and_sets_security_headers(tmp_path: Path) -> None:
    app, _, service = _build(tmp_path)
    with TestClient(app) as client:
        protected = client.get("/", follow_redirects=False)
        assert protected.status_code == 303
        assert protected.headers["location"].startswith("/login")

        page = client.get("/login")
        token = _csrf(page)
        rejected = client.post(
            "/login",
            data={"password": "not the password", "csrf_token": token},
        )
        assert rejected.status_code == 401

        token = _csrf(rejected)
        accepted = client.post(
            "/login",
            data={"password": PASSWORD, "csrf_token": token},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Your inbox, at a glance" in dashboard.text
        assert dashboard.headers["x-frame-options"] == "SAMEORIGIN"
        assert service.started
    assert not service.started


def test_home_assistant_ingress_prefix_routes_and_generates_prefixed_urls(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    prefix = "/api/hassio_ingress/example-token"
    headers = {"x-ingress-path": prefix}
    with TestClient(app) as client:
        protected = client.get(f"{prefix}/", headers=headers, follow_redirects=False)
        assert protected.status_code == 303
        assert protected.headers["location"] == f"{prefix}/login?next=/"

        login = client.get(f"{prefix}/login", headers=headers)
        assert login.status_code == 200
        assert f'href="{prefix}/static/app.css"' in login.text
        stylesheet = client.get(f"{prefix}/static/app.css", headers=headers)
        assert stylesheet.status_code == 200
        assert "--ink:" in stylesheet.text
        token = _csrf(login)
        accepted = client.post(
            f"{prefix}/login",
            headers=headers,
            data={"password": PASSWORD, "csrf_token": token, "next": "/"},
            follow_redirects=False,
        )
        assert accepted.headers["location"] == f"{prefix}/"

        dashboard = client.get(f"{prefix}/", headers=headers)
        assert dashboard.status_code == 200
        assert f'href="{prefix}/backfill"' in dashboard.text
        assert "frame-ancestors 'self'" in dashboard.headers["content-security-policy"]


def test_login_limiter_allows_five_failures_then_throttles(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path, attempts=5)
    with TestClient(app) as client:
        for _ in range(5):
            page = client.get("/login")
            response = client.post(
                "/login",
                data={"password": "incorrect value", "csrf_token": _csrf(page)},
            )
            assert response.status_code == 401
        page = client.get("/login")
        blocked = client.post(
            "/login",
            data={"password": PASSWORD, "csrf_token": _csrf(page)},
        )
        assert blocked.status_code == 429


def test_csrf_is_required_for_every_mutating_action(tmp_path: Path) -> None:
    app, _, service = _build(tmp_path)
    with TestClient(app) as client:
        _login(client)
        response = client.post("/backfill/start", data={}, follow_redirects=False)
        assert response.status_code == 403
        assert service.calls == []

        page = client.get("/backfill")
        response = client.post(
            "/backfill/start",
            data={"csrf_token": _csrf(page)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert service.calls[-1][0] == "start_backfill"


def test_protected_pages_status_and_actions(tmp_path: Path) -> None:
    app, database, service = _build(tmp_path)
    with TestClient(app) as client:
        database.enqueue_message(
            "approval-sample",
            "approval-thread",
            MessageOrigin.BACKFILL,
        )
        database.save_decision(
            "approval-sample",
            ClassificationResult(
                primary_category=Category.COLLEGE_PLACEMENT,
                source=DecisionSource.RULE,
            ),
            sender_key="sample-sender",
            internal_date=1,
            had_inbox=True,
            state=MessageState.STAGED,
        )
        assert client.get("/api/status").status_code == 401
        token = _login(client)
        for path in ("/backfill", "/review", "/learning", "/settings", "/activity"):
            response = client.get(path)
            assert response.status_code == 200

        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["model_available"] is True

        approved = client.post(
            f"/categories/{Category.COLLEGE_PLACEMENT.value}/approve",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert approved.status_code == 303
        assert service.calls[-1][0] == "approve_category"

        corrected = client.post(
            "/review/message-1/correct",
            data={
                "csrf_token": token,
                "category": Category.DIRECT_PERSONAL.value,
                "scope": "similar",
            },
            follow_redirects=False,
        )
        assert corrected.status_code == 303
        assert service.calls[-1] == (
            "correct_message",
            ("message-1", Category.DIRECT_PERSONAL),
            {"scope": "similar"},
        )

        accuracy_answer = client.post(
            "/learning/approval-sample/label",
            data={
                "csrf_token": token,
                "category": Category.COLLEGE_PLACEMENT.value,
            },
            follow_redirects=False,
        )
        assert accuracy_answer.status_code == 303
        assert service.calls[-1] == (
            "submit_accuracy_label",
            ("approval-sample", Category.COLLEGE_PLACEMENT),
            {},
        )

        schedule = client.post(
            "/learning/schedule",
            data={
                "csrf_token": token,
                "enabled": "true",
                "interval_days": "7",
                "hour_local": "3",
            },
            follow_redirects=False,
        )
        assert schedule.status_code == 303
        assert service.calls[-1] == (
            "update_learning_schedule",
            (),
            {"enabled": True, "interval_days": 7, "hour_local": 3},
        )

        trained = client.post(
            "/learning/train",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert trained.status_code == 303
        assert service.calls[-1] == (
            "train_personalized_model",
            (),
            {"force": True},
        )

        rollback = client.post(
            "/learning/rollback",
            data={"csrf_token": token, "confirmation": "ROLLBACK"},
            follow_redirects=False,
        )
        assert rollback.status_code == 303
        assert service.calls[-1][0] == "rollback_main_model"

        domains = client.post(
            "/settings/college-domains",
            data={"csrf_token": token, "domains": "College.edu, placements.college.edu"},
            follow_redirects=False,
        )
        assert domains.status_code == 303
        assert service.calls[-1][1] == ({"college.edu", "placements.college.edu"},)

        retried = client.post(
            "/jobs/retry",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert service.calls[-1][0] == "retry_failed_jobs"


def test_category_without_a_staged_sample_cannot_be_approved(tmp_path: Path) -> None:
    app, _, service = _build(tmp_path)
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            f"/categories/{Category.SECURITY_OTP.value}/approve",
            data={"csrf_token": token},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/backfill"
        assert not any(call[0] == "approve_category" for call in service.calls)


def test_login_rejects_backslash_open_redirect(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "password": PASSWORD,
                "csrf_token": _csrf(page),
                "next": "/\\evil.example",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_failed_disconnect_keeps_session_and_surfaces_retry(tmp_path: Path) -> None:
    app, _, service = _build(tmp_path)
    service.disconnect_error = True
    with TestClient(app) as client:
        token = _login(client)
        response = client.post(
            "/disconnect",
            data={"csrf_token": token, "confirmation": "DISCONNECT"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "retained so you can retry safely" in settings_page.text


def test_preview_is_on_demand_and_requires_known_message(tmp_path: Path) -> None:
    app, database, service = _build(tmp_path)
    with TestClient(app) as client:
        database.enqueue_message(
            "gmail-message-1",
            "thread-1",
            MessageOrigin.BACKFILL,
        )
        _login(client)
        missing = client.get("/api/messages/unknown/preview")
        assert missing.status_code == 404
        assert not any(call[0] == "get_message_preview" for call in service.calls)

        response = client.get("/api/messages/gmail-message-1/preview")
        assert response.status_code == 200
        assert response.json()["subject"] == "Private subject"
        assert response.json()["body"] == "The full message body"
        assert response.json()["attachment_text"] == "Extracted attachment text"
        assert response.json()["content"] == "The full message body"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["gmail_url"].endswith("/gmail-message-1")


def test_health_is_public_but_readiness_checks_local_sqlite(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["database"] == "ok"
