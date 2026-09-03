from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mail_buddy.contracts import MODEL_NAME


def _read_secret(value: str | None, file_path: Path | None) -> str | None:
    if value:
        return value.strip()
    if file_path and file_path.exists():
        return file_path.read_text(encoding="utf-8").strip()
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAIL_BUDDY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Mail-Buddy"
    environment: str = "production"
    data_dir: Path = Path("/data")
    backup_dir: Path = Path("/backups")
    database_filename: str = "mail_buddy.sqlite3"
    google_client_secret_path: Path = Path("/run/secrets/google_client_secret")

    encryption_key: str | None = None
    encryption_key_file: Path | None = Path("/run/secrets/encryption_key")
    session_secret: str | None = None
    session_secret_file: Path | None = Path("/run/secrets/session_secret")
    password_hash: str | None = None
    password_hash_file: Path | None = Path("/run/secrets/password_hash")

    ollama_url: str = "http://ollama:11434"
    ollama_primary_url: str | None = None
    ollama_model: str = MODEL_NAME
    ollama_timeout_seconds: float = 120.0
    ollama_connect_timeout_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    poll_interval_seconds: int = Field(default=120, ge=30, le=3600)
    worker_idle_seconds: float = Field(default=1.0, ge=0.1, le=30)
    backfill_page_size: int = Field(default=500, ge=10, le=500)
    sample_size: int = Field(default=25, ge=1, le=100)
    max_model_input_chars: int = Field(default=8_000, ge=1_000, le=30_000)
    personalization_enabled: bool = True
    training_interval_days: int = Field(default=7, ge=1, le=90)
    training_hour_local: int = Field(default=2, ge=0, le=23)
    training_min_examples: int = Field(default=10, ge=5, le=10_000)
    training_min_evaluated: int = Field(default=5, ge=1, le=10_000)
    training_min_accuracy: float = Field(default=0.65, ge=0.0, le=1.0)
    personalization_min_confidence: float = Field(default=0.70, ge=0.5, le=1.0)
    personalization_min_margin: float = Field(default=0.15, ge=0.0, le=1.0)

    secure_cookies: bool = True
    session_max_age_seconds: int = Field(default=43_200, ge=300, le=604_800)
    login_attempts: int = Field(default=5, ge=1, le=20)
    login_window_seconds: int = Field(default=300, ge=30, le=3600)

    college_domains: str = ""
    demo_mode: bool = False
    disable_worker: bool = False
    log_level: str = "INFO"

    @field_validator("college_domains")
    @classmethod
    def normalize_domains(cls, value: str) -> str:
        return ",".join(
            sorted({item.strip().lower().lstrip("@") for item in value.split(",") if item.strip()})
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_filename

    @property
    def resolved_encryption_key(self) -> str | None:
        return _read_secret(self.encryption_key, self.encryption_key_file)

    @property
    def resolved_session_secret(self) -> str | None:
        return _read_secret(self.session_secret, self.session_secret_file)

    @property
    def resolved_password_hash(self) -> str | None:
        return _read_secret(self.password_hash, self.password_hash_file)

    @property
    def college_domain_set(self) -> set[str]:
        return {item for item in self.college_domains.split(",") if item}

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def validate_runtime_secrets(self) -> None:
        missing: list[str] = []
        if not self.resolved_encryption_key:
            missing.append("encryption_key")
        if not self.resolved_session_secret:
            missing.append("session_secret")
        if not self.resolved_password_hash:
            missing.append("password_hash")
        if missing and not self.demo_mode:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required Mail-Buddy secrets: {joined}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
