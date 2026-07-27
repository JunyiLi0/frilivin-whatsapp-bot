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
    # Documents are processed in the same queue but need a far larger budget:
    # parsing a spreadsheet and the Sage reference exports cannot fit in 2 s.
    worker_document_job_timeout: int = 60
    # A command that has to list the WhatsApp groups first cannot fit in 2 s
    # either: the answer comes from WhatsApp, not from local state.
    worker_directory_job_timeout: int = 30
    worker_max_retries: int = 3
    worker_retry_intervals: str = "2,8,32"

    # --- media ---
    media_outbox_dir: str = "/media/out"
    media_retention_days: int = 14

    # --- rate limiting ---
    rate_limit_per_day: int = 500
    rate_limit_per_minute: int = 30

    # --- example handlers ---
    ping_enabled: bool = True
    relay_keywords: str = ""
    relay_target_jids: str = ""

    # --- sage import handler ---
    sage_enabled: bool = True
    sage_clients_path: str = "/data/sage/clients.txt"
    sage_articles_path: str = "/data/sage/articles.txt"
    # Empty means anyone may submit a spreadsheet; a list restricts it.
    sage_admin_jids: str = ""

    # --- broadcast handler ---
    broadcast_enabled: bool = True
    broadcast_command: str = "!envoi"
    # Empty means nobody may broadcast, which disables the handler entirely.
    broadcast_admin_jids: str = ""
    broadcast_max_recipients: int = 25
    # "nord=120363000000000000@g.us,sud=33612345678"
    broadcast_aliases: str = ""

    # --- group broadcast handler ---
    group_broadcast_enabled: bool = True
    group_broadcast_command: str = "!envoigroupe"
    # Empty falls back to BROADCAST_ADMIN_JIDS; empty on both sides disables
    # the handler entirely.
    group_broadcast_admin_jids: str = ""
    group_broadcast_max_recipients: int = 25
    # How long a group list is reused before being fetched from WhatsApp again.
    group_directory_ttl_seconds: float = 300.0
    # Country code assumed when a member number is typed in national form
    # (07 66 66 06 73). Empty disables that reading.
    phone_country_code: str = "33"

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
    def sage_admin_list(self) -> list[str]:
        return _split_csv(self.sage_admin_jids)

    @property
    def broadcast_admin_list(self) -> list[str]:
        """Raw allowlist entries — the handler canonicalises them."""
        return _split_csv(self.broadcast_admin_jids)

    @property
    def group_broadcast_admin_list(self) -> list[str]:
        """Who may write to a group — the broadcast allowlist unless overridden.

        One allowlist is the common case: the people who may broadcast are the
        people who may write to a group. A dedicated list is there for when
        they are not.
        """
        return _split_csv(self.group_broadcast_admin_jids) or self.broadcast_admin_list

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
