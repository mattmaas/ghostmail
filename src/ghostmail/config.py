"""Configuration management for GhostMail."""

import os
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """GhostMail configuration."""

    model_config = SettingsConfigDict(
        env_prefix="GHOSTMAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Project paths
    data_dir: Path = Field(
        default=Path.home() / ".ghostmail" / "data",
        description="Directory for local data storage",
    )

    # Gmail API credentials (set via Google Cloud Console)
    gmail_client_id: str = Field(
        default="",
        description="OAuth2 client ID from Google Cloud Console",
    )
    gmail_client_secret: str = Field(
        default="",
        description="OAuth2 client secret from Google Cloud Console",
    )

    # LLM API Keys
    # DeepSeek API (paid but cheap - https://platform.deepseek.com)
    deepseek_api_key: str = Field(
        default="",
        description="DeepSeek API key for reasoning tasks",
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        description="DeepSeek API base URL",
    )

    # OpenCode Zen - for MiniMax and Kimi free tier
    # Set your OpenCode Zen API key here or via GHOSTMAIL_OPENCODE_API_KEY
    opencode_api_key: str = Field(
        default="",
        description="OpenCode Zen API key for MiniMax/Kimi free tier",
    )
    opencode_base_url: str = Field(
        default="https://api.opencode.cn/v1",
        description="OpenCode Zen API base URL",
    )

    # Personal identity & optional integrations
    self_email: str = Field(
        default="",
        description=(
            "Your own Gmail address. Used as the digest sender/recipient and "
            "excluded from inbox sweeps (self-sent mail). "
            "Set via GHOSTMAIL_SELF_EMAIL."
        ),
    )
    self_aliases: list[str] = Field(
        default=[],
        description=(
            "Additional addresses you send from (also excluded from sweeps). "
            "Set via GHOSTMAIL_SELF_ALIASES as a JSON list, e.g. "
            "GHOSTMAIL_SELF_ALIASES='[\"you@work.com\"]'"
        ),
    )
    jobauto_jobs_path: Optional[Path] = Field(
        default=None,
        description=(
            "Optional path to a JobAuto jobs.json for cross-linking recruiter "
            "mail to your application records. Unset -> cross-link disabled. "
            "Set via GHOSTMAIL_JOBAUTO_JOBS_PATH."
        ),
    )

    # Gmail API settings
    gmail_scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.labels",
        ],
        description="OAuth scopes for Gmail API",
    )

    # Rate limiting
    gmail_quota_units_per_minute: int = Field(
        default=15000,
        description="Gmail API quota units per minute",
    )

    # AI Engine settings
    ai_temperature: float = Field(
        default=0.3,
        description="Temperature for LLM generation",
    )
    ai_max_tokens: int = Field(
        default=2048,
        description="Max tokens for LLM responses",
    )

    # Privacy settings
    sensitive_keywords: list[str] = Field(
        default=[
            "password",
            "ssn",
            "social security",
            "bank",
            "credit card",
            "medical",
            "diagnosis",
            "prescription",
            "therapy",
            "confidential",
            "secret",
            "private key",
            "api key",
            "access token",
        ],
        description="Keywords that trigger local-only processing",
    )

    # Auto-execution thresholds
    auto_label_confidence: float = Field(
        default=0.85,
        description="Minimum confidence to auto-apply labels",
    )
    auto_archive_confidence: float = Field(
        default=0.90,
        description="Minimum confidence to auto-archive",
    )

    @property
    def data_dir_expanded(self) -> Path:
        """Ensure data directory exists and return path."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir

    @property
    def credentials_path(self) -> Path:
        """Path to OAuth credentials file."""
        return self.data_dir_expanded / "credentials.json"

    @property
    def database_path(self) -> Path:
        """Path to SQLite database."""
        return self.data_dir_expanded / "ghostmail.db"

    def self_addresses(self) -> list[str]:
        """All addresses treated as self-sent (primary + aliases), lowercased."""
        return [a.lower() for a in [self.self_email, *self.self_aliases] if a]


# Global settings instance
settings = Settings()


def get_settings() -> Settings:
    """Get settings instance, ensuring data dir exists."""
    settings.data_dir_expanded  # Ensure directory exists
    return settings
