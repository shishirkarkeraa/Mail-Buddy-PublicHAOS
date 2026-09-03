from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mail_buddy.config import Settings
from mail_buddy.contracts import CATEGORY_LABELS, NEEDS_REVIEW_LABEL
from mail_buddy.db import Database
from mail_buddy.gmail import GmailClient
from mail_buddy.security import SecretBox

try:  # Permit importing the rest of the app before runtime dependencies install.
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:  # pragma: no cover - incomplete installation only.
    GoogleAuthRequest = None  # type: ignore[assignment,misc]
    Credentials = None  # type: ignore[assignment,misc]
    InstalledAppFlow = None  # type: ignore[assignment,misc]


GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
OAUTH_SCOPES = (GMAIL_MODIFY_SCOPE,)
TOKEN_SETTING_KEY = "oauth_token"  # noqa: S105 - database key, not a credential.


class OAuthError(RuntimeError):
    pass


class OAuthConfigurationError(OAuthError):
    pass


class OAuthTokenError(OAuthError):
    pass


class OAuthManager:
    """Owns the installed-app OAuth flow and encrypted credential persistence."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        secret_box: SecretBox | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        encryption_key = settings.resolved_encryption_key
        if secret_box is None and not encryption_key:
            raise OAuthConfigurationError(
                "MAIL_BUDDY_ENCRYPTION_KEY or its secret file is required"
            )
        self.secret_box = secret_box or SecretBox(str(encryption_key))

    def _require_dependencies(self) -> None:
        if Credentials is None or InstalledAppFlow is None or GoogleAuthRequest is None:
            raise OAuthConfigurationError("Google OAuth dependencies are not installed")

    def save_credentials(self, credentials: Any) -> None:
        try:
            serialized = credentials.to_json()
            json.loads(serialized)
        except (AttributeError, TypeError, json.JSONDecodeError) as error:
            raise OAuthTokenError("OAuth credentials could not be serialized") from error
        self.database.set_setting(
            TOKEN_SETTING_KEY,
            self.secret_box.encrypt(serialized),
        )

    def load_credentials(self, *, refresh: bool = True) -> Any | None:
        encrypted = self.database.get_setting(TOKEN_SETTING_KEY)
        if not encrypted:
            return None
        self._require_dependencies()
        try:
            serialized = self.secret_box.decrypt(encrypted)
            info = json.loads(serialized)
            credentials = Credentials.from_authorized_user_info(
                info,
                scopes=list(OAUTH_SCOPES),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise OAuthTokenError("Stored OAuth credentials are invalid") from error

        if refresh and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(GoogleAuthRequest())
            except Exception as error:
                raise OAuthTokenError("OAuth credentials could not be refreshed") from error
            self.save_credentials(credentials)
        return credentials

    def authorize(
        self,
        *,
        host: str = "localhost",
        bind_addr: str = "0.0.0.0",  # noqa: S104 - required for an SSH tunnel.
        port: int = 8765,
        open_browser: bool = False,
    ) -> dict[str, Any]:
        """Run an offline installed-app flow suitable for an SSH tunnel.

        ``host`` is embedded in the redirect URI while ``bind_addr`` controls
        the listening interface. A typical remote invocation tunnels local
        port 8765 to the Pi and opens the printed authorization URL locally.
        """

        self._require_dependencies()
        client_secret = Path(self.settings.google_client_secret_path)
        if not client_secret.is_file():
            raise OAuthConfigurationError(
                f"Google OAuth client secret not found at {client_secret}"
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret),
            scopes=list(OAUTH_SCOPES),
        )
        try:
            credentials = flow.run_local_server(
                host=host,
                bind_addr=bind_addr,
                port=port,
                open_browser=open_browser,
                authorization_prompt_message=(
                    "Open this URL in a browser through the SSH tunnel:\n{url}"
                ),
                success_message=("Mail-Buddy authorization succeeded. You may close this tab."),
                access_type="offline",
                prompt="consent",
                include_granted_scopes="true",
            )
        except Exception as error:
            raise OAuthError("Google authorization did not complete") from error

        client = GmailClient(credentials)
        profile = client.get_profile()
        email = str(profile.get("emailAddress", ""))
        history_id = str(profile.get("historyId", ""))
        if not email or not history_id:
            raise OAuthTokenError("Gmail profile omitted account identity")

        labels = client.ensure_labels()
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
        self.save_credentials(credentials)
        self.database.connect_account(email, history_id)
        return {
            "email": email,
            "history_id": history_id,
            "labels": labels,
        }

    def gmail_client(self, *, refresh: bool = True) -> GmailClient | None:
        credentials = self.load_credentials(refresh=refresh)
        return GmailClient(credentials) if credentials is not None else None

    def delete_credentials(self) -> None:
        self.database.delete_setting(TOKEN_SETTING_KEY)
