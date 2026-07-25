"""The one and only outbound path: rate limit → ledger → bridge.

Handlers call ``ctx.send``; ``ctx.send`` calls a :class:`~whatsapp_bot.processing.base.Sender`.
In production that sender is :class:`LiveSender`, so every message — whichever
handler produced it — passes the same quota and lands in the same table.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import structlog

from whatsapp_bot import db as db_module
from whatsapp_bot.bridge_client import BridgeClient, BridgeError
from whatsapp_bot.models import Outbound
from whatsapp_bot.ratelimit import RateLimiter


def new_outbound_id() -> str:
    return f"out-{uuid4().hex}"


class LiveSender:
    def __init__(
        self,
        conn: sqlite3.Connection,
        limiter: RateLimiter,
        bridge: BridgeClient,
        logger: structlog.stdlib.BoundLogger,
    ) -> None:
        self._conn = conn
        self._limiter = limiter
        self._bridge = bridge
        self._log = logger

    def __call__(self, out: Outbound, *, reply_to: str | None = None) -> str:
        outbound_id = new_outbound_id()

        verdict = self._limiter.check_and_consume()
        if not verdict.allowed:
            # Dropped on purpose, without raising: retrying a send that the
            # quota refused would only burn the remaining budget faster. The row
            # stays in the ledger so nothing disappears silently.
            db_module.record_outbound(
                self._conn,
                outbound_id,
                out,
                status="rate_limited",
                reply_to=reply_to,
                error=verdict.reason,
            )
            self._log.warning(
                "send_rate_limited",
                outbound_id=outbound_id,
                jid=out.jid,
                scope=verdict.scope,
                minute_count=verdict.minute_count,
                day_count=verdict.day_count,
            )
            return outbound_id

        db_module.record_outbound(self._conn, outbound_id, out, status="queued", reply_to=reply_to)

        try:
            self._bridge.send(outbound_id, out)
        except BridgeError as exc:
            db_module.record_outbound(
                self._conn,
                outbound_id,
                out,
                status="failed",
                reply_to=reply_to,
                error=str(exc)[:2000],
            )
            self._log.error("send_failed", outbound_id=outbound_id, jid=out.jid, error=str(exc))
            # Propagates so RQ retries the job: a message that did not leave is
            # worse than a rare duplicate when the bridge comes back.
            raise

        self._log.info(
            "send_queued",
            outbound_id=outbound_id,
            jid=out.jid,
            handler=out.handler,
            day_count=verdict.day_count,
        )
        return outbound_id


class RecordingSender:
    """In-memory sender used by tests and by ``--dry-run`` tooling."""

    def __init__(self) -> None:
        self.messages: list[Outbound] = []

    def __call__(self, out: Outbound, *, reply_to: str | None = None) -> str:
        self.messages.append(out)
        return new_outbound_id()
