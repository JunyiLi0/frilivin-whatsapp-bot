"""LiveSender: the single choke point every outbound message goes through."""

from __future__ import annotations

import sqlite3

import fakeredis
import pytest

from whatsapp_bot import db as db_module
from whatsapp_bot.bridge_client import BridgeError
from whatsapp_bot.logging import get_logger
from whatsapp_bot.models import Outbound
from whatsapp_bot.ratelimit import RateLimiter
from whatsapp_bot.sending import LiveSender


class FakeBridge:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, Outbound]] = []
        self.fail_with = fail_with

    def send(self, outbound_id: str, out: Outbound) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((outbound_id, out))
        return outbound_id


def build_sender(
    conn: sqlite3.Connection,
    redis_conn: fakeredis.FakeRedis,
    bridge: FakeBridge,
    *,
    per_minute: int = 30,
    per_day: int = 500,
) -> LiveSender:
    limiter = RateLimiter(redis_conn, per_minute=per_minute, per_day=per_day)
    return LiveSender(conn, limiter, bridge, get_logger("test"))  # type: ignore[arg-type]


MESSAGE = Outbound(jid="120363000000000000@g.us", text="coucou", handler="Test")


def test_a_sent_message_reaches_the_bridge_and_the_ledger(
    conn: sqlite3.Connection, redis_conn: fakeredis.FakeRedis
) -> None:
    bridge = FakeBridge()
    outbound_id = build_sender(conn, redis_conn, bridge)(MESSAGE, reply_to="m1")

    assert len(bridge.calls) == 1
    row = db_module.get_message(conn, outbound_id)
    assert row is not None
    assert row["status"] == "queued"  # the bridge confirms later, via the callback
    assert row["reply_to"] == "m1"
    assert row["text"] == "coucou"


def test_ids_are_unique(conn: sqlite3.Connection, redis_conn: fakeredis.FakeRedis) -> None:
    send = build_sender(conn, redis_conn, FakeBridge())
    assert send(MESSAGE) != send(MESSAGE)


def test_a_throttled_message_is_recorded_but_not_sent(
    conn: sqlite3.Connection, redis_conn: fakeredis.FakeRedis
) -> None:
    bridge = FakeBridge()
    send = build_sender(conn, redis_conn, bridge, per_minute=1)

    send(MESSAGE)
    throttled_id = send(MESSAGE)

    assert len(bridge.calls) == 1
    row = db_module.get_message(conn, throttled_id)
    assert row is not None
    assert row["status"] == "rate_limited"
    assert "minute" in row["error"]


def test_throttling_does_not_raise(
    conn: sqlite3.Connection, redis_conn: fakeredis.FakeRedis
) -> None:
    """Retrying a send the quota refused would only burn the budget faster."""
    send = build_sender(conn, redis_conn, FakeBridge(), per_day=0)
    send(MESSAGE)  # must not raise


def test_a_bridge_failure_is_recorded_and_propagates(
    conn: sqlite3.Connection, redis_conn: fakeredis.FakeRedis
) -> None:
    bridge = FakeBridge(fail_with=BridgeError("bridge unreachable"))
    send = build_sender(conn, redis_conn, bridge)

    with pytest.raises(BridgeError):
        send(MESSAGE)

    rows = db_module.recent_messages(conn, limit=1)
    assert rows[0]["status"] == "failed"
    assert "unreachable" in rows[0]["error"]
