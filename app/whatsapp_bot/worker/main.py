"""Worker entry point: ``python -m whatsapp_bot.worker.main``.

Retries use a scheduled backoff, so the worker must run its built-in scheduler
(``with_scheduler=True``) — without it a job whose retry has an interval would
be scheduled and never picked up again.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from redis import Redis
from rq import Worker
from rq.job import Job

from whatsapp_bot import db as db_module
from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.logging import configure_logging, get_logger
from whatsapp_bot.processing.registry import get_handlers
from whatsapp_bot.queues import get_dead_letter_queue, get_queue, make_redis
from whatsapp_bot.worker.tasks import process_message

ExceptionHandler = Callable[..., bool]


def make_dead_letter_handler(settings: Settings, connection: Redis) -> ExceptionHandler:
    """Park jobs that exhausted every retry instead of losing them.

    RQ calls exception handlers on *each* failure, including the ones it is
    about to retry, hence the ``should_retry`` guard: at this point the counter
    has not been decremented yet.
    """
    log = get_logger("whatsapp_bot.worker.dlq")

    def dead_letter_handler(job: Job, *exc_info: Any) -> bool:
        error = repr(exc_info[1]) if len(exc_info) > 1 else "unknown error"

        if job.should_retry:
            log.warning(
                "job_retry_scheduled",
                job_id=job.id,
                retries_left=job.retries_left,
                error=error,
            )
            return True

        payload: dict[str, Any] = job.args[0] if job.args else {}
        message_id = payload.get("id")

        try:
            get_dead_letter_queue(connection, settings).enqueue(
                process_message,
                payload,
                job_id=f"dead-{job.id}",
                job_timeout=settings.worker_job_timeout,
                meta={"original_job_id": job.id, "error": error},
            )
        except Exception:
            log.exception("dead_letter_enqueue_failed", job_id=job.id)

        if message_id:
            try:
                with db_module.session(settings.database_path) as conn:
                    db_module.mark_status(conn, str(message_id), "dead", error=error[:2000])
            except Exception:
                log.exception("dead_letter_mark_failed", message_id=message_id)

        log.error("job_dead_lettered", job_id=job.id, message_id=message_id, error=error)
        return True

    return dead_letter_handler


def main() -> int:
    settings = get_settings()
    configure_logging("worker", settings.log_level)
    log = get_logger("whatsapp_bot.worker")

    connection = make_redis(settings.redis_url)

    # Load the chain now rather than on the first message: a syntax error in a
    # handler should surface at boot, not at 3 a.m. on the first alert.
    handlers = get_handlers()
    if not handlers:
        log.warning("no_handlers_registered")

    queue = get_queue(connection, settings)
    worker = Worker(
        [queue],
        connection=connection,
        exception_handlers=[make_dead_letter_handler(settings, connection)],
    )

    log.info(
        "worker_starting",
        queue=settings.queue_name,
        dlq=settings.dlq_name,
        job_timeout=settings.worker_job_timeout,
        max_retries=settings.worker_max_retries,
        retry_intervals=settings.retry_intervals,
        handlers=[f"{handler.name}:{handler.priority}" for handler in handlers],
    )

    worker.work(with_scheduler=True, logging_level=settings.log_level.upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())
