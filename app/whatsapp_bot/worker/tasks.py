"""The job executed for every inbound message, and the pipeline it runs.

``run_pipeline`` is deliberately free of I/O setup: it takes handlers and a
context and does nothing else, which is what makes the whole chain testable with
a handful of fake messages.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from whatsapp_bot import db as db_module
from whatsapp_bot.bridge_client import BridgeClient
from whatsapp_bot.config import get_settings
from whatsapp_bot.logging import get_logger
from whatsapp_bot.models import InboundMessage
from whatsapp_bot.processing.base import Context, Handler
from whatsapp_bot.processing.registry import get_handlers
from whatsapp_bot.queues import make_redis
from whatsapp_bot.ratelimit import RateLimiter
from whatsapp_bot.sending import LiveSender

MAX_ERROR_LENGTH = 2000


class HandlerFailure(RuntimeError):
    """Raised when a handler blows up, so the log names the culprit."""

    def __init__(self, handler: str, original: BaseException) -> None:
        super().__init__(f"{handler}: {original!r}")
        self.handler = handler
        self.original = original


@dataclass
class PipelineResult:
    executed: list[str]
    stopped_by: str | None
    sent: int


def run_pipeline(msg: InboundMessage, ctx: Context, handlers: Sequence[Handler]) -> PipelineResult:
    """Run every matching handler in order, stopping on ``stop_propagation``."""
    root_logger = ctx.logger
    executed: list[str] = []
    stopped_by: str | None = None

    for handler in handlers:
        ctx.logger = root_logger.bind(handler=handler.name)
        try:
            if not handler.match(msg):
                continue
            outbounds = handler.run(msg, ctx) or []
        except Exception as exc:
            raise HandlerFailure(handler.name, exc) from exc

        for out in outbounds:
            # Stamp the author so the ledger says which behaviour spoke.
            ctx.send(out if out.handler else out.model_copy(update={"handler": handler.name}))

        executed.append(handler.name)
        if handler.stop_propagation:
            stopped_by = handler.name
            break

    ctx.logger = root_logger
    return PipelineResult(executed=executed, stopped_by=stopped_by, sent=ctx.sent_count)


def process_message(payload: dict[str, Any]) -> dict[str, Any]:
    """RQ entry point. Enqueued by ``POST /webhook``, one call per message.

    Redis and HTTP clients are built per job on purpose: RQ forks a work horse
    for each job, and a connection inherited across ``fork()`` would be shared
    by two processes at once.
    """
    settings = get_settings()
    msg = InboundMessage.model_validate(payload)
    log = get_logger(
        "whatsapp_bot.pipeline",
        message_id=msg.id,
        chat_jid=msg.chat_jid,
        is_group=msg.is_group,
    )
    started = time.perf_counter()

    redis_conn = make_redis(settings.redis_url)
    bridge = BridgeClient(
        settings.bridge_url, settings.bot_token, timeout=settings.bridge_timeout_seconds
    )

    try:
        with db_module.session(settings.database_path) as conn:
            db_module.mark_status(conn, msg.id, "processing", bump_attempts=True)

            limiter = RateLimiter(
                redis_conn,
                per_minute=settings.rate_limit_per_minute,
                per_day=settings.rate_limit_per_day,
            )
            ctx = Context(
                db=conn,
                config=settings,
                logger=log,
                sender=LiveSender(conn, limiter, bridge, log),
                inbound_id=msg.id,
            )

            try:
                result = run_pipeline(msg, ctx, get_handlers())
            except HandlerFailure as exc:
                db_module.mark_status(
                    conn,
                    msg.id,
                    "failed",
                    error=str(exc)[:MAX_ERROR_LENGTH],
                    handler=exc.handler,
                )
                log.error("pipeline_handler_failed", handler=exc.handler, error=str(exc.original))
                raise
            except Exception as exc:
                db_module.mark_status(conn, msg.id, "failed", error=repr(exc)[:MAX_ERROR_LENGTH])
                log.exception("pipeline_failed")
                raise

            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            db_module.mark_status(
                conn, msg.id, "processed", handler=",".join(result.executed) or None
            )
            log.info(
                "message_processed",
                handlers=result.executed,
                stopped_by=result.stopped_by,
                sent=result.sent,
                duration_ms=duration_ms,
            )
            return {
                "id": msg.id,
                "handlers": result.executed,
                "stopped_by": result.stopped_by,
                "sent": result.sent,
                "duration_ms": duration_ms,
            }
    finally:
        bridge.close()
        redis_conn.close()
