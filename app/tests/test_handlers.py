"""Behaviour of the three shipped handlers."""

from __future__ import annotations

from factories import DEFAULT_GROUP_JID, FakeSender, make_message

from whatsapp_bot.config import Settings
from whatsapp_bot.processing.base import Context
from whatsapp_bot.processing.handlers.fallback import FallbackLogHandler
from whatsapp_bot.processing.handlers.group_relay import GroupRelayHandler, display_name
from whatsapp_bot.processing.handlers.ping import PingHandler
from whatsapp_bot.worker.tasks import run_pipeline

TARGET_A = "120363000000000001@g.us"
TARGET_B = "120363000000000002@g.us"


def relay_settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(
        update={
            "relay_keywords": "urgent,alerte",
            "relay_target_jids": f"{TARGET_A},{TARGET_B}",
            **overrides,
        }
    )


# --- PingHandler -------------------------------------------------------------


class TestPingHandler:
    def test_matches_the_command(self, settings: Settings) -> None:
        handler = PingHandler(settings)
        assert handler.match(make_message("!ping"))
        assert handler.match(make_message("  !PING  "))
        assert handler.match(make_message("!ping extra words"))

    def test_ignores_anything_else(self, settings: Settings) -> None:
        handler = PingHandler(settings)
        assert not handler.match(make_message("ping"))
        assert not handler.match(make_message("dis !ping"))
        assert not handler.match(make_message(""))

    def test_answers_pong_quoting_the_question(self, settings: Settings, context: Context) -> None:
        msg = make_message("!ping", message_id="abc")
        replies = PingHandler(settings).run(msg, context)

        assert replies is not None
        assert len(replies) == 1
        assert replies[0].text.startswith("pong")
        assert replies[0].jid == msg.chat_jid
        assert replies[0].quoted_id == "abc"

    def test_stops_the_chain(self, settings: Settings) -> None:
        assert PingHandler(settings).stop_propagation is True

    def test_can_be_switched_off(self, settings: Settings) -> None:
        handler = PingHandler(settings.model_copy(update={"ping_enabled": False}))
        assert handler.enabled is False


# --- GroupRelayHandler -------------------------------------------------------


class TestGroupRelayHandler:
    def test_enabled_only_when_fully_configured(self, settings: Settings) -> None:
        assert GroupRelayHandler(relay_settings(settings)).enabled is True
        assert GroupRelayHandler(settings).enabled is False
        assert GroupRelayHandler(relay_settings(settings, relay_target_jids="")).enabled is False
        assert GroupRelayHandler(relay_settings(settings, relay_keywords="")).enabled is False

    def test_matches_a_keyword_regardless_of_case(self, settings: Settings) -> None:
        handler = GroupRelayHandler(relay_settings(settings))
        assert handler.match(make_message("c'est URGENT"))
        assert handler.match(make_message("alerte rouge"))
        assert not handler.match(make_message("bonjour"))

    def test_never_relays_group_traffic(self, settings: Settings) -> None:
        """Relaying groups into groups is how you build an infinite loop."""
        handler = GroupRelayHandler(relay_settings(settings))
        assert not handler.match(make_message("urgent", is_group=True))

    def test_ignores_non_text_messages(self, settings: Settings) -> None:
        handler = GroupRelayHandler(relay_settings(settings))
        assert not handler.match(make_message("urgent", msg_type="image"))

    def test_relays_to_every_target(self, settings: Settings, context: Context) -> None:
        handler = GroupRelayHandler(relay_settings(settings))
        replies = handler.run(make_message("urgent: fuite d'eau"), context)

        assert replies is not None
        assert [reply.jid for reply in replies] == [TARGET_A, TARGET_B]
        for reply in replies:
            assert "urgent: fuite d'eau" in reply.text
            assert "+33612345678" in reply.text

    def test_counts_relays_in_the_state_table(self, settings: Settings, context: Context) -> None:
        handler = GroupRelayHandler(relay_settings(settings))
        handler.run(make_message("urgent"), context)
        handler.run(make_message("urgent"), context)

        assert context.get_state("relay:count") == "2"


def test_display_name_formats_phone_numbers() -> None:
    assert display_name("33612345678@s.whatsapp.net") == "+33612345678"
    assert display_name("33612345678:12@s.whatsapp.net") == "+33612345678"
    assert display_name(DEFAULT_GROUP_JID) == "120363000000000000"


# --- FallbackLogHandler ------------------------------------------------------


class TestFallbackLogHandler:
    def test_matches_everything(self) -> None:
        handler = FallbackLogHandler()
        assert handler.match(make_message("n'importe quoi"))
        assert handler.match(make_message("", msg_type="sticker"))

    def test_never_answers(self, context: Context, sender: FakeSender) -> None:
        assert FallbackLogHandler().run(make_message("hello"), context) is None
        assert sender.messages == []

    def test_runs_last(self) -> None:
        assert FallbackLogHandler().priority > PingHandler.priority

    def test_knows_whether_someone_already_replied(
        self, settings: Settings, context: Context
    ) -> None:
        handlers = [PingHandler(settings), FallbackLogHandler()]
        run_pipeline(make_message("!ping"), context, handlers)

        # PingHandler stops propagation, so the fallback never runs on a command.
        assert context.sent_count == 1


def test_unhandled_message_reaches_only_the_fallback(
    settings: Settings, context: Context, sender: FakeSender
) -> None:
    handlers = [PingHandler(settings), FallbackLogHandler()]
    result = run_pipeline(make_message("bonjour tout le monde"), context, handlers)

    assert result.executed == ["FallbackLogHandler"]
    assert sender.messages == []
