"""Shared fixtures: a throwaway SQLite file, a fake Redis, a fake bridge.

No test touches the network, WhatsApp or a real Redis.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import fakeredis
import pytest
from factories import FakeSender

from whatsapp_bot import db as db_module
from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.logging import get_logger
from whatsapp_bot.processing.base import Context
from whatsapp_bot.processing.registry import reset_registry
from whatsapp_bot.ratelimit import RateLimiter

TEST_TOKEN = "test-token-0123456789"

# Cleared before each test so a developer's exported shell variables cannot
# change what the handlers do.
_OVERRIDABLE_ENV = (
    "BOT_TOKEN",
    "PING_ENABLED",
    "RELAY_KEYWORDS",
    "RELAY_TARGET_JIDS",
    "BROADCAST_ENABLED",
    "BROADCAST_COMMAND",
    "BROADCAST_ADMIN_JIDS",
    "BROADCAST_MAX_RECIPIENTS",
    "BROADCAST_ALIASES",
    "GROUP_BROADCAST_ENABLED",
    "GROUP_BROADCAST_COMMAND",
    "GROUP_BROADCAST_ADMIN_JIDS",
    "GROUP_BROADCAST_MAX_RECIPIENTS",
    "GROUP_DIRECTORY_TTL_SECONDS",
    "PHONE_COUNTRY_CODE",
    "RATE_LIMIT_PER_DAY",
    "RATE_LIMIT_PER_MINUTE",
    "WORKER_JOB_TIMEOUT",
    "WORKER_MAX_RETRIES",
    "WORKER_RETRY_INTERVALS",
    "DATABASE_PATH",
    "REDIS_URL",
    "BRIDGE_URL",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _OVERRIDABLE_ENV:
        monkeypatch.delenv(name, raising=False)
    # Handlers read the settings when the registry instantiates them, so the
    # one mandatory setting has to be present — as it always is in production.
    monkeypatch.setenv("BOT_TOKEN", TEST_TOKEN)
    reset_registry()
    get_settings.cache_clear()
    yield
    reset_registry()
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings backed by a temporary database, ignoring any local .env."""
    return Settings(
        bot_token=TEST_TOKEN,
        database_path=str(tmp_path / "bot.db"),
        redis_url="redis://localhost:6379/15",
        _env_file=None,
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    with db_module.session(settings.database_path) as connection:
        yield connection


@pytest.fixture
def redis_conn() -> fakeredis.FakeRedis:
    return fakeredis.FakeRedis()


@pytest.fixture
def limiter(redis_conn: fakeredis.FakeRedis, settings: Settings) -> RateLimiter:
    return RateLimiter(
        redis_conn,
        per_minute=settings.rate_limit_per_minute,
        per_day=settings.rate_limit_per_day,
    )


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def context(conn: sqlite3.Connection, settings: Settings, sender: FakeSender) -> Context:
    return Context(
        db=conn,
        config=settings,
        logger=get_logger("test"),
        sender=sender,
        inbound_id="msg-1",
    )
