"""Deduplication, the message ledger and the key/value state table."""

from __future__ import annotations

import sqlite3

from factories import make_message

from whatsapp_bot import db as db_module
from whatsapp_bot.models import Outbound


class TestDeduplication:
    def test_a_new_message_is_recorded(self, conn: sqlite3.Connection) -> None:
        assert db_module.record_inbound(conn, make_message(message_id="m1")) is True

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["direction"] == "in"
        assert row["status"] == "received"

    def test_the_same_id_is_rejected_the_second_time(self, conn: sqlite3.Connection) -> None:
        msg = make_message(message_id="m1", text="premier")
        assert db_module.record_inbound(conn, msg) is True
        assert db_module.record_inbound(conn, msg) is False

    def test_a_duplicate_never_overwrites_the_original(self, conn: sqlite3.Connection) -> None:
        db_module.record_inbound(conn, make_message(message_id="m1", text="premier"))
        db_module.mark_status(conn, "m1", "processed")
        db_module.record_inbound(conn, make_message(message_id="m1", text="rejeu"))

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["text"] == "premier"
        assert row["status"] == "processed"

    def test_different_ids_coexist(self, conn: sqlite3.Connection) -> None:
        assert db_module.record_inbound(conn, make_message(message_id="m1")) is True
        assert db_module.record_inbound(conn, make_message(message_id="m2")) is True


class TestStatusTracking:
    def test_status_and_error_are_updated(self, conn: sqlite3.Connection) -> None:
        db_module.record_inbound(conn, make_message(message_id="m1"))
        db_module.mark_status(conn, "m1", "failed", error="boom", handler="X")

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["status"] == "failed"
        assert row["error"] == "boom"
        assert row["handler"] == "X"

    def test_success_clears_a_previous_error(self, conn: sqlite3.Connection) -> None:
        db_module.record_inbound(conn, make_message(message_id="m1"))
        db_module.mark_status(conn, "m1", "failed", error="boom")
        db_module.mark_status(conn, "m1", "processed")

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["error"] is None

    def test_attempts_only_increase_when_asked(self, conn: sqlite3.Connection) -> None:
        db_module.record_inbound(conn, make_message(message_id="m1"))

        db_module.mark_status(conn, "m1", "processing", bump_attempts=True)
        db_module.mark_status(conn, "m1", "processing", bump_attempts=True)
        db_module.mark_status(conn, "m1", "processed")

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["attempts"] == 2

    def test_the_handler_is_kept_when_not_provided(self, conn: sqlite3.Connection) -> None:
        db_module.record_inbound(conn, make_message(message_id="m1"))
        db_module.mark_status(conn, "m1", "processing", handler="PingHandler")
        db_module.mark_status(conn, "m1", "processed")

        row = db_module.get_message(conn, "m1")
        assert row is not None
        assert row["handler"] == "PingHandler"


class TestOutbound:
    def test_an_outbound_row_links_back_to_its_trigger(self, conn: sqlite3.Connection) -> None:
        out = Outbound(jid="120363000000000000@g.us", text="coucou", handler="Relay")
        db_module.record_outbound(conn, "out-1", out, status="queued", reply_to="m1")

        row = db_module.get_message(conn, "out-1")
        assert row is not None
        assert row["direction"] == "out"
        assert row["status"] == "queued"
        assert row["reply_to"] == "m1"
        assert row["handler"] == "Relay"
        assert row["is_group"] == 1

    def test_a_private_recipient_is_not_a_group(self, conn: sqlite3.Connection) -> None:
        out = Outbound(jid="33612345678@s.whatsapp.net", text="salut")
        db_module.record_outbound(conn, "out-2", out, status="sent")

        row = db_module.get_message(conn, "out-2")
        assert row is not None
        assert row["is_group"] == 0

    def test_recent_messages_are_newest_first(self, conn: sqlite3.Connection) -> None:
        for index in range(5):
            db_module.record_inbound(conn, make_message(message_id=f"m{index}"))

        rows = db_module.recent_messages(conn, limit=3)
        assert len(rows) == 3
        assert rows[0]["id"] == "m4"


class TestState:
    def test_missing_keys_return_the_default(self, conn: sqlite3.Connection) -> None:
        assert db_module.get_state(conn, "nope") is None
        assert db_module.get_state(conn, "nope", "default") == "default"

    def test_values_are_written_then_overwritten(self, conn: sqlite3.Connection) -> None:
        db_module.set_state(conn, "k", "v1")
        assert db_module.get_state(conn, "k") == "v1"

        db_module.set_state(conn, "k", "v2")
        assert db_module.get_state(conn, "k") == "v2"


def test_wal_mode_is_enabled(conn: sqlite3.Connection) -> None:
    """Both the api and the worker write to this file; WAL is what makes that work."""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
