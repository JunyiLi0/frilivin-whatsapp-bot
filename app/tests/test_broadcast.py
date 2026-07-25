"""The CSV broadcast: parsing, authorisation, quota cap and receipts."""

from __future__ import annotations

from factories import FakeSender, make_message

from whatsapp_bot.config import Settings
from whatsapp_bot.processing.base import Context
from whatsapp_bot.processing.handlers.broadcast import (
    COUNTER_KEY,
    BroadcastHandler,
    parse_entries,
)
from whatsapp_bot.processing.handlers.fallback import FallbackLogHandler
from whatsapp_bot.worker.tasks import run_pipeline

COMMAND = "!envoi"
ADMIN = "33612345678@s.whatsapp.net"  # == factories.DEFAULT_SENDER_JID
STRANGER = "33999999999@s.whatsapp.net"
GROUP = "120363000000000000@g.us"

# The example from the original request, verbatim.
SAMPLE = """!envoi
33766793050; Hello this is a message
33784828374; Another one"""


def broadcast_settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update={"broadcast_admin_jids": ADMIN, **overrides})


# --- parsing -----------------------------------------------------------------


class TestParseEntries:
    def test_reads_the_sample(self) -> None:
        entries, errors = parse_entries(SAMPLE, COMMAND)

        assert errors == []
        assert [(entry.destination, entry.text) for entry in entries] == [
            ("33766793050", "Hello this is a message"),
            ("33784828374", "Another one"),
        ]
        assert [entry.line_number for entry in entries] == [2, 3]

    def test_list_may_start_on_the_command_line(self) -> None:
        entries, errors = parse_entries("!envoi 33766793050; Salut", COMMAND)

        assert errors == []
        assert len(entries) == 1
        assert entries[0].destination == "33766793050"
        assert entries[0].text == "Salut"

    def test_skips_blank_lines_and_comments(self) -> None:
        body = "!envoi\n\n# liste de juillet\n33766793050; Salut\n\n"
        entries, errors = parse_entries(body, COMMAND)

        assert errors == []
        assert len(entries) == 1

    def test_keeps_semicolons_inside_the_message(self) -> None:
        entries, _ = parse_entries("!envoi\n33766793050; a; b; c", COMMAND)

        assert entries[0].text == "a; b; c"

    def test_reports_a_missing_separator_with_its_line_number(self) -> None:
        body = "!envoi\n33766793050; ok\n33784828374 pas de séparateur"
        entries, errors = parse_entries(body, COMMAND)

        assert len(entries) == 1
        assert len(errors) == 1
        assert errors[0].line_number == 3
        assert "séparateur" in errors[0].reason

    def test_reports_empty_halves(self) -> None:
        body = "!envoi\n; message sans destinataire\n33766793050;"
        entries, errors = parse_entries(body, COMMAND)

        assert entries == []
        assert [error.reason for error in errors] == ["destinataire vide", "message vide"]

    def test_empty_body_yields_nothing(self) -> None:
        assert parse_entries("!envoi", COMMAND) == ([], [])


# --- matching and authorisation ----------------------------------------------


class TestAuthorisation:
    def test_disabled_without_admins(self, settings: Settings) -> None:
        assert (
            BroadcastHandler(settings.model_copy(update={"broadcast_admin_jids": ""})).enabled
            is False
        )

    def test_enabled_with_admins(self, settings: Settings) -> None:
        assert BroadcastHandler(broadcast_settings(settings)).enabled is True

    def test_admins_may_be_written_as_bare_numbers(self, settings: Settings) -> None:
        handler = BroadcastHandler(
            settings.model_copy(update={"broadcast_admin_jids": "33612345678"})
        )
        assert ADMIN in handler.admins

    def test_matches_the_command_in_private(self, settings: Settings) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))

        assert handler.match(make_message(SAMPLE))
        assert not handler.match(make_message("bonjour"))
        assert not handler.match(make_message(SAMPLE, is_group=True))

    def test_a_stranger_gets_no_answer(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        msg = make_message(SAMPLE, from_jid=STRANGER, chat_jid=STRANGER)

        # The command matches — refusal happens in run(), silently.
        assert handler.match(msg)
        assert handler.run(msg, context) is None
        assert sender.messages == []


# --- sending -----------------------------------------------------------------


class TestBroadcast:
    def test_sends_one_message_per_line(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        replies = handler.run(make_message(SAMPLE), context)

        assert sender.jids == [
            "33766793050@s.whatsapp.net",
            "33784828374@s.whatsapp.net",
        ]
        assert sender.texts == ["Hello this is a message", "Another one"]
        assert sender.handlers == ["BroadcastHandler", "BroadcastHandler"]

        assert replies is not None and len(replies) == 1
        assert "2 message(s)" in replies[0].text
        assert "+33766793050" in replies[0].text

    def test_receipt_quotes_the_request(self, settings: Settings, context: Context) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        replies = handler.run(make_message(SAMPLE, message_id="abc"), context)

        assert replies is not None
        assert replies[0].quoted_id == "abc"

    def test_sends_to_groups_too(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        handler.run(make_message(f"!envoi\n{GROUP}; Compte rendu"), context)

        assert sender.jids == [GROUP]

    def test_an_unreadable_destination_does_not_block_the_others(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        body = "!envoi\ntoto; perdu\n33766793050; passé"
        replies = handler.run(make_message(body), context)

        assert sender.texts == ["passé"]
        assert replies is not None
        assert "1 ligne(s) ignorée(s)" in replies[0].text
        assert "ligne 2" in replies[0].text
        assert "toto" in replies[0].text

    def test_counts_broadcasts_in_state(self, settings: Settings, context: Context) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))

        handler.run(make_message(SAMPLE), context)
        handler.run(make_message(SAMPLE, message_id="msg-2"), context)

        assert context.get_state(COUNTER_KEY) == "4"

    def test_shows_usage_when_the_list_is_empty(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        replies = handler.run(make_message("!envoi"), context)

        assert sender.messages == []
        assert replies is not None
        assert "un destinataire par ligne" in replies[0].text


# --- the quota cap -----------------------------------------------------------


class TestCap:
    def test_refuses_the_whole_batch_and_sends_nothing(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings, broadcast_max_recipients=2))
        body = "!envoi\n33766793050; a\n33784828374; b\n33612345679; c"

        replies = handler.run(make_message(body), context)

        assert sender.messages == []
        assert replies is not None
        assert "3 destinataires, maximum 2" in replies[0].text
        assert "Rien n'a été envoyé" in replies[0].text

    def test_cap_is_clamped_to_the_per_minute_quota(self, settings: Settings) -> None:
        handler = BroadcastHandler(
            broadcast_settings(settings, broadcast_max_recipients=25, rate_limit_per_minute=5)
        )
        # 5 sends a minute, one of which is the receipt.
        assert handler.max_recipients == 4

    def test_cap_is_left_alone_when_it_fits(self, settings: Settings) -> None:
        handler = BroadcastHandler(
            broadcast_settings(settings, broadcast_max_recipients=10, rate_limit_per_minute=30)
        )
        assert handler.max_recipients == 10


# --- aliases -----------------------------------------------------------------


class TestAliases:
    def test_resolves_an_alias_from_the_environment(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings, broadcast_aliases=f"nord={GROUP}"))
        handler.run(make_message("!envoi\nnord; Bonjour le nord"), context)

        assert sender.jids == [GROUP]

    def test_alias_lookup_ignores_case(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings, broadcast_aliases=f"nord={GROUP}"))
        handler.run(make_message("!envoi\nNORD; Bonjour"), context)

        assert sender.jids == [GROUP]

    def test_state_alias_wins_over_the_environment(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        other = "120363000000000009@g.us"
        context.set_state("broadcast:alias:nord", other)
        handler = BroadcastHandler(broadcast_settings(settings, broadcast_aliases=f"nord={GROUP}"))

        handler.run(make_message("!envoi\nnord; Bonjour"), context)

        assert sender.jids == [other]


# --- inside the pipeline ------------------------------------------------------


class TestInPipeline:
    def test_stops_the_chain_before_the_fallback(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        result = run_pipeline(make_message(SAMPLE), context, [handler, FallbackLogHandler()])

        assert result.executed == ["BroadcastHandler"]
        assert result.stopped_by == "BroadcastHandler"
        # two broadcasts sent by the handler, plus the receipt sent by the runner
        assert len(sender.messages) == 3

    def test_a_stranger_falls_through_to_the_fallback(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = BroadcastHandler(broadcast_settings(settings))
        msg = make_message(SAMPLE, from_jid=STRANGER, chat_jid=STRANGER)

        result = run_pipeline(msg, context, [handler, FallbackLogHandler()])

        # The command still stops the chain, but nothing was sent.
        assert result.stopped_by == "BroadcastHandler"
        assert sender.messages == []
