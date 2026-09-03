from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import secrets
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from mail_buddy.config import Settings, get_settings
from mail_buddy.contracts import (
    CATEGORY_LABELS,
    TAXONOMY_VERSION,
    BackfillState,
    Category,
    DashboardStatus,
    MessageState,
)
from mail_buddy.db import Database
from mail_buddy.security import LoginLimiter, redact_text, verify_password

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
LOGIN_CSRF_MAX_AGE_SECONDS = 15 * 60
_DASHBOARD_SECTIONS = {"/backfill", "/review", "/learning", "/activity", "/settings"}


class MailBuddyService(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def get_status(self) -> DashboardStatus: ...

    async def start_backfill(self) -> Any: ...

    async def pause_backfill(self) -> Any: ...

    async def resume_backfill(self) -> Any: ...

    async def approve_category(self, category: Category) -> Any: ...

    async def correct_message(
        self, message_id: str, category: Category, scope: str = "message"
    ) -> Any: ...

    async def submit_accuracy_label(self, message_id: str, category: Category) -> Any: ...

    async def update_learning_schedule(
        self, *, enabled: bool, interval_days: int, hour_local: int
    ) -> Any: ...

    async def train_personalized_model(self, *, force: bool = False) -> Any: ...

    async def rollback_main_model(self) -> Any: ...

    def get_learning_status(self) -> dict[str, Any]: ...

    async def undo_batch(self, batch_id: str) -> Any: ...

    async def retry_failed_jobs(self) -> Any: ...

    async def disconnect(self) -> Any: ...


def _default_service(settings: Settings, database: Database) -> MailBuddyService:
    try:
        from mail_buddy.service import MailBuddyService as Service
    except ImportError as exc:  # pragma: no cover - catches broken production images
        raise RuntimeError("Mail-Buddy service module is unavailable") from exc
    return Service(settings=settings, database=database)


async def _invoke(target: object, method_name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(target, method_name, None)
    if method is None:
        raise RuntimeError(f"Service operation is unavailable: {method_name}")
    signature = inspect.signature(method)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supported = kwargs
    if not accepts_kwargs:
        supported = {key: value for key, value in kwargs.items() if key in signature.parameters}
    result = method(*args, **supported)
    return await result if inspect.isawaitable(result) else result


def _client_ip(request: Request) -> str:
    # The Compose deployment exposes only Caddy. Caddy normalizes X-Forwarded-For;
    # direct development requests fall back to Starlette's client address.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _safe_next(value: str | None) -> str:
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and not any(ord(character) < 32 for character in value)
    ):
        return value
    return "/"


def _ingress_prefix(request: Request) -> str:
    """Return Home Assistant's trusted, path-only Ingress prefix when present."""
    value = request.headers.get("x-ingress-path", "").strip().rstrip("/")
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return ""
    return value


def _ingress_url(request: Request, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("Mail-Buddy paths must be absolute")
    return f"{_ingress_prefix(request)}{path}"


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _login_csrf_token(request: Request) -> str:
    """Return a short-lived login token that survives a hostname change.

    Browser cookies are deliberately host-specific. Home Assistant may be
    reached through a LAN hostname, an IP address, or Tailscale, so the login
    form cannot require a pre-existing cookie from the same host. The token is
    still authenticated with the persistent application session secret.
    """
    signer: TimestampSigner = request.app.state.login_csrf_signer
    return signer.sign(secrets.token_urlsafe(32)).decode("ascii")


async def _require_csrf(request: Request) -> None:
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied:
        form = await request.form()
        value = form.get("csrf_token", "")
        supplied = value if isinstance(value, str) else ""
    expected = request.session.get("csrf_token", "")
    if (
        not isinstance(expected, str)
        or not expected
        or not supplied
        or not hmac.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="The form expired. Refresh and try again.")


async def _require_login_csrf(request: Request) -> None:
    form = await request.form()
    value = form.get("csrf_token", "")
    supplied = value if isinstance(value, str) else ""
    if not supplied:
        raise HTTPException(status_code=403, detail="The form expired. Refresh and try again.")
    signer: TimestampSigner = request.app.state.login_csrf_signer
    try:
        signer.unsign(supplied, max_age=LOGIN_CSRF_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(
            status_code=403, detail="The form expired. Refresh and try again."
        ) from exc


def _require_login(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        destination = quote(request.url.path, safe="/")
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={destination}"},
        )


def _require_api_login(request: Request) -> None:
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=401, detail="Authentication required")


def _flash(request: Request, message: str, tone: str = "info") -> None:
    request.session["flash"] = {
        "message": redact_text(message)[:240],
        "tone": tone if tone in {"info", "success", "warning", "danger"} else "info",
    }


def _remember_dashboard_section(request: Request, path: str) -> None:
    if path in _DASHBOARD_SECTIONS:
        request.session["last_dashboard_path"] = path


def _category_name(value: str | Category | None) -> str:
    if not value:
        return "Unclassified"
    try:
        category = value if isinstance(value, Category) else Category(value)
    except ValueError:
        return str(value).replace("_", " ").title()
    return CATEGORY_LABELS[category]


def _format_time(value: str | int | None) -> str:
    if value is None or value == "":
        return "Not yet"
    try:
        if isinstance(value, int) or str(value).isdigit():
            raw = int(value)
            if raw > 10_000_000_000:
                raw //= 1000
            parsed = datetime.fromtimestamp(raw, tz=UTC)
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d %b %Y, %H:%M")
    except (OSError, ValueError):
        return "Unavailable"


def _format_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


def _masked_message_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:10].upper()


def _decode_list(value: object) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


_EVENT_DETAIL_KEYS = {
    "attempt",
    "backfill_status",
    "batch_id",
    "category",
    "count",
    "delay_seconds",
    "error_code",
    "job_kind",
    "message_count",
    "model",
    "reason_code",
    "source",
    "state",
}


def _event_details(value: object) -> str:
    try:
        decoded = json.loads(str(value)) if value else {}
    except (TypeError, ValueError):
        return "Details withheld"
    if not isinstance(decoded, dict):
        return "Details withheld"
    safe = {
        str(key): redact_text(str(item))[:80]
        for key, item in decoded.items()
        if str(key) in _EVENT_DETAIL_KEYS
    }
    return " · ".join(f"{key.replace('_', ' ')}: {item}" for key, item in safe.items())


def _base_context(request: Request, *, title: str, active: str) -> dict[str, Any]:
    return {
        "request": request,
        "title": title,
        "active": active,
        "csrf_token": _csrf_token(request),
        "flash": request.session.pop("flash", None),
        "category_name": _category_name,
        "format_time": _format_time,
        "format_bytes": _format_bytes,
        "category_options": [(category.value, _category_name(category)) for category in Category],
        "ingress_url": lambda path: _ingress_url(request, path),
    }


def _database_status(database: Database, settings: Settings) -> DashboardStatus:
    account = database.get_account()
    counts = database.get_counts()
    backfill = database.get_backfill()
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        free = 0
    try:
        state = BackfillState(backfill.get("status", BackfillState.IDLE.value))
    except ValueError:
        state = BackfillState.ERROR
    return DashboardStatus(
        connected=account.get("status") == "connected",
        account_email=account.get("email"),
        account_status=str(account.get("status", "disconnected")),
        last_sync_at=account.get("last_sync_at"),
        history_id=account.get("history_id"),
        model_available=False,
        model_name=settings.ollama_model,
        queue_depth=counts.get("queue", 0),
        review_count=counts.get(MessageState.NEEDS_REVIEW.value, 0),
        staged_count=counts.get(MessageState.STAGED.value, 0),
        applied_count=counts.get(MessageState.APPLIED.value, 0),
        backfill_status=state,
        backfill_scanned=int(backfill.get("total_scanned") or 0),
        backfill_staged=int(backfill.get("total_staged") or 0),
        disk_free_bytes=free,
    )


async def _get_status(
    service: MailBuddyService | None, database: Database, settings: Settings
) -> DashboardStatus:
    if service is not None:
        try:
            value = await _invoke(service, "get_status")
            return (
                value
                if isinstance(value, DashboardStatus)
                else DashboardStatus.model_validate(value)
            )
        except Exception:
            logger.warning("STATUS_UNAVAILABLE")
    return _database_status(database, settings)


def _category_rows(database: Database, settings: Settings) -> list[dict[str, Any]]:
    approvals = {
        (row["category"], row["taxonomy_version"], row["model"])
        for row in database.list_approvals()
    }
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT primary_category AS category,
                SUM(CASE WHEN state = 'staged' THEN 1 ELSE 0 END) AS staged_count,
                COUNT(*) AS total_count,
                COUNT(DISTINCT COALESCE(sender_key, '')) AS sender_count
            FROM messages
            WHERE primary_category IS NOT NULL
            GROUP BY primary_category
            """
        ).fetchall()
    counts = {str(row["category"]): dict(row) for row in rows}
    result: list[dict[str, Any]] = []
    for category in Category:
        item = counts.get(category.value, {})
        samples = database.list_category_samples(
            category,
            limit=settings.sample_size,
        )
        result.append(
            {
                "category": category,
                "name": _category_name(category),
                "label": CATEGORY_LABELS[category],
                "staged_count": int(item.get("staged_count") or 0),
                "total_count": int(item.get("total_count") or 0),
                "sender_count": int(item.get("sender_count") or 0),
                "approved": (
                    category.value,
                    TAXONOMY_VERSION,
                    settings.ollama_model,
                )
                in approvals,
                "samples": [
                    {
                        **message,
                        "display_id": _masked_message_id(str(message["message_id"])),
                        "reason_list": _decode_list(message.get("reason_codes")),
                        "flag_list": _decode_list(message.get("flags")),
                    }
                    for message in samples
                ],
            }
        )
    return result


def _review_messages(database: Database, limit: int = 100) -> list[dict[str, Any]]:
    return [
        {
            **message,
            "display_id": _masked_message_id(str(message["message_id"])),
            "reason_list": _decode_list(message.get("reason_codes")),
            "flag_list": _decode_list(message.get("flags")),
        }
        for message in database.list_messages(state=MessageState.NEEDS_REVIEW, limit=limit)
    ]


def _rules(database: Database) -> list[dict[str, Any]]:
    # Deliberately omit pattern_ciphertext. Even encrypted values do not belong in HTML.
    return [
        {
            "id": int(rule["id"]),
            "kind": str(rule["kind"]),
            "category": str(rule["category"]),
            "enabled": bool(rule["enabled"]),
            "created_at": rule["created_at"],
        }
        for rule in database.list_rules()
    ]


async def _learning_status(service: MailBuddyService | None, database: Database) -> dict[str, Any]:
    if service is not None and hasattr(service, "get_learning_status"):
        try:
            result = await _invoke(service, "get_learning_status")
            if isinstance(result, dict):
                return result
        except Exception:
            logger.warning("LEARNING_STATUS_UNAVAILABLE")
    active = database.get_active_personalized_model()
    return {
        "enabled": database.get_setting("learning_enabled") == "true",
        "interval_days": int(database.get_setting("training_interval_days") or 7),
        "hour_local": int(database.get_setting("training_hour_local") or 2),
        "timezone": "local time",
        "last_training_at": database.get_setting("last_training_at"),
        "example_count": len(database.list_training_examples()),
        "candidate_count": len(database.list_accuracy_candidates(limit=100)),
        "active_model": active,
        "models": database.list_personalized_models(limit=10),
        "active_main_model": database.get_active_main_model(),
        "main_models": database.list_main_models(limit=10),
        "category_counts": {},
        "companion_ready": False,
        "lora_ready": False,
        "companion_run": database.get_latest_training_run("companion"),
        "lora_run": database.get_latest_training_run("lora"),
        "next_eligible_at": None,
        "lora_due": False,
        "current_phase": "unavailable",
        "failure_reason": None,
        "lora_split_ready": False,
    }


def _accuracy_candidates(database: Database, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {
            **message,
            "display_id": _masked_message_id(str(message["message_id"])),
        }
        for message in database.list_accuracy_candidates(limit=limit)
    ]


def _events(database: Database) -> list[dict[str, Any]]:
    return [
        {
            "level": str(event["level"]).lower(),
            "code": str(event["code"]),
            "details": _event_details(event.get("details")),
            "created_at": event["created_at"],
        }
        for event in database.list_events(limit=50)
    ]


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    service: MailBuddyService | None = None,
    service_factory: Callable[[Settings, Database], MailBuddyService] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_path)
    ephemeral_session_secret = secrets.token_urlsafe(48)
    session_secret = settings.resolved_session_secret or ephemeral_session_secret
    limiter = LoginLimiter(
        max_attempts=settings.login_attempts,
        window_seconds=settings.login_window_seconds,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        settings.ensure_directories()
        settings.validate_runtime_secrets()
        database.initialize()
        active_service = service
        if active_service is None and not settings.disable_worker:
            factory = service_factory or _default_service
            active_service = factory(settings, database)
        application.state.service = active_service
        if active_service is not None:
            await _invoke(active_service, "start")
        try:
            yield
        finally:
            if active_service is not None:
                await _invoke(active_service, "stop")

    application = FastAPI(
        title="Mail-Buddy",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.database = database
    application.state.service = service
    application.state.login_csrf_signer = TimestampSigner(
        session_secret, salt="mail-buddy-login-csrf"
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="mail_buddy_session",
        max_age=settings.session_max_age_seconds,
        same_site="strict",
        https_only=settings.secure_cookies,
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )

    @application.middleware("http")
    async def home_assistant_ingress(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        """Make HA Ingress's path prefix transparent to FastAPI and Jinja.

        Home Assistant preserves the Ingress path and sends it in
        ``X-Ingress-Path``. FastAPI routes must see the remaining application
        path, while generated URLs and redirects must retain the prefix. Do not
        set Starlette's ``root_path``: StaticFiles treats it as part of the
        already-stripped route and otherwise returns 404 for CSS and JavaScript.
        """
        prefix = _ingress_prefix(request)
        if prefix:
            scope = request.scope
            path = str(scope.get("path", "/"))
            if path == prefix:
                scope["path"] = "/"
            elif path.startswith(f"{prefix}/"):
                scope["path"] = path[len(prefix) :]
        response = await call_next(request)
        location = response.headers.get("location")
        if prefix and location and location.startswith("/") and not location.startswith("//"):
            response.headers["location"] = f"{prefix}{location}"
        return response

    @application.middleware("http")
    async def security_headers(request: Request, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; form-action 'self'; frame-ancestors 'self'; "
            "base-uri 'none'; object-src 'none'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Home Assistant Ingress embeds the app in a same-origin iframe.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @application.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        try:
            with database.connect() as connection:
                connection.execute("SELECT 1").fetchone()
        except Exception:
            return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
        return JSONResponse(
            {
                "status": "ready",
                "database": "ok",
                "worker": "disabled" if request.app.state.service is None else "running",
            }
        )

    @application.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/") -> Response:
        if request.session.get("authenticated") is True:
            return RedirectResponse(_safe_next(next), status_code=303)
        context = _base_context(request, title="Sign in", active="")
        context["csrf_token"] = _login_csrf_token(request)
        context["next"] = _safe_next(next)
        context["setup_required"] = not bool(settings.resolved_password_hash)
        return TEMPLATES.TemplateResponse(request=request, name="login.html", context=context)

    @application.post("/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        password: str = Form(""),
        next: str = Form("/"),
        _: None = Depends(_require_login_csrf),
    ) -> Response:
        ip = _client_ip(request)
        if not limiter.is_allowed(ip):
            context = _base_context(request, title="Sign in", active="")
            context["csrf_token"] = _login_csrf_token(request)
            context.update(
                {
                    "next": _safe_next(next),
                    "error": "Too many attempts. Wait five minutes and try again.",
                    "setup_required": False,
                }
            )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context=context,
                status_code=429,
            )
        password_hash = settings.resolved_password_hash
        if not password_hash or not verify_password(password_hash, password):
            limiter.record_failure(ip)
            context = _base_context(request, title="Sign in", active="")
            context["csrf_token"] = _login_csrf_token(request)
            context.update(
                {
                    "next": _safe_next(next),
                    "error": "That password did not match.",
                    "setup_required": not bool(password_hash),
                }
            )
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context=context,
                status_code=401,
            )
        limiter.reset(ip)
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        request.session["signed_in_at"] = datetime.now(UTC).isoformat()
        return RedirectResponse(_safe_next(next), status_code=303)

    @application.post("/logout")
    async def logout(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        request.session.clear()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("mail_buddy_session", path="/")
        return response

    @application.get("/", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        overview: bool = False,
        _: None = Depends(_require_login),
    ) -> Response:
        last_path = request.session.get("last_dashboard_path")
        if not overview and isinstance(last_path, str) and last_path in _DASHBOARD_SECTIONS:
            return RedirectResponse(_ingress_url(request, last_path), status_code=303)
        request.session["last_dashboard_path"] = "/"
        status = await _get_status(request.app.state.service, database, settings)
        learning = await _learning_status(request.app.state.service, database)
        context = _base_context(request, title="Overview", active="dashboard")
        context.update(
            {
                "status": status,
                "events": _events(database)[:6],
                "batches": database.list_recent_batches(limit=4),
                "learning": learning,
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="dashboard.html", context=context)

    @application.get("/api/status")
    async def api_status(request: Request, _: None = Depends(_require_api_login)) -> JSONResponse:
        status = await _get_status(request.app.state.service, database, settings)
        return JSONResponse(status.model_dump(mode="json"))

    @application.get("/api/messages/{message_id}/preview")
    async def message_preview(
        message_id: str,
        request: Request,
        _: None = Depends(_require_api_login),
    ) -> JSONResponse:
        if database.get_message(message_id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        try:
            preview = await _invoke(request.app.state.service, "get_message_preview", message_id)
        except Exception as exc:
            logger.warning("MESSAGE_PREVIEW_UNAVAILABLE")
            raise HTTPException(
                status_code=503,
                detail="Preview is unavailable. Check the Gmail connection.",
            ) from exc
        if not isinstance(preview, dict):
            raise HTTPException(status_code=502, detail="Preview response was invalid")
        message_content = {
            "sender": str(preview.get("sender", "")),
            "subject": str(preview.get("subject", "")),
            "body": str(preview.get("body", preview.get("content", preview.get("snippet", "")))),
            "attachment_text": str(preview.get("attachment_text", "")),
            "content": str(preview.get("body", preview.get("content", preview.get("snippet", "")))),
            "internal_date": preview.get("internal_date"),
            "gmail_url": f"https://mail.google.com/mail/u/0/#all/{message_id}",
        }
        return JSONResponse(message_content)

    @application.get("/backfill", response_class=HTMLResponse)
    async def backfill_page(request: Request, _: None = Depends(_require_login)) -> Response:
        _remember_dashboard_section(request, "/backfill")
        status = await _get_status(request.app.state.service, database, settings)
        context = _base_context(request, title="Mailbox backfill", active="backfill")
        context.update({"status": status, "categories": _category_rows(database, settings)})
        return TEMPLATES.TemplateResponse(request=request, name="backfill.html", context=context)

    async def run_operation(
        request: Request,
        operation: str,
        success: str,
        redirect_to: str,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        try:
            await _invoke(request.app.state.service, operation, *args, **kwargs)
            _flash(request, success, "success")
        except Exception:
            logger.warning("WEB_OPERATION_FAILED operation=%s", operation)
            _flash(
                request,
                "The operation could not be completed. Check Activity for a safe error code.",
                "danger",
            )
        return RedirectResponse(redirect_to, status_code=303)

    @application.post("/backfill/start")
    async def start_backfill(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request,
            "start_backfill",
            "Historical scan started. Gmail will not change before category approval.",
            "/backfill",
        )

    @application.post("/backfill/pause")
    async def pause_backfill(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request, "pause_backfill", "Historical scan paused.", "/backfill"
        )

    @application.post("/backfill/resume")
    async def resume_backfill(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request, "resume_backfill", "Historical scan resumed.", "/backfill"
        )

    @application.post("/categories/{category_value}/approve")
    async def approve_category(
        category_value: str,
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        try:
            category = Category(category_value)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Unknown category") from exc
        if not database.list_category_samples(category, limit=1):
            _flash(
                request,
                "This category has no staged sample to review, so it was not approved.",
                "warning",
            )
            return RedirectResponse("/backfill", status_code=303)
        return await run_operation(
            request,
            "approve_category",
            f"{_category_name(category)} approved for this taxonomy and model.",
            "/backfill",
            category,
        )

    @application.get("/review", response_class=HTMLResponse)
    async def review_page(request: Request, _: None = Depends(_require_login)) -> Response:
        _remember_dashboard_section(request, "/review")
        context = _base_context(request, title="Needs review", active="review")
        context["messages"] = _review_messages(database)
        return TEMPLATES.TemplateResponse(request=request, name="review.html", context=context)

    @application.post("/review/{message_id}/correct")
    async def correct_message(
        message_id: str,
        request: Request,
        category: str = Form(...),
        scope: str = Form("message"),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        try:
            selected = Category(category)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Unknown category") from exc
        if scope not in {"message", "similar", "sender"}:
            raise HTTPException(status_code=422, detail="Unknown correction scope")
        return await run_operation(
            request,
            "correct_message",
            "Correction saved. The message is queued for safe application.",
            "/review",
            message_id,
            selected,
            scope=scope,
        )

    @application.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, _: None = Depends(_require_login)) -> Response:
        _remember_dashboard_section(request, "/settings")
        status = await _get_status(request.app.state.service, database, settings)
        stored_domains = database.get_setting("college_domains")
        domains = stored_domains if stored_domains is not None else settings.college_domains
        context = _base_context(request, title="Settings", active="settings")
        context.update(
            {
                "status": status,
                "domains": domains,
                "rules": _rules(database),
                "model_name": settings.ollama_model,
                "poll_interval": settings.poll_interval_seconds,
                "settings_editable": hasattr(request.app.state.service, "update_college_domains"),
                "rules_editable": hasattr(request.app.state.service, "delete_rule"),
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="settings.html", context=context)

    @application.get("/learning", response_class=HTMLResponse)
    async def learning_page(request: Request, _: None = Depends(_require_login)) -> Response:
        _remember_dashboard_section(request, "/learning")
        context = _base_context(request, title="Personal learning", active="learning")
        context.update(
            {
                "learning": await _learning_status(request.app.state.service, database),
                "messages": _accuracy_candidates(database),
                "learning_editable": hasattr(request.app.state.service, "update_learning_schedule"),
            }
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="learning.html",
            context=context,
        )

    @application.post("/learning/{message_id}/label")
    async def submit_accuracy_label(
        message_id: str,
        request: Request,
        category: str = Form(...),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        if database.get_message(message_id) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        try:
            selected = Category(category)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Unknown category") from exc
        return await run_operation(
            request,
            "submit_accuracy_label",
            "Accuracy answer saved as encrypted training data; Gmail correction queued.",
            "/learning",
            message_id,
            selected,
        )

    @application.post("/learning/schedule")
    async def update_learning_schedule(
        request: Request,
        enabled: str = Form("false"),
        interval_days: int = Form(...),
        hour_local: int = Form(...),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        if not 1 <= interval_days <= 90 or not 0 <= hour_local <= 23:
            raise HTTPException(status_code=422, detail="Invalid training schedule")
        return await run_operation(
            request,
            "update_learning_schedule",
            "Automatic training schedule updated.",
            "/learning",
            enabled=enabled == "true",
            interval_days=interval_days,
            hour_local=hour_local,
        )

    @application.post("/learning/train")
    async def train_personalized_model(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request,
            "train_personalized_model",
            "Training completed. The candidate is active only if it passed evaluation.",
            "/learning",
            force=True,
        )

    @application.post("/learning/rollback")
    async def rollback_main_model(
        request: Request,
        confirmation: str = Form(...),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        if confirmation != "ROLLBACK":
            raise HTTPException(status_code=422, detail="Type ROLLBACK to confirm")
        return await run_operation(
            request,
            "rollback_main_model",
            "The previous fine-tuned main model is active again.",
            "/learning",
        )

    @application.post("/settings/college-domains")
    async def update_college_domains(
        request: Request,
        domains: str = Form(""),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        normalized = {
            item.strip().lower().lstrip("@")
            for item in domains.replace("\n", ",").split(",")
            if item.strip()
        }
        if any(
            len(item) > 253
            or "." not in item
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for char in item)
            for item in normalized
        ):
            raise HTTPException(status_code=422, detail="Enter valid comma-separated domains")
        return await run_operation(
            request,
            "update_college_domains",
            "College domains updated.",
            "/settings",
            normalized,
        )

    @application.post("/settings/rules/{rule_id}/delete")
    async def delete_rule(
        rule_id: int,
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request, "delete_rule", "Correction rule removed.", "/settings", rule_id
        )

    @application.get("/activity", response_class=HTMLResponse)
    async def activity_page(request: Request, _: None = Depends(_require_login)) -> Response:
        _remember_dashboard_section(request, "/activity")
        context = _base_context(request, title="Activity", active="activity")
        context.update(
            {
                "events": _events(database),
                "batches": database.list_recent_batches(limit=20),
                "now": datetime.now(UTC).isoformat(),
            }
        )
        return TEMPLATES.TemplateResponse(request=request, name="activity.html", context=context)

    @application.post("/jobs/retry")
    async def retry_failed_jobs(
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        return await run_operation(
            request,
            "retry_failed_jobs",
            "Failed work was reset and queued for another safe attempt.",
            "/activity",
        )

    @application.post("/batches/{batch_id}/undo")
    async def undo_batch(
        batch_id: str,
        request: Request,
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        if not database.list_audit_batch(batch_id):
            raise HTTPException(status_code=404, detail="Undo batch not found or expired")
        return await run_operation(
            request,
            "undo_batch",
            "Undo queued. Original Mail-Buddy labels and inbox state will be restored.",
            "/activity",
            batch_id,
        )

    @application.post("/disconnect")
    async def disconnect(
        request: Request,
        confirmation: str = Form(""),
        _: None = Depends(_require_login),
        __: None = Depends(_require_csrf),
    ) -> Response:
        if confirmation != "DISCONNECT":
            _flash(request, "Type DISCONNECT exactly to confirm.", "warning")
            return RedirectResponse("/settings", status_code=303)
        try:
            await _invoke(request.app.state.service, "disconnect")
        except Exception:
            logger.warning("WEB_OPERATION_FAILED operation=disconnect")
            _flash(
                request,
                "Disconnect did not complete. Gmail access and local credentials "
                "were retained so you can retry safely.",
                "danger",
            )
            return RedirectResponse("/settings", status_code=303)
        request.session.clear()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("mail_buddy_session", path="/")
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if str(request.scope.get("path", "/")).startswith("/api/"):
            return JSONResponse({"detail": str(exc.detail)}, status_code=exc.status_code)
        context = _base_context(request, title="Request error", active="")
        context.update(
            {
                "status_code": exc.status_code,
                "message": str(exc.detail),
            }
        )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="error.html",
            context=context,
            status_code=exc.status_code,
        )

    return application


app = create_app()
