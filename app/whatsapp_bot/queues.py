"""Redis connection and RQ queues.

``decode_responses`` must stay ``False``: RQ stores pickled payloads and breaks
if the client decodes them as text.
"""

from __future__ import annotations

from functools import lru_cache

from redis import Redis
from rq import Queue

from whatsapp_bot.config import Settings, get_settings


def make_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=False, socket_timeout=5)


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    return make_redis(get_settings().redis_url)


def get_queue(connection: Redis | None = None, settings: Settings | None = None) -> Queue:
    """The main inbound queue consumed by the worker."""
    settings = settings or get_settings()
    return Queue(
        settings.queue_name,
        connection=connection or get_redis(),
        default_timeout=settings.worker_job_timeout,
    )


def get_dead_letter_queue(
    connection: Redis | None = None, settings: Settings | None = None
) -> Queue:
    """Where jobs land once every retry has been exhausted.

    Jobs are parked here rather than dropped so a bad deploy can be replayed
    with ``make dlq-requeue`` once the handler is fixed.
    """
    settings = settings or get_settings()
    return Queue(settings.dlq_name, connection=connection or get_redis())
