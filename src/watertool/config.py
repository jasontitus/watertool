"""Environment-driven configuration.

Secrets live in the process environment / .env, never in code. The Rachio token
and basic-auth password are SecretStr so they don't leak into logs or reprs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        populate_by_name=True,
    )

    # --- Rachio ---
    # Rachio's app calls it "API Key"; accept either env name.
    rachio_api_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("RACHIO_API_TOKEN", "RACHIO_API_KEY"),
    )
    rachio_api_base: str = "https://api.rach.io/1"
    rachio_cloud_rest_base: str = "https://cloud-rest.rach.io"

    # --- Storage ---
    database_path: Path = Path("data/watertool.db")

    # --- Webhook ingestion ---
    public_base_url: str = ""
    webhook_path: str = "/webhooks/rachio"
    webhook_verify: str = "hmac"  # hmac | basic | none
    webhook_basic_user: str = "rachio"
    webhook_basic_pass: SecretStr = SecretStr("")

    # --- Jobs ---
    backfill_days: int = 365
    reconcile_overlap_days: int = 2
    event_window_days: int = 28  # chunk size for Rachio /event queries
    api_min_interval_seconds: float = 0.5  # polite spacing between API calls

    @property
    def webhook_url(self) -> str:
        base = self.public_base_url.rstrip("/")
        return f"{base}{self.webhook_path}" if base else ""

    @property
    def token(self) -> str:
        return self.rachio_api_token.get_secret_value()


def load_settings() -> Settings:
    return Settings()
