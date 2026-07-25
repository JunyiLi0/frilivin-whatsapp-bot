"""Inspect and replay the dead-letter queue — ``make dlq`` / ``make dlq-requeue``.

Jobs land there once every retry failed. Fix the handler, redeploy, then replay:
nothing is lost in the meantime.
"""

from __future__ import annotations

import argparse
import sys

from rq import Retry

from whatsapp_bot.config import get_settings
from whatsapp_bot.queues import get_dead_letter_queue, get_queue, make_redis
from whatsapp_bot.worker.tasks import process_message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dead-letter queue management")
    parser.add_argument("action", choices=["list", "requeue", "purge"])
    args = parser.parse_args(argv)

    settings = get_settings()
    connection = make_redis(settings.redis_url)
    dlq = get_dead_letter_queue(connection, settings)

    if args.action == "list":
        jobs = dlq.get_jobs()
        if not jobs:
            print("(dead-letter queue vide)")
            return 0
        print(f"{len(jobs)} job(s) en dead-letter :")
        for job in jobs:
            payload = job.args[0] if job.args else {}
            meta = job.meta or {}
            print(
                f"  {job.id}  message={payload.get('id', '?')}  "
                f"origine={meta.get('original_job_id', '?')}  erreur={meta.get('error', '?')}"
            )
        return 0

    if args.action == "purge":
        count = len(dlq)
        dlq.empty()  # type: ignore[no-untyped-call]  # rq ships no annotation here
        print(f"{count} job(s) supprimé(s)")
        return 0

    inbound = get_queue(connection, settings)
    requeued = 0
    for job in dlq.get_jobs():
        payload = job.args[0] if job.args else None
        if not payload:
            continue
        inbound.enqueue(
            process_message,
            payload,
            job_timeout=settings.worker_job_timeout,
            retry=Retry(max=settings.worker_max_retries, interval=settings.retry_intervals),
        )
        job.delete()
        requeued += 1

    print(f"{requeued} job(s) remis en file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
