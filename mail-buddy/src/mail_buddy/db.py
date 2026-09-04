from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from mail_buddy.contracts import (
    BackfillState,
    Category,
    ClassificationResult,
    JobKind,
    MessageOrigin,
    MessageState,
    RuleKind,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_lock = threading.Lock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._init_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA synchronous = FULL;

                    CREATE TABLE IF NOT EXISTS app_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS accounts (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        email TEXT,
                        history_id TEXT,
                        status TEXT NOT NULL DEFAULT 'disconnected',
                        connected_at TEXT,
                        last_sync_at TEXT,
                        last_error_code TEXT
                    );

                    CREATE TABLE IF NOT EXISTS label_map (
                        category TEXT PRIMARY KEY,
                        label_name TEXT NOT NULL UNIQUE,
                        label_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS label_aliases (
                        label_id TEXT PRIMARY KEY,
                        category TEXT NOT NULL,
                        label_name TEXT NOT NULL,
                        recorded_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS label_alias_category_idx
                    ON label_aliases(category);

                    CREATE TABLE IF NOT EXISTS messages (
                        message_id TEXT PRIMARY KEY,
                        thread_id TEXT NOT NULL,
                        origin TEXT NOT NULL,
                        internal_date INTEGER NOT NULL DEFAULT 0,
                        sender_key TEXT,
                        state TEXT NOT NULL,
                        primary_category TEXT,
                        alternate_category TEXT,
                        decision_source TEXT,
                        review_required INTEGER NOT NULL DEFAULT 0,
                        reason_codes TEXT NOT NULL DEFAULT '[]',
                        flags TEXT NOT NULL DEFAULT '[]',
                        model TEXT,
                        taxonomy_version TEXT,
                        had_inbox INTEGER,
                        current_app_label_id TEXT,
                        staged_at TEXT,
                        processed_at TEXT,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS messages_state_idx
                    ON messages(state, primary_category);

                    CREATE INDEX IF NOT EXISTS messages_sender_idx
                    ON messages(sender_key);

                    -- Gmail content is stored separately so message metadata can
                    -- continue to be queried without decrypting email text.
                    CREATE TABLE IF NOT EXISTS message_content_cache (
                        message_id TEXT PRIMARY KEY,
                        sender_ciphertext TEXT NOT NULL,
                        subject_ciphertext TEXT NOT NULL,
                        body_ciphertext TEXT NOT NULL,
                        attachment_text_ciphertext TEXT NOT NULL,
                        internal_date INTEGER NOT NULL DEFAULT 0,
                        cached_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS message_content_cache_cached_idx
                    ON message_content_cache(cached_at);

                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT NOT NULL REFERENCES messages(message_id)
                            ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        priority INTEGER NOT NULL DEFAULT 100,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        available_at TEXT NOT NULL,
                        last_error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(message_id, kind)
                    );

                    CREATE INDEX IF NOT EXISTS jobs_claim_idx
                    ON jobs(status, available_at, priority, id);

                    CREATE TABLE IF NOT EXISTS category_approvals (
                        category TEXT NOT NULL,
                        taxonomy_version TEXT NOT NULL,
                        model TEXT NOT NULL,
                        approved_at TEXT NOT NULL,
                        PRIMARY KEY(category, taxonomy_version, model)
                    );

                    CREATE TABLE IF NOT EXISTS rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        pattern_ciphertext TEXT NOT NULL,
                        category TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        batch_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        before_app_label_ids TEXT NOT NULL,
                        before_had_inbox INTEGER NOT NULL,
                        after_category TEXT,
                        created_at TEXT NOT NULL,
                        undo_until TEXT NOT NULL,
                        undone_at TEXT
                    );

                    CREATE INDEX IF NOT EXISTS audit_batch_idx
                    ON audit_log(batch_id, undone_at);

                    CREATE UNIQUE INDEX IF NOT EXISTS audit_operation_idx
                    ON audit_log(batch_id, message_id, action);

                    CREATE TABLE IF NOT EXISTS backfill (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        status TEXT NOT NULL,
                        page_token TEXT,
                        captured_history_id TEXT,
                        total_scanned INTEGER NOT NULL DEFAULT 0,
                        total_staged INTEGER NOT NULL DEFAULT 0,
                        started_at TEXT,
                        completed_at TEXT,
                        last_error_code TEXT
                    );

                    CREATE TABLE IF NOT EXISTS correspondents (
                        address_hash TEXT PRIMARY KEY,
                        two_way INTEGER NOT NULL,
                        checked_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS training_examples (
                        message_id TEXT PRIMARY KEY REFERENCES messages(message_id)
                            ON DELETE CASCADE,
                        sender_key TEXT NOT NULL DEFAULT '',
                        content_ciphertext TEXT NOT NULL,
                        category TEXT NOT NULL,
                        predicted_category TEXT,
                        source TEXT NOT NULL,
                        dataset_version INTEGER NOT NULL DEFAULT 1,
                        evaluation_fold INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS training_examples_category_idx
                    ON training_examples(category);

                    CREATE TABLE IF NOT EXISTS personalized_models (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        artifact_ciphertext TEXT NOT NULL,
                        example_count INTEGER NOT NULL,
                        evaluated_count INTEGER NOT NULL,
                        accuracy REAL NOT NULL,
                        macro_f1 REAL NOT NULL DEFAULT 0,
                        category_recall TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        promoted_at TEXT
                    );

                    CREATE INDEX IF NOT EXISTS personalized_models_status_idx
                    ON personalized_models(status, id DESC);

                    CREATE TABLE IF NOT EXISTS main_models (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        base_model TEXT NOT NULL,
                        artifact_sha256 TEXT NOT NULL UNIQUE,
                        dataset_version INTEGER NOT NULL,
                        example_count INTEGER NOT NULL,
                        accuracy REAL NOT NULL,
                        macro_f1 REAL NOT NULL,
                        category_recall TEXT NOT NULL DEFAULT '{}',
                        laptop_installed INTEGER NOT NULL DEFAULT 0,
                        pi_installed INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        promoted_at TEXT
                    );

                    CREATE INDEX IF NOT EXISTS main_models_status_idx
                    ON main_models(status, id DESC);

                    CREATE TABLE IF NOT EXISTS training_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        dataset_version INTEGER,
                        details TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS training_runs_one_active_idx
                    ON training_runs(kind) WHERE status = 'running';

                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        level TEXT NOT NULL,
                        code TEXT NOT NULL,
                        details TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS events_created_idx
                    ON events(created_at DESC);
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO accounts(id, status)
                    VALUES(1, 'disconnected')
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO backfill(id, status)
                    VALUES(1, ?)
                    """,
                    (BackfillState.IDLE.value,),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO label_aliases(
                        label_id, category, label_name, recorded_at
                    )
                    SELECT label_id, category, label_name, updated_at
                    FROM label_map
                    """
                )
                existing_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(training_examples)").fetchall()
                }
                for name, definition in (
                    ("sender_key", "TEXT NOT NULL DEFAULT ''"),
                    ("dataset_version", "INTEGER NOT NULL DEFAULT 1"),
                    ("evaluation_fold", "INTEGER"),
                ):
                    if name not in existing_columns:
                        connection.execute(
                            f"ALTER TABLE training_examples ADD COLUMN {name} {definition}"
                        )
                model_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(personalized_models)"
                    ).fetchall()
                }
                for name, definition in (
                    ("macro_f1", "REAL NOT NULL DEFAULT 0"),
                    ("category_recall", "TEXT NOT NULL DEFAULT '{}'"),
                ):
                    if name not in model_columns:
                        connection.execute(
                            f"ALTER TABLE personalized_models ADD COLUMN {name} {definition}"
                        )
                connection.execute("PRAGMA user_version = 4")

    def set_setting(self, key: str, value: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_setting(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def delete_setting(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key = ?", (key,))

    def connect_account(self, email: str, history_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE accounts SET email = ?, history_id = ?, status = 'connected',
                    connected_at = ?, last_sync_at = ?, last_error_code = NULL
                WHERE id = 1
                """,
                (email, history_id, now, now),
            )

    def update_history(self, history_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE accounts SET history_id = ?, last_sync_at = ?,
                    status = 'connected', last_error_code = NULL
                WHERE id = 1
                """,
                (history_id, utc_now()),
            )

    def set_account_error(self, code: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE accounts SET status = 'error', last_error_code = ?
                WHERE id = 1
                """,
                (code,),
            )

    def disconnect_account(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE accounts SET email = NULL, history_id = NULL,
                    status = 'disconnected', connected_at = NULL,
                    last_sync_at = NULL, last_error_code = NULL
                WHERE id = 1
                """
            )

    def get_account(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = 1").fetchone()
        return dict(row) if row else {"status": "disconnected"}

    def upsert_label(self, category: str, label_name: str, label_id: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO label_map(category, label_name, label_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(category) DO UPDATE SET
                    label_name = excluded.label_name,
                    label_id = excluded.label_id,
                    updated_at = excluded.updated_at
                """,
                (category, label_name, label_id, now),
            )
            connection.execute(
                """
                INSERT INTO label_aliases(
                    label_id, category, label_name, recorded_at
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(label_id) DO UPDATE SET
                    category = excluded.category,
                    label_name = excluded.label_name
                """,
                (label_id, category, label_name, now),
            )
            connection.commit()

    def get_labels(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT category, label_id FROM label_map").fetchall()
        return {str(row["category"]): str(row["label_id"]) for row in rows}

    def get_label_aliases(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT label_id, category FROM label_aliases").fetchall()
        return {str(row["label_id"]): str(row["category"]) for row in rows}

    def enqueue_message(
        self,
        message_id: str,
        thread_id: str,
        origin: MessageOrigin,
        *,
        internal_date: int = 0,
        priority: int | None = None,
    ) -> bool:
        now = utc_now()
        job_priority = (
            priority if priority is not None else (0 if origin == MessageOrigin.LIVE else 100)
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT origin FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, thread_id, origin, internal_date, state,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    internal_date = MAX(messages.internal_date, excluded.internal_date),
                    origin = CASE
                        WHEN excluded.origin = 'live' THEN 'live'
                        ELSE messages.origin
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    thread_id,
                    origin.value,
                    internal_date,
                    MessageState.QUEUED.value,
                    now,
                    now,
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO jobs(
                    message_id, kind, priority, status, available_at,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(message_id, kind) DO UPDATE SET
                    status = 'pending',
                    attempts = 0,
                    priority = MIN(jobs.priority, excluded.priority),
                    available_at = excluded.available_at,
                    last_error_code = NULL,
                    updated_at = excluded.updated_at
                WHERE jobs.status = 'failed'
                """,
                (
                    message_id,
                    JobKind.CLASSIFY.value,
                    job_priority,
                    now,
                    now,
                    now,
                ),
            )
            if cursor.rowcount > 0 and existing is not None:
                connection.execute(
                    """
                    UPDATE messages SET state = ?, error_code = NULL,
                        updated_at = ?
                    WHERE message_id = ? AND state = ?
                    """,
                    (
                        MessageState.QUEUED.value,
                        now,
                        message_id,
                        MessageState.ERROR.value,
                    ),
                )
            connection.commit()
        return existing is None or cursor.rowcount > 0

    def retry_failed_jobs(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT message_id, kind FROM jobs WHERE status = 'failed'"
            ).fetchall()
            if not rows:
                connection.commit()
                return 0
            connection.execute(
                """
                UPDATE jobs SET status = 'pending', attempts = 0,
                    available_at = ?, last_error_code = NULL, updated_at = ?
                WHERE status = 'failed'
                """,
                (now, now),
            )
            for row in rows:
                state = (
                    MessageState.QUEUED
                    if row["kind"] == JobKind.CLASSIFY.value
                    else MessageState.READY_TO_APPLY
                )
                connection.execute(
                    """
                    UPDATE messages SET state = ?, error_code = NULL,
                        updated_at = ?
                    WHERE message_id = ?
                    """,
                    (state.value, now, row["message_id"]),
                )
            connection.commit()
        return len(rows)

    def enqueue_apply(self, message_id: str, *, priority: int = 20) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    message_id, kind, priority, status, available_at,
                    created_at, updated_at
                )
                VALUES(?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(message_id, kind) DO UPDATE SET
                    status = CASE
                        WHEN jobs.status IN ('done', 'failed') THEN 'pending'
                        ELSE jobs.status
                    END,
                    priority = MIN(jobs.priority, excluded.priority),
                    available_at = excluded.available_at,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    JobKind.APPLY.value,
                    priority,
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE messages SET state = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (MessageState.READY_TO_APPLY.value, now, message_id),
            )

    def reset_stale_jobs(self) -> int:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'pending', available_at = ?, updated_at = ?
                WHERE status = 'running'
                """,
                (now, now),
            )
        return cursor.rowcount

    def claim_job(self) -> dict[str, Any] | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'pending' AND available_at <= ?
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            connection.commit()
        claimed = dict(row)
        claimed["attempts"] = int(claimed["attempts"]) + 1
        return claimed

    def complete_job(self, job_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'done', updated_at = ? WHERE id = ?",
                (utc_now(), job_id),
            )

    def retry_job(self, job_id: int, code: str, delay_seconds: int) -> None:
        available = (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'pending', available_at = ?,
                    last_error_code = ?, updated_at = ?
                WHERE id = ?
                """,
                (available, code, utc_now(), job_id),
            )

    def fail_job(self, job_id: int, message_id: str, code: str) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = 'failed', last_error_code = ?,
                    updated_at = ? WHERE id = ?
                """,
                (code, now, job_id),
            )
            connection.execute(
                """
                UPDATE messages SET state = ?, error_code = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (MessageState.ERROR.value, code, now, message_id),
            )

    def save_decision(
        self,
        message_id: str,
        decision: ClassificationResult,
        *,
        sender_key: str,
        internal_date: int,
        had_inbox: bool,
        state: MessageState,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """
                SELECT origin, staged_at FROM messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE messages SET
                    internal_date = ?, sender_key = ?, state = ?,
                    primary_category = ?, alternate_category = ?,
                    decision_source = ?, review_required = ?,
                    reason_codes = ?, flags = ?, model = ?,
                    taxonomy_version = ?, had_inbox = ?, staged_at = ?,
                    error_code = NULL, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    internal_date,
                    sender_key,
                    state.value,
                    decision.primary_category.value,
                    (decision.alternate_category.value if decision.alternate_category else None),
                    decision.source.value,
                    int(decision.review_required),
                    json.dumps([item.value for item in decision.reason_codes]),
                    json.dumps([item.value for item in decision.flags]),
                    decision.model,
                    decision.taxonomy_version,
                    int(had_inbox),
                    now,
                    now,
                    message_id,
                ),
            )
            first_backfill_decision = (
                previous is not None
                and previous["origin"] == MessageOrigin.BACKFILL.value
                and previous["staged_at"] is None
            )
            if first_backfill_decision:
                connection.execute(
                    """
                    UPDATE backfill SET total_staged = total_staged + 1
                    WHERE id = 1
                    """,
                )
            connection.commit()

    def mark_applied(
        self, message_id: str, category_label_id: str | None, category: Category | None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages SET state = ?, current_app_label_id = ?,
                    primary_category = COALESCE(?, primary_category),
                    review_required = 0, processed_at = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    MessageState.APPLIED.value,
                    category_label_id,
                    category.value if category else None,
                    utc_now(),
                    utc_now(),
                    message_id,
                ),
            )

    def mark_needs_review(self, message_id: str, review_label_id: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE messages SET state = ?, review_required = 1,
                    current_app_label_id = COALESCE(?, current_app_label_id),
                    processed_at = ?, updated_at = ?
                WHERE message_id = ?
                """,
                (
                    MessageState.NEEDS_REVIEW.value,
                    review_label_id,
                    utc_now(),
                    utc_now(),
                    message_id,
                ),
            )

    def mark_gone(self, message_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE messages SET state = ?, updated_at = ? WHERE message_id = ?",
                (MessageState.GONE.value, utc_now(), message_id),
            )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def cache_message_content(
        self,
        *,
        message_id: str,
        sender_ciphertext: str,
        subject_ciphertext: str,
        body_ciphertext: str,
        attachment_text_ciphertext: str,
        internal_date: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO message_content_cache(
                    message_id, sender_ciphertext, subject_ciphertext,
                    body_ciphertext, attachment_text_ciphertext, internal_date,
                    cached_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    sender_ciphertext = excluded.sender_ciphertext,
                    subject_ciphertext = excluded.subject_ciphertext,
                    body_ciphertext = excluded.body_ciphertext,
                    attachment_text_ciphertext = excluded.attachment_text_ciphertext,
                    internal_date = excluded.internal_date,
                    cached_at = excluded.cached_at
                """,
                (
                    message_id,
                    sender_ciphertext,
                    subject_ciphertext,
                    body_ciphertext,
                    attachment_text_ciphertext,
                    internal_date,
                    utc_now(),
                ),
            )

    def get_cached_message_content(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM message_content_cache WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return dict(row) if row else None

    def next_message_without_cached_content(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT m.message_id
                FROM messages AS m
                LEFT JOIN message_content_cache AS c ON c.message_id = m.message_id
                WHERE m.state != ? AND c.message_id IS NULL
                ORDER BY CASE WHEN m.state = ? THEN 0 ELSE 1 END,
                    m.internal_date DESC, m.updated_at DESC
                LIMIT 1
                """,
                (MessageState.GONE.value, MessageState.NEEDS_REVIEW.value),
            ).fetchone()
        return dict(row) if row else None

    def get_content_sync_progress(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(m.message_id) AS total,
                    COUNT(c.message_id) AS cached,
                    MAX(c.cached_at) AS last_cached_at
                FROM messages AS m
                LEFT JOIN message_content_cache AS c ON c.message_id = m.message_id
                WHERE m.state != ?
                """,
                (MessageState.GONE.value,),
            ).fetchone()
        return dict(row) if row else {"total": 0, "cached": 0, "last_cached_at": None}

    def list_messages(
        self,
        *,
        state: MessageState | None = None,
        category: Category | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if state and category:
                rows = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE state = ? AND primary_category = ?
                    ORDER BY internal_date DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (state.value, category.value, limit),
                ).fetchall()
            elif state:
                rows = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE state = ?
                    ORDER BY internal_date DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (state.value, limit),
                ).fetchall()
            elif category:
                rows = connection.execute(
                    """
                    SELECT * FROM messages
                    WHERE primary_category = ?
                    ORDER BY internal_date DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (category.value, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM messages
                    ORDER BY internal_date DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def list_category_samples(self, category: Category, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return staged samples diversified by sender and decision source."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                WITH ranked AS (
                    SELECT messages.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY
                                COALESCE(sender_key, message_id),
                                COALESCE(decision_source, '')
                            ORDER BY internal_date DESC, updated_at DESC
                        ) AS sender_rank
                    FROM messages
                    WHERE state = ? AND primary_category = ?
                )
                SELECT * FROM ranked
                ORDER BY sender_rank ASC, internal_date DESC, updated_at DESC
                LIMIT ?
                """,
                (MessageState.STAGED.value, category.value, limit),
            ).fetchall()
        return [{key: row[key] for key in row.keys() if key != "sender_rank"} for row in rows]

    def get_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM messages GROUP BY state"
            ).fetchall()
            queue = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('pending', 'running')"
            ).fetchone()
        counts = {str(row["state"]): int(row["count"]) for row in rows}
        counts["queue"] = int(queue["count"]) if queue else 0
        return counts

    def approve_category(self, category: Category, taxonomy: str, model: str) -> str:
        now = utc_now()
        batch_id = str(uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO category_approvals(
                    category, taxonomy_version, model, approved_at
                )
                VALUES(?, ?, ?, ?)
                ON CONFLICT(category, taxonomy_version, model) DO UPDATE SET
                    approved_at = excluded.approved_at
                """,
                (category.value, taxonomy, model, now),
            )
            rows = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE state = ? AND primary_category = ?
                    AND taxonomy_version = ? AND COALESCE(model, ?) = ?
                    AND review_required = 0
                """,
                (
                    MessageState.STAGED.value,
                    category.value,
                    taxonomy,
                    model,
                    model,
                ),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        message_id, kind, priority, status, available_at,
                        created_at, updated_at
                    )
                    VALUES(?, ?, 20, 'pending', ?, ?, ?)
                    ON CONFLICT(message_id, kind) DO UPDATE SET
                        status = 'pending', priority = 20, available_at = excluded.available_at,
                        updated_at = excluded.updated_at
                    """,
                    (row["message_id"], JobKind.APPLY.value, now, now, now),
                )
                connection.execute(
                    "UPDATE messages SET state = ?, updated_at = ? WHERE message_id = ?",
                    (
                        MessageState.READY_TO_APPLY.value,
                        now,
                        row["message_id"],
                    ),
                )
            connection.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (f"pending_batch:{category.value}", batch_id, now),
            )
            connection.commit()
        return batch_id

    def is_category_approved(self, category: Category, taxonomy: str, model: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM category_approvals
                WHERE category = ? AND taxonomy_version = ? AND model = ?
                """,
                (category.value, taxonomy, model),
            ).fetchone()
        return bool(row)

    def list_approvals(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM category_approvals ORDER BY category"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_rule(self, kind: RuleKind, pattern_ciphertext: str, category: Category) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO rules(kind, pattern_ciphertext, category, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (kind.value, pattern_ciphertext, category.value, utc_now()),
            )
        return int(cursor.lastrowid)

    def list_rules(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM rules ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def delete_rule(self, rule_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))

    def upsert_training_example(
        self,
        *,
        message_id: str,
        sender_key: str,
        content_ciphertext: str,
        category: Category,
        predicted_category: Category | None,
        source: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO training_examples(
                    message_id, sender_key, content_ciphertext, category,
                    predicted_category, source, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    sender_key = excluded.sender_key,
                    content_ciphertext = excluded.content_ciphertext,
                    category = excluded.category,
                    predicted_category = COALESCE(
                        training_examples.predicted_category,
                        excluded.predicted_category
                    ),
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    sender_key,
                    content_ciphertext,
                    category.value,
                    predicted_category.value if predicted_category else None,
                    source,
                    now,
                    now,
                ),
            )

    def update_training_folds(self, folds: dict[str, int], dataset_version: int) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for message_id, fold in folds.items():
                connection.execute(
                    """
                    UPDATE training_examples
                    SET evaluation_fold = ?, dataset_version = ?, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (fold, dataset_version, utc_now(), message_id),
                )
            connection.commit()

    def list_training_examples(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM training_examples ORDER BY created_at, message_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_accuracy_candidates(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT messages.* FROM messages
                LEFT JOIN training_examples
                    ON training_examples.message_id = messages.message_id
                WHERE training_examples.message_id IS NULL
                    AND messages.state IN (?, ?)
                    AND messages.primary_category IS NOT NULL
                    AND COALESCE(messages.decision_source, '') != ?
                ORDER BY
                    CASE WHEN messages.reason_codes LIKE '%PERSONALIZED_DISAGREEMENT%'
                        THEN 0 ELSE 1 END,
                    messages.updated_at DESC,
                    messages.message_id
                LIMIT ?
                """,
                (
                    MessageState.APPLIED.value,
                    MessageState.STAGED.value,
                    "manual",
                    min(500, max(limit * 20, limit)),
                ),
            ).fetchall()
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            groups.setdefault(str(item["primary_category"]), []).append(item)
        selected: list[dict[str, Any]] = []
        categories = sorted(groups)
        while categories and len(selected) < limit:
            remaining: list[str] = []
            for category in categories:
                items = groups[category]
                if items and len(selected) < limit:
                    # Alternate newest and older examples within each category.
                    selected.append(items.pop(0 if len(selected) % 2 == 0 else -1))
                if items:
                    remaining.append(category)
            categories = remaining
        return selected

    def create_personalized_model(
        self,
        *,
        name: str,
        artifact_ciphertext: str,
        example_count: int,
        evaluated_count: int,
        accuracy: float,
        macro_f1: float,
        category_recall: dict[str, float],
        promote: bool,
    ) -> int:
        now = utc_now()
        status = "active" if promote else "rejected"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if promote:
                connection.execute(
                    "UPDATE personalized_models SET status = 'retired' WHERE status = 'active'"
                )
            cursor = connection.execute(
                """
                INSERT INTO personalized_models(
                    name, artifact_ciphertext, example_count, evaluated_count,
                    accuracy, macro_f1, category_recall, status, created_at, promoted_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    artifact_ciphertext,
                    example_count,
                    evaluated_count,
                    accuracy,
                    macro_f1,
                    json.dumps(category_recall, sort_keys=True),
                    status,
                    now,
                    now if promote else None,
                ),
            )
            connection.commit()
        return int(cursor.lastrowid)

    def get_active_personalized_model(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM personalized_models
                WHERE status = 'active'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def list_personalized_models(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM personalized_models ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_training_run(
        self,
        kind: str,
        *,
        dataset_version: int | None = None,
        stale_after: timedelta = timedelta(hours=12),
    ) -> int | None:
        now = datetime.now(UTC)
        stale = (now - stale_after).isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE training_runs SET status = 'failed', finished_at = ?,
                    details = '{"reason":"stale_lock_recovered"}'
                WHERE kind = ? AND status = 'running' AND started_at < ?
                """,
                (now.isoformat(), kind, stale),
            )
            running = connection.execute(
                "SELECT 1 FROM training_runs WHERE kind = ? AND status = 'running'",
                (kind,),
            ).fetchone()
            if running:
                connection.rollback()
                return None
            cursor = connection.execute(
                """
                INSERT INTO training_runs(kind, status, started_at, dataset_version)
                VALUES(?, 'running', ?, ?)
                """,
                (kind, now.isoformat(), dataset_version),
            )
            connection.commit()
        return int(cursor.lastrowid)

    def finish_training_run(
        self,
        run_id: int,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"complete", "failed", "rejected"}:
            raise ValueError("Invalid training-run status")
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE training_runs SET status = ?, finished_at = ?, details = ?
                WHERE id = ? AND status = 'running'
                """,
                (status, utc_now(), json.dumps(details or {}, sort_keys=True), run_id),
            )

    def get_latest_training_run(self, kind: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM training_runs WHERE kind = ? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    def finish_latest_training_run(
        self,
        kind: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM training_runs WHERE kind = ? AND status = 'running'
                ORDER BY id DESC LIMIT 1
                """,
                (kind,),
            ).fetchone()
        if row:
            self.finish_training_run(int(row["id"]), status, details)

    def recover_stale_training_runs(self, stale_after: timedelta) -> int:
        cutoff = (datetime.now(UTC) - stale_after).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE training_runs SET status = 'failed', finished_at = ?,
                    details = '{"reason":"stale_lock_recovered"}'
                WHERE status = 'running' AND started_at < ?
                """,
                (utc_now(), cutoff),
            )
        return int(cursor.rowcount)

    def register_main_model(
        self,
        *,
        name: str,
        base_model: str,
        artifact_sha256: str,
        dataset_version: int,
        example_count: int,
        accuracy: float,
        macro_f1: float,
        category_recall: dict[str, float],
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO main_models(
                    name, base_model, artifact_sha256, dataset_version,
                    example_count, accuracy, macro_f1, category_recall,
                    status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?)
                """,
                (
                    name,
                    base_model,
                    artifact_sha256,
                    dataset_version,
                    example_count,
                    accuracy,
                    macro_f1,
                    json.dumps(category_recall, sort_keys=True),
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def mark_main_model_installed(self, name: str, host: str) -> None:
        column = {"laptop": "laptop_installed", "pi": "pi_installed"}.get(host)
        if column is None:
            raise ValueError("Unknown model host")
        with self.connect() as connection:
            connection.execute(
                f"UPDATE main_models SET {column} = 1 WHERE name = ?",  # noqa: S608
                (name,),
            )

    def promote_main_model(self, name: str) -> list[str]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                """
                SELECT * FROM main_models WHERE name = ? AND status = 'candidate'
                    AND laptop_installed = 1 AND pi_installed = 1
                """,
                (name,),
            ).fetchone()
            if candidate is None:
                connection.rollback()
                raise ValueError("Candidate is not installed on both hosts")
            active = connection.execute(
                "SELECT * FROM main_models WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active and (
                float(candidate["accuracy"]) < float(active["accuracy"])
                or float(candidate["macro_f1"]) < float(active["macro_f1"])
            ):
                connection.rollback()
                raise ValueError("Candidate metrics regress from the active model")
            connection.execute("UPDATE main_models SET status = 'retired' WHERE status = 'active'")
            connection.execute(
                """
                UPDATE main_models SET status = 'active', promoted_at = ? WHERE name = ?
                """,
                (now, name),
            )
            obsolete = connection.execute(
                """
                SELECT id, name FROM main_models WHERE status = 'retired'
                ORDER BY promoted_at DESC, id DESC LIMIT -1 OFFSET 2
                """
            ).fetchall()
            if obsolete:
                connection.executemany(
                    "DELETE FROM main_models WHERE id = ?",
                    [(row["id"],) for row in obsolete],
                )
            connection.commit()
        return [str(row["name"]) for row in obsolete]

    def get_active_main_model(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM main_models WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_main_models(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM main_models ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def rollback_main_model(self) -> str:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT * FROM main_models WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = connection.execute(
                """
                SELECT * FROM main_models WHERE status = 'retired'
                ORDER BY promoted_at DESC, id DESC LIMIT 1
                """
            ).fetchone()
            if active is None or previous is None:
                connection.rollback()
                raise ValueError("No previous fine-tuned model is available")
            connection.execute(
                "UPDATE main_models SET status = 'retired' WHERE id = ?", (active["id"],)
            )
            connection.execute(
                "UPDATE main_models SET status = 'active', promoted_at = ? WHERE id = ?",
                (utc_now(), previous["id"]),
            )
            connection.commit()
        return str(previous["name"])

    def create_audit(
        self,
        *,
        batch_id: str,
        message_id: str,
        action: str,
        before_app_label_ids: list[str],
        before_had_inbox: bool,
        after_category: Category | None,
    ) -> None:
        created = datetime.now(UTC)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO audit_log(
                    batch_id, message_id, action, before_app_label_ids,
                    before_had_inbox, after_category, created_at, undo_until
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    message_id,
                    action,
                    json.dumps(before_app_label_ids),
                    int(before_had_inbox),
                    after_category.value if after_category else None,
                    created.isoformat(),
                    (created + timedelta(days=90)).isoformat(),
                ),
            )

    def list_audit_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_log
                WHERE batch_id = ? AND undone_at IS NULL AND undo_until >= ?
                ORDER BY id
                """,
                (batch_id, utc_now()),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_audit_undone(self, audit_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE audit_log SET undone_at = ? WHERE id = ?",
                (utc_now(), audit_id),
            )

    def list_recent_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT batch_id, action, after_category, MIN(created_at) AS created_at,
                    COUNT(*) AS message_count,
                    SUM(CASE WHEN undone_at IS NOT NULL THEN 1 ELSE 0 END) AS undone_count,
                    MAX(undo_until) AS undo_until
                FROM audit_log
                GROUP BY batch_id, action, after_category
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_backfill(self, captured_history_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE backfill SET status = ?, page_token = NULL,
                    captured_history_id = ?, total_scanned = 0,
                    total_staged = 0, started_at = ?, completed_at = NULL,
                    last_error_code = NULL
                WHERE id = 1
                """,
                (BackfillState.RUNNING.value, captured_history_id, utc_now()),
            )

    def recover_expired_history(self, history_id: str) -> None:
        """Advance the cursor and durably start its recovery scan atomically."""

        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE accounts SET history_id = ?, last_sync_at = ?,
                    status = 'connected', last_error_code = NULL
                WHERE id = 1
                """,
                (history_id, now),
            )
            connection.execute(
                """
                UPDATE backfill SET status = ?, page_token = NULL,
                    captured_history_id = ?, total_scanned = 0,
                    total_staged = 0, started_at = ?, completed_at = NULL,
                    last_error_code = NULL
                WHERE id = 1
                """,
                (
                    BackfillState.RUNNING.value,
                    history_id,
                    now,
                ),
            )
            connection.commit()

    def set_backfill_status(self, status: BackfillState, *, error_code: str | None = None) -> None:
        completed = utc_now() if status == BackfillState.COMPLETE else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE backfill SET status = ?, completed_at = COALESCE(?, completed_at),
                    last_error_code = ? WHERE id = 1
                """,
                (status.value, completed, error_code),
            )

    def update_backfill_page(self, next_page_token: str | None, scanned_count: int) -> None:
        status = (
            BackfillState.RUNNING.value if next_page_token else BackfillState.SCAN_COMPLETE.value
        )
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE backfill SET page_token = ?, total_scanned = total_scanned + ?,
                    status = ? WHERE id = 1
                """,
                (next_page_token, scanned_count, status),
            )

    def get_backfill(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM backfill WHERE id = 1").fetchone()
        return dict(row) if row else {"status": BackfillState.IDLE.value}

    def get_correspondent(
        self,
        address_hash: str,
        *,
        false_ttl: timedelta = timedelta(days=1),
        true_ttl: timedelta = timedelta(days=7),
    ) -> bool | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT two_way, checked_at FROM correspondents
                WHERE address_hash = ?
                """,
                (address_hash,),
            ).fetchone()
        if not row:
            return None
        two_way = bool(row["two_way"])
        try:
            checked_at = datetime.fromisoformat(str(row["checked_at"]))
        except ValueError:
            return None
        ttl = true_ttl if two_way else false_ttl
        if checked_at < datetime.now(UTC) - ttl:
            return None
        return two_way

    def set_correspondent(self, address_hash: str, two_way: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO correspondents(address_hash, two_way, checked_at)
                VALUES(?, ?, ?)
                ON CONFLICT(address_hash) DO UPDATE SET
                    two_way = excluded.two_way, checked_at = excluded.checked_at
                """,
                (address_hash, int(two_way), utc_now()),
            )

    def add_event(self, level: str, code: str, details: dict[str, Any] | None = None) -> None:
        safe_keys = {
            "accuracy",
            "attempt",
            "batch_id",
            "category",
            "count",
            "job_id",
            "macro_f1",
            "model",
            "reason",
            "status",
        }
        safe_details = {
            key: value
            for key, value in (details or {}).items()
            if key in safe_keys and isinstance(value, bool | int | float | str)
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO events(level, code, details, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (level, code, json.dumps(safe_details), utc_now()),
            )

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM audit_log WHERE created_at < ?",
                (cutoff,),
            )
            connection.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))

    def backup(self, backup_dir: Path, keep: int = 7) -> Path:
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = backup_dir / (
            f"mail_buddy-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        )
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
        backups = sorted(backup_dir.glob("mail_buddy-*.sqlite3"), reverse=True)
        for old_backup in backups[keep:]:
            old_backup.unlink(missing_ok=True)
        return destination

    def purge_account_data(self) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM jobs")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM message_content_cache")
            connection.execute("DELETE FROM label_map")
            connection.execute("DELETE FROM label_aliases")
            connection.execute("DELETE FROM category_approvals")
            connection.execute("DELETE FROM rules")
            connection.execute("DELETE FROM personalized_models")
            connection.execute("DELETE FROM main_models")
            connection.execute("DELETE FROM training_runs")
            connection.execute("DELETE FROM audit_log")
            connection.execute("DELETE FROM correspondents")
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM app_settings")
            connection.execute(
                """
                UPDATE accounts SET email = NULL, history_id = NULL,
                    status = 'disconnected', connected_at = NULL,
                    last_sync_at = NULL, last_error_code = NULL
                WHERE id = 1
                """
            )
            connection.execute(
                """
                UPDATE backfill SET status = ?, page_token = NULL,
                    captured_history_id = NULL, total_scanned = 0,
                    total_staged = 0, started_at = NULL, completed_at = NULL,
                    last_error_code = NULL WHERE id = 1
                """,
                (BackfillState.IDLE.value,),
            )
            connection.commit()
