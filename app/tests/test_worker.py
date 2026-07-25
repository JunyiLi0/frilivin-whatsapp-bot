"""The RQ job end to end, and what happens when it keeps failing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import fakeredis
import pytest
from factories import make_message

from whatsapp_bot import db as db_module
from whatsapp_bot.config import get_settings
from whatsapp_bot.models import Outbound

TOKEN = "test-token-0123456789"


class FakeBridgeClient:
    """Stands in for the HTTP client the job builds for itself."""

    sent: ClassVar[list[tuple[str, Outbound]]] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def send(self, outbound_id: str, out: Outbound) -> str:
        if FakeBridgeClient.fail_with is not None:
            raise FakeBridgeClient.fail_with
        FakeBridgeClient.sent.append((outbound_id, out))
        return outbound_id

    def close(self) -> None:
        pass


@pytest.fixture
def job_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redis_conn: fakeredis.FakeRedis
) -> Path:
    """Point the job at a temp database, fakeredis and a fake bridge."""
    database = tmp_path / "bot.db"
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_PATH", str(database))
    get_settings.cache_clear()

    from whatsapp_bot.worker import tasks

    FakeBridgeClient.sent = []
    FakeBridgeClient.fail_with = None
    monkeypatch.setattr(tasks, "make_redis", lambda _url: redis_conn)
    monkeypatch.setattr(tasks, "BridgeClient", FakeBridgeClient)

    return database


class TestProcessMessage:
    def test_a_command_is_answered_and_marked_processed(self, job_env: Path) -> None:
        from whatsapp_bot.worker.tasks import process_message

        msg = make_message("!ping", message_id="wa-1")
        with db_module.session(str(job_env)) as conn:
            db_module.record_inbound(conn, msg)

        result = process_message(msg.model_dump())

        assert result["handlers"] == ["PingHandler"]
        assert result["stopped_by"] == "PingHandler"
        assert result["sent"] == 1
        assert len(FakeBridgeClient.sent) == 1
        assert FakeBridgeClient.sent[0][1].text.startswith("pong")

        with db_module.session(str(job_env)) as conn:
            row = db_module.get_message(conn, "wa-1")
        assert row is not None
        assert row["status"] == "processed"
        assert row["attempts"] == 1

    def test_an_ordinary_message_only_reaches_the_fallback(self, job_env: Path) -> None:
        from whatsapp_bot.worker.tasks import process_message

        msg = make_message("bonjour", message_id="wa-2")
        with db_module.session(str(job_env)) as conn:
            db_module.record_inbound(conn, msg)

        result = process_message(msg.model_dump())

        assert result["handlers"] == ["FallbackLogHandler"]
        assert result["sent"] == 0
        assert FakeBridgeClient.sent == []

    def test_it_stays_well_inside_the_two_second_budget(self, job_env: Path) -> None:
        from whatsapp_bot.worker.tasks import process_message

        msg = make_message("!ping", message_id="wa-3")
        with db_module.session(str(job_env)) as conn:
            db_module.record_inbound(conn, msg)

        result = process_message(msg.model_dump())

        assert result["duration_ms"] < 2000

    def test_a_bridge_outage_marks_the_message_failed_and_raises(self, job_env: Path) -> None:
        from whatsapp_bot.bridge_client import BridgeError
        from whatsapp_bot.worker.tasks import process_message

        FakeBridgeClient.fail_with = BridgeError("bridge unreachable")
        msg = make_message("!ping", message_id="wa-4")
        with db_module.session(str(job_env)) as conn:
            db_module.record_inbound(conn, msg)

        # Raising is what triggers RQ's retry.
        with pytest.raises(BridgeError):
            process_message(msg.model_dump())

        with db_module.session(str(job_env)) as conn:
            row = db_module.get_message(conn, "wa-4")
        assert row is not None
        assert row["status"] == "failed"

    def test_an_invalid_payload_is_rejected_before_any_work(self, job_env: Path) -> None:
        from pydantic import ValidationError

        from whatsapp_bot.worker.tasks import process_message

        with pytest.raises(ValidationError):
            process_message({"id": "wa-5"})


class FakeJob:
    def __init__(self, *, retries_left: int | None, payload: dict[str, Any]) -> None:
        self.id = "job-1"
        self.args = (payload,)
        self.retries_left = retries_left

    @property
    def should_retry(self) -> bool:
        return self.retries_left is not None and self.retries_left > 0


class TestDeadLetterQueue:
    def test_a_job_with_retries_left_is_not_dead_lettered(
        self, job_env: Path, redis_conn: fakeredis.FakeRedis
    ) -> None:
        from whatsapp_bot.queues import get_dead_letter_queue
        from whatsapp_bot.worker.main import make_dead_letter_handler

        settings = get_settings()
        handler = make_dead_letter_handler(settings, redis_conn)
        job = FakeJob(retries_left=2, payload={"id": "wa-6"})

        handler(job, ValueError, ValueError("boom"), None)

        assert len(get_dead_letter_queue(redis_conn, settings)) == 0

    def test_an_exhausted_job_is_parked_and_marked_dead(
        self, job_env: Path, redis_conn: fakeredis.FakeRedis
    ) -> None:
        from whatsapp_bot.queues import get_dead_letter_queue
        from whatsapp_bot.worker.main import make_dead_letter_handler

        msg = make_message("!ping", message_id="wa-7")
        with db_module.session(str(job_env)) as conn:
            db_module.record_inbound(conn, msg)

        settings = get_settings()
        handler = make_dead_letter_handler(settings, redis_conn)
        job = FakeJob(retries_left=0, payload=msg.model_dump())

        handler(job, ValueError, ValueError("boom"), None)

        dlq = get_dead_letter_queue(redis_conn, settings)
        assert len(dlq) == 1
        assert dlq.get_jobs()[0].args[0]["id"] == "wa-7"

        with db_module.session(str(job_env)) as conn:
            row = db_module.get_message(conn, "wa-7")
        assert row is not None
        assert row["status"] == "dead"
        assert "boom" in row["error"]
