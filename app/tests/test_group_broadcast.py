"""``!envoigroupe``: parsing, group resolution, refusals and receipts."""

from __future__ import annotations

from typing import Any

from factories import FakeSender, make_message
from test_groups import DUPONT, NORD, SUD, FakeBridge

from whatsapp_bot.bridge_client import BridgeError
from whatsapp_bot.config import Settings
from whatsapp_bot.groups import Group, GroupDirectory
from whatsapp_bot.processing.base import Context
from whatsapp_bot.processing.handlers.fallback import FallbackLogHandler
from whatsapp_bot.processing.handlers.group_broadcast import (
    COUNTER_KEY,
    GroupBroadcastHandler,
    parse_entries,
)
from whatsapp_bot.worker.tasks import run_pipeline

COMMAND = "!envoigroupe"
ADMIN = "33612345678@s.whatsapp.net"  # == factories.DEFAULT_SENDER_JID
STRANGER = "33999999999@s.whatsapp.net"

# The example from the original request, transposed onto the test groups.
SAMPLE = """!envoigroupe
Chantier Nord; 33766660673; Livraison décalée
Dupont; 33788888888; Le devis est parti"""


def rows(*groups: Group) -> list[dict[str, Any]]:
    return [
        {"jid": group.jid, "subject": group.subject, "numbers": list(group.numbers)}
        for group in groups
    ]


def make_handler(
    settings: Settings,
    *groups: Group,
    fail_with: Exception | None = None,
    **overrides: object,
) -> GroupBroadcastHandler:
    """A handler wired to a fake bridge holding ``groups``."""
    bridge = FakeBridge(rows(*groups), fail_with=fail_with)
    directory = GroupDirectory(bridge)  # type: ignore[arg-type]
    updated = settings.model_copy(update={"broadcast_admin_jids": ADMIN, **overrides})
    return GroupBroadcastHandler(updated, directory)


# --- parsing -----------------------------------------------------------------


class TestParseEntries:
    def test_reads_the_sample(self) -> None:
        entries, errors = parse_entries(SAMPLE, COMMAND)

        assert errors == []
        assert [(entry.name, entry.number, entry.text) for entry in entries] == [
            ("Chantier Nord", "33766660673", "Livraison décalée"),
            ("Dupont", "33788888888", "Le devis est parti"),
        ]
        assert [entry.line_number for entry in entries] == [2, 3]

    def test_the_name_may_be_empty(self) -> None:
        entries, errors = parse_entries("!envoigroupe\n; 33766660673; Salut", COMMAND)

        assert errors == []
        assert (entries[0].name, entries[0].number) == ("", "33766660673")

    def test_the_number_may_be_empty(self) -> None:
        entries, errors = parse_entries("!envoigroupe\nNord; ; Salut", COMMAND)

        assert errors == []
        assert (entries[0].name, entries[0].number) == ("Nord", "")

    def test_both_empty_is_refused(self) -> None:
        entries, errors = parse_entries("!envoigroupe\n; ; Salut", COMMAND)

        assert entries == []
        assert "au moins un" in errors[0].reason

    def test_list_may_start_on_the_command_line(self) -> None:
        entries, errors = parse_entries("!envoigroupe Nord; 337; Salut", COMMAND)

        assert errors == []
        assert (entries[0].name, entries[0].number, entries[0].text) == ("Nord", "337", "Salut")

    def test_keeps_semicolons_inside_the_message(self) -> None:
        entries, _ = parse_entries("!envoigroupe\nNord; 337; a; b; c", COMMAND)

        assert entries[0].text == "a; b; c"

    def test_skips_blank_lines_and_comments(self) -> None:
        body = "!envoigroupe\n\n# liste de juillet\nNord; 337; Salut\n\n"
        entries, errors = parse_entries(body, COMMAND)

        assert errors == []
        assert len(entries) == 1

    def test_reports_a_missing_separator_with_its_line_number(self) -> None:
        body = "!envoigroupe\nNord; 337; ok\nSud; pas de troisième champ"
        entries, errors = parse_entries(body, COMMAND)

        assert len(entries) == 1
        assert errors[0].line_number == 3
        assert "format attendu" in errors[0].reason

    def test_reports_an_empty_message(self) -> None:
        _, errors = parse_entries("!envoigroupe\nNord; 337;", COMMAND)

        assert [error.reason for error in errors] == ["message vide"]

    def test_empty_body_yields_nothing(self) -> None:
        assert parse_entries("!envoigroupe", COMMAND) == ([], [])


# --- matching and authorisation ----------------------------------------------


class TestAuthorisation:
    def test_disabled_without_admins(self, settings: Settings) -> None:
        assert GroupBroadcastHandler(settings).enabled is False

    def test_reuses_the_broadcast_allowlist(self, settings: Settings) -> None:
        handler = GroupBroadcastHandler(settings.model_copy(update={"broadcast_admin_jids": ADMIN}))

        assert handler.enabled is True
        assert handler.admins == {ADMIN}

    def test_a_dedicated_allowlist_wins(self, settings: Settings) -> None:
        handler = GroupBroadcastHandler(
            settings.model_copy(
                update={
                    "broadcast_admin_jids": ADMIN,
                    "group_broadcast_admin_jids": "33698765432",
                }
            )
        )

        assert handler.admins == {"33698765432@s.whatsapp.net"}

    def test_matches_its_own_command_only(self, settings: Settings) -> None:
        handler = make_handler(settings, NORD)

        assert handler.match(make_message(SAMPLE))
        assert not handler.match(make_message("!envoi\n33766793050; Salut"))
        assert not handler.match(make_message(SAMPLE, is_group=True))

    def test_a_stranger_gets_no_answer(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD)
        msg = make_message(SAMPLE, from_jid=STRANGER, chat_jid=STRANGER)

        assert handler.match(msg)
        assert handler.run(msg, context) is None
        assert sender.messages == []


# --- sending -----------------------------------------------------------------


class TestSending:
    def test_sends_one_message_per_group(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message(SAMPLE), context)

        assert sender.jids == [NORD.jid, DUPONT.jid]
        assert sender.texts == ["Livraison décalée", "Le devis est parti"]
        assert sender.handlers == ["GroupBroadcastHandler", "GroupBroadcastHandler"]

        assert replies is not None and len(replies) == 1
        assert "2 message(s)" in replies[0].text
        assert "Chantier Nord" in replies[0].text

    def test_partial_fragments_are_enough(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        """« No; 337; message 2 » must reach « Chantier Nord »."""
        handler = make_handler(settings, NORD, SUD, DUPONT)

        handler.run(make_message("!envoigroupe\nNo; 337; message 2"), context)

        assert sender.jids == [NORD.jid]
        assert sender.texts == ["message 2"]

    def test_the_name_alone_is_enough(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        handler.run(make_message("!envoigroupe\nDupont; ; Salut"), context)

        assert sender.jids == [DUPONT.jid]

    def test_the_number_alone_is_enough(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        handler.run(make_message("!envoigroupe\n; 33700000001; Salut"), context)

        assert sender.jids == [SUD.jid]

    def test_a_national_number_reaches_its_group(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        handler.run(make_message("!envoigroupe\n; 07 00 00 00 01; Salut"), context)

        assert sender.jids == [SUD.jid]

    def test_receipt_quotes_the_request(self, settings: Settings, context: Context) -> None:
        handler = make_handler(settings, NORD, DUPONT)

        replies = handler.run(make_message(SAMPLE, message_id="abc"), context)

        assert replies is not None
        assert replies[0].quoted_id == "abc"

    def test_counts_what_was_sent(self, settings: Settings, context: Context) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        handler.run(make_message(SAMPLE), context)
        handler.run(make_message(SAMPLE, message_id="msg-2"), context)

        assert context.get_state(COUNTER_KEY) == "4"

    def test_shows_usage_and_the_known_groups_when_the_list_is_empty(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD)

        replies = handler.run(make_message("!envoigroupe"), context)

        assert sender.messages == []
        assert replies is not None
        assert "une ligne par groupe" in replies[0].text
        assert "Chantier Nord" in replies[0].text


# --- refusals ----------------------------------------------------------------


class TestRefusals:
    def test_an_unknown_group_is_reported_with_its_reason(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message("!envoigroupe\nBretagne; ; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "1 ligne(s) rejetée(s)" in replies[0].text
        assert "ligne 2" in replies[0].text
        assert "aucun groupe ne correspond" in replies[0].text
        assert "Bretagne" in replies[0].text

    def test_several_matches_are_reported_by_name(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message("!envoigroupe\nChantier; ; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "2 groupes correspondent" in replies[0].text
        assert "Chantier Nord" in replies[0].text
        assert "Chantier Sud" in replies[0].text
        assert "précisez" in replies[0].text

    def test_a_shared_number_without_a_name_is_ambiguous(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message("!envoigroupe\n; 33766660673; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "2 groupes correspondent" in replies[0].text

    def test_a_too_short_number_is_refused(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message("!envoigroupe\n; 33; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "trop court" in replies[0].text

    def test_a_number_without_a_single_digit_is_refused(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        replies = handler.run(make_message("!envoigroupe\n; à définir; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "illisible" in replies[0].text

    def test_a_rejected_line_does_not_block_the_others(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)
        body = "!envoigroupe\nBretagne; ; perdu\nDupont; ; passé"

        replies = handler.run(make_message(body), context)

        assert sender.texts == ["passé"]
        assert replies is not None
        assert "1 message(s)" in replies[0].text
        assert "1 ligne(s) rejetée(s)" in replies[0].text

    def test_nothing_is_sent_when_the_bridge_cannot_list_the_groups(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, fail_with=BridgeError("bridge unreachable"))

        replies = handler.run(make_message(SAMPLE), context)

        assert sender.messages == []
        assert replies is not None
        assert "Impossible de récupérer la liste des groupes" in replies[0].text

    def test_usage_survives_an_unreachable_bridge(
        self, settings: Settings, context: Context
    ) -> None:
        handler = make_handler(settings, NORD, fail_with=BridgeError("bridge unreachable"))

        replies = handler.run(make_message("!envoigroupe"), context)

        assert replies is not None
        assert "une ligne par groupe" in replies[0].text


# --- the cache ---------------------------------------------------------------


class TestDirectoryUse:
    def test_one_fetch_serves_a_whole_batch(self, settings: Settings, context: Context) -> None:
        bridge = FakeBridge(rows(NORD, SUD, DUPONT))
        handler = GroupBroadcastHandler(
            settings.model_copy(update={"broadcast_admin_jids": ADMIN}),
            GroupDirectory(bridge),  # type: ignore[arg-type]
        )

        handler.run(make_message(SAMPLE), context)

        assert bridge.calls == 1

    def test_a_miss_on_a_cached_list_triggers_one_refresh(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        """A group joined since the last fetch must not look like a typo."""
        bridge = FakeBridge(rows(NORD))
        handler = GroupBroadcastHandler(
            settings.model_copy(update={"broadcast_admin_jids": ADMIN}),
            GroupDirectory(bridge),  # type: ignore[arg-type]
        )
        handler.directory.snapshot()  # warm the cache without SUD in it
        bridge.rows = rows(NORD, SUD)

        handler.run(make_message("!envoigroupe\nSud; ; Salut"), context)

        assert bridge.calls == 2
        assert sender.jids == [SUD.jid]

    def test_a_hit_does_not_refresh(self, settings: Settings, context: Context) -> None:
        bridge = FakeBridge(rows(NORD))
        handler = GroupBroadcastHandler(
            settings.model_copy(update={"broadcast_admin_jids": ADMIN}),
            GroupDirectory(bridge),  # type: ignore[arg-type]
        )
        handler.directory.snapshot()

        handler.run(make_message("!envoigroupe\nNord; ; Salut"), context)

        assert bridge.calls == 1

    def test_a_failed_refresh_still_reports_the_miss(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        bridge = FakeBridge(rows(NORD))
        handler = GroupBroadcastHandler(
            settings.model_copy(update={"broadcast_admin_jids": ADMIN}),
            GroupDirectory(bridge),  # type: ignore[arg-type]
        )
        handler.directory.snapshot()
        bridge.fail_with = BridgeError("bridge unreachable")

        replies = handler.run(make_message("!envoigroupe\nBretagne; ; Salut"), context)

        assert sender.messages == []
        assert replies is not None
        assert "aucun groupe ne correspond" in replies[0].text


# --- the quota cap -----------------------------------------------------------


class TestCap:
    def test_refuses_the_whole_batch_and_sends_nothing(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT, group_broadcast_max_recipients=2)
        body = "!envoigroupe\nNord; ; a\nSud; ; b\nDupont; ; c"

        replies = handler.run(make_message(body), context)

        assert sender.messages == []
        assert replies is not None
        assert "3 groupes, maximum 2" in replies[0].text
        assert "Rien n'a été envoyé" in replies[0].text

    def test_cap_is_clamped_to_the_per_minute_quota(self, settings: Settings) -> None:
        handler = make_handler(
            settings, NORD, group_broadcast_max_recipients=25, rate_limit_per_minute=5
        )

        assert handler.max_recipients == 4

    def test_cap_is_left_alone_when_it_fits(self, settings: Settings) -> None:
        handler = make_handler(
            settings, NORD, group_broadcast_max_recipients=10, rate_limit_per_minute=30
        )

        assert handler.max_recipients == 10


# --- inside the pipeline ------------------------------------------------------


class TestInPipeline:
    def test_stops_the_chain_before_the_fallback(
        self, settings: Settings, context: Context, sender: FakeSender
    ) -> None:
        handler = make_handler(settings, NORD, SUD, DUPONT)

        result = run_pipeline(make_message(SAMPLE), context, [handler, FallbackLogHandler()])

        assert result.executed == ["GroupBroadcastHandler"]
        assert result.stopped_by == "GroupBroadcastHandler"
        # two messages sent by the handler, plus the receipt sent by the runner
        assert len(sender.messages) == 3
