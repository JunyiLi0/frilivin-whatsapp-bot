"""Application settings, read once from the environment.

Comma-separated lists are declared as plain strings and exposed through parsed
properties: pydantic-settings would otherwise try to JSON-decode ``list[str]``
fields, which makes ``.env`` files awkward to write by hand.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- identity & logging ---
    service_name: str = "app"
    log_level: str = "info"

    # --- internal auth ---
    bot_token: str = Field(min_length=8)

    # --- peers ---
    bridge_url: str = "http://bridge:3000"
    bridge_timeout_seconds: float = 5.0

    # --- storage ---
    database_path: str = "/data/bot.db"
    redis_url: str = "redis://redis:6379/0"

    # --- queues ---
    queue_name: str = "inbound"
    dlq_name: str = "dead"

    # --- worker ---
    worker_job_timeout: int = 2
    worker_max_retries: int = 3
    worker_retry_intervals: str = "2,8,32"

    # --- rate limiting ---
    rate_limit_per_day: int = 500
    rate_limit_per_minute: int = 30

    # --- example handlers ---
    ping_enabled: bool = True
    relay_keywords: str = ""
    relay_target_jids: str = ""

    # --- broadcast handler ---
    broadcast_enabled: bool = True
    broadcast_command: str = "!envoi"
    # Empty means nobody may broadcast, which disables the handler entirely.
    broadcast_admin_jids: str = ""
    broadcast_max_recipients: int = 25
    # "nord=120363000000000000@g.us,sud=33612345678"
    broadcast_aliases: str = ""

    @property
    def retry_intervals(self) -> list[int]:
        """Backoff, in seconds, between worker retries."""
        values = [int(item) for item in _split_csv(self.worker_retry_intervals)]
        return values or [2, 8, 32]

    @property
    def relay_keyword_list(self) -> list[str]:
        return [keyword.lower() for keyword in _split_csv(self.relay_keywords)]

    @property
    def relay_target_list(self) -> list[str]:
        return _split_csv(self.relay_target_jids)

    @property
    def broadcast_admin_list(self) -> list[str]:
        """Raw allowlist entries — the handler canonicalises them."""
        return _split_csv(self.broadcast_admin_jids)

    @property
    def broadcast_alias_map(self) -> dict[str, str]:
        """``name=target`` pairs, keyed by lowercase name."""
        mapping: dict[str, str] = {}
        for item in _split_csv(self.broadcast_aliases):
            name, separator, target = item.partition("=")
            if separator and name.strip() and target.strip():
                mapping[name.strip().lower()] = target.strip()
        return mapping


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Tests reset it with ``get_settings.cache_clear()``.
    """
    return Settings()  # type: ignore[call-arg]  # values come from the environment
