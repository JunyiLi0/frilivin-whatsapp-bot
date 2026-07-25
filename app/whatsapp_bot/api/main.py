"""Ingest API.

``POST /webhook`` does three cheap things — validate, deduplicate, enqueue — and
answers. Nothing is processed synchronously: the bridge must never wait on
business logic, and WhatsApp must never wait on the bridge.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from rq import Retry

from whatsapp_bot import __version__
from whatsapp_bot import db as db_module
from whatsapp_bot.bridge_client import BridgeError, get_bridge_client
from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.logging import configure_logging, get_logger
from whatsapp_bot.models import InboundMessage, SendStatusUpdate, WebhookAck
from whatsapp_bot.queues import get_queue, get_redis
from whatsapp_bot.worker.tasks import process_message

log = get_logger("whatsapp_bot.api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    settings = get_settings()
    configure_logging("api", settings.log_level)
    with db_module.session(settings.database_path) as conn:
        db_module.init_db(conn)
    log.info("api_started", version=__version__, database=settings.database_path)
    yield


app = FastAPI(
    title="frilivin-whatsapp-bot",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# --- dependencies ------------------------------------------------------------


def settings_dep() -> Settings:
    return get_settings()


def db_dep(settings: Annotated[Settings, Depends(settings_dep)]) -> Iterator[sqlite3.Connection]:
    conn = db_module.connect(settings.database_path)
    try:
        db_module.init_db(conn)
        yield conn
    finally:
        conn.close()


def require_token(
    settings: Annotated[Settings, Depends(settings_dep)],
    x_bot_token: Annotated[str | None, Header()] = None,
) -> None:
    """Shared-secret check. ``compare_digest`` keeps it constant-time."""
    if not x_bot_token or not secrets.compare_digest(x_bot_token, settings.bot_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


Auth = Annotated[None, Depends(require_token)]
Db = Annotated[sqlite3.Connection, Depends(db_dep)]
Config = Annotated[Settings, Depends(settings_dep)]


# --- endpoints ---------------------------------------------------------------


@app.get("/health")
def health(settings: Config, conn: Db) -> dict[str, Any]:
    """Liveness for Docker plus a quick look at both dependencies."""
    checks: dict[str, str] = {}

    try:
        conn.execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except sqlite3.Error as exc:
        checks["database"] = f"error: {exc}"

    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # health must report, never raise
        checks["redis"] = f"error: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "service": settings.service_name,
        "checks": checks,
    }


@app.post("/webhook", response_model=WebhookAck)
def webhook(msg: InboundMessage, settings: Config, conn: Db, _auth: Auth) -> WebhookAck:
    """Accept one inbound message from the bridge."""
    is_new = db_module.record_inbound(conn, msg)
    if not is_new:
        # Baileys redelivers on reconnect; the primary key is the dedup point.
        log.info("message_duplicate", message_id=msg.id)
        return WebhookAck(status="duplicate", id=msg.id)

    job = get_queue(settings=settings).enqueue(
        process_message,
        msg.model_dump(by_alias=False),
        job_timeout=settings.worker_job_timeout,
        retry=Retry(max=settings.worker_max_retries, interval=settings.retry_intervals),
        failure_ttl=86400,
        result_ttl=3600,
    )
    db_module.mark_status(conn, msg.id, "queued")

    log.info(
        "message_queued",
        message_id=msg.id,
        job_id=job.id,
        chat_jid=msg.chat_jid,
        is_group=msg.is_group,
        type=msg.type,
    )
    return WebhookAck(status="queued", id=msg.id, job_id=job.id)


@app.post("/internal/send-status", status_code=status.HTTP_204_NO_CONTENT)
def send_status(update: SendStatusUpdate, conn: Db, _auth: Auth) -> Response:
    """Callback from the bridge once an outbound message is settled."""
    db_module.mark_status(conn, update.id, update.status, error=update.error)
    log.info(
        "send_settled",
        outbound_id=update.id,
        result=update.status,
        wa_message_id=update.wa_message_id,
        error=update.error,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/groups")
def groups(_auth: Auth) -> dict[str, Any]:
    """Proxy the bridge's group listing — this is how you find target JIDs."""
    try:
        found = get_bridge_client().groups()
    except BridgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"count": len(found), "groups": found}
