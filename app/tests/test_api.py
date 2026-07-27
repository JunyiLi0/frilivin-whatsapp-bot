"""The ingest API: authentication, validation, deduplication, enqueueing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from whatsapp_bot import db as db_module
from whatsapp_bot.api.main import job_timeout_for
from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.models import InboundMessage

TOKEN = "test-token-0123456789"
AUTH = {"X-Bot-Token": TOKEN}


def payload(message_id: str = "wa-1", text: str = "!ping") -> dict[str, object]:
    return {
        "id": message_id,
        "from": "33612345678@s.whatsapp.net",
        "chat_jid": "33612345678@s.whatsapp.net",
        "is_group": False,
        "timestamp": 1700000000,
        "type": "text",
        "text": text,
        "quoted_id": None,
    }


@pytest.fixture
def client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redis_conn: fakeredis.FakeRedis
) -> Iterator[TestClient]:
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "bot.db"))
    get_settings.cache_clear()

    # No real Redis in tests: both the queue and /health go through fakeredis.
    from whatsapp_bot import queues
    from whatsapp_bot.api import main as api_main

    monkeypatch.setattr(queues, "get_redis", lambda: redis_conn)
    monkeypatch.setattr(api_main, "get_redis", lambda: redis_conn)

    with TestClient(api_main.app) as test_client:
        yield test_client

    get_settings.cache_clear()


class TestHealth:
    def test_reports_ok_when_both_dependencies_answer(self, client: TestClient) -> None:
        response = client.get("/health")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["checks"] == {"database": "ok", "redis": "ok"}

    def test_needs_no_token(self, client: TestClient) -> None:
        """Docker's healthcheck has no secret to present."""
        assert client.get("/health").status_code == 200


class TestWebhookAuth:
    def test_rejects_a_missing_token(self, client: TestClient) -> None:
        assert client.post("/webhook", json=payload()).status_code == 401

    def test_rejects_a_wrong_token(self, client: TestClient) -> None:
        response = client.post("/webhook", json=payload(), headers={"X-Bot-Token": "nope"})
        assert response.status_code == 401


class TestWebhook:
    def test_accepts_and_queues_a_message(self, client: TestClient) -> None:
        response = client.post("/webhook", json=payload(), headers=AUTH)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["id"] == "wa-1"
        assert body["job_id"]

    def test_the_same_id_is_only_queued_once(self, client: TestClient) -> None:
        first = client.post("/webhook", json=payload(), headers=AUTH)
        second = client.post("/webhook", json=payload(), headers=AUTH)

        assert first.json()["status"] == "queued"
        assert second.json()["status"] == "duplicate"
        assert second.json()["job_id"] is None

    def test_the_message_is_written_to_the_ledger(self, client: TestClient, tmp_path: Path) -> None:
        client.post("/webhook", json=payload(text="bonjour"), headers=AUTH)

        with db_module.session(str(tmp_path / "bot.db")) as conn:
            row = db_module.get_message(conn, "wa-1")

        assert row is not None
        assert row["status"] == "queued"
        assert row["text"] == "bonjour"
        assert row["direction"] == "in"

    def test_rejects_an_invalid_payload(self, client: TestClient) -> None:
        broken = payload()
        del broken["id"]

        assert client.post("/webhook", json=broken, headers=AUTH).status_code == 422

    def test_rejects_an_unknown_message_type(self, client: TestClient) -> None:
        broken = payload()
        broken["type"] = "hologram"

        assert client.post("/webhook", json=broken, headers=AUTH).status_code == 422

    def test_accepts_a_group_message(self, client: TestClient) -> None:
        body = payload(message_id="wa-group")
        body["is_group"] = True
        body["chat_jid"] = "120363000000000000@g.us"

        assert client.post("/webhook", json=body, headers=AUTH).status_code == 200


class TestJobTimeout:
    """Which budget the worker gets, decided at enqueue time."""

    def build(self, **fields: object) -> InboundMessage:
        return InboundMessage.model_validate({**payload(), **fields})

    def test_a_text_message_gets_the_default_budget(self, settings: Settings) -> None:
        assert job_timeout_for(self.build(), settings) == settings.worker_job_timeout

    def test_a_document_gets_the_document_budget(self, settings: Settings) -> None:
        msg = self.build(type="document", filename="commande.xlsx", media_path="/media/in/c.xlsx")

        assert job_timeout_for(msg, settings) == settings.worker_document_job_timeout

    def test_the_group_command_gets_the_directory_budget(self, settings: Settings) -> None:
        """It has to ask WhatsApp for the group list before it can answer."""
        msg = self.build(text="!envoigroupe\nNord; 337; Salut")

        assert job_timeout_for(msg, settings) == settings.worker_directory_job_timeout

    def test_the_plain_broadcast_keeps_the_default_budget(self, settings: Settings) -> None:
        msg = self.build(text="!envoi\n33766793050; Salut")

        assert job_timeout_for(msg, settings) == settings.worker_job_timeout


class TestSendStatus:
    def test_settles_an_outbound_message(self, client: TestClient, tmp_path: Path) -> None:
        from whatsapp_bot.models import Outbound

        with db_module.session(str(tmp_path / "bot.db")) as conn:
            db_module.record_outbound(
                conn, "out-1", Outbound(jid="x@g.us", text="hi"), status="queued"
            )

        response = client.post(
            "/internal/send-status",
            json={"id": "out-1", "status": "sent", "wa_message_id": "WA123"},
            headers=AUTH,
        )
        assert response.status_code == 204

        with db_module.session(str(tmp_path / "bot.db")) as conn:
            row = db_module.get_message(conn, "out-1")
        assert row is not None
        assert row["status"] == "sent"

    def test_records_a_failure_with_its_error(self, client: TestClient, tmp_path: Path) -> None:
        from whatsapp_bot.models import Outbound

        with db_module.session(str(tmp_path / "bot.db")) as conn:
            db_module.record_outbound(
                conn, "out-2", Outbound(jid="x@g.us", text="hi"), status="queued"
            )

        client.post(
            "/internal/send-status",
            json={"id": "out-2", "status": "failed", "error": "not connected"},
            headers=AUTH,
        )

        with db_module.session(str(tmp_path / "bot.db")) as conn:
            row = db_module.get_message(conn, "out-2")
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == "not connected"

    def test_requires_a_token(self, client: TestClient) -> None:
        response = client.post("/internal/send-status", json={"id": "out-3", "status": "sent"})
        assert response.status_code == 401


class TestGroups:
    def test_requires_a_token(self, client: TestClient) -> None:
        assert client.get("/groups").status_code == 401

    def test_reports_503_when_the_bridge_is_down(self, client: TestClient) -> None:
        response = client.get("/groups", headers=AUTH)
        assert response.status_code == 503
