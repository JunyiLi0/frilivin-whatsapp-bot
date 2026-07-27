"""Designating a group by fragments: normalisation, ranking, and the cache."""

from __future__ import annotations

from typing import Any

import pytest

from whatsapp_bot.bridge_client import BridgeError
from whatsapp_bot.groups import (
    Group,
    GroupDirectory,
    Tier,
    digits_only,
    find_group,
    fold,
    number_variants,
    parse_group,
)

NORD = Group("120363000000000001@g.us", "Chantier Nord", ("33766660673", "33612345678"))
SUD = Group("120363000000000002@g.us", "Chantier Sud", ("33700000001",))
DUPONT = Group("120363000000000003@g.us", "Client Dupont", ("33788888888", "33766660673"))

ALL = (NORD, SUD, DUPONT)


class TestNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Chantier Nord", "chantier nord"),
            ("Île-de-France", "ile de france"),
            ("  ÉQUIPE  «  A  »  ", "equipe a"),
            ("", ""),
        ],
    )
    def test_folds_case_accents_and_punctuation(self, raw: str, expected: str) -> None:
        assert fold(raw) == expected

    def test_keeps_only_digits(self) -> None:
        assert digits_only("+33 (0)7-66.66.06.73") == "330766660673"

    def test_national_form_gets_an_international_variant(self) -> None:
        assert number_variants("0766660673", "33") == ("0766660673", "33766660673")

    def test_international_form_is_left_alone(self) -> None:
        assert number_variants("33766660673", "33") == ("33766660673",)

    def test_no_country_code_means_no_variant(self) -> None:
        assert number_variants("0766660673", "") == ("0766660673",)


class TestParseGroup:
    def test_reads_a_bridge_row(self) -> None:
        row: dict[str, Any] = {
            "jid": "120363000000000001@g.us",
            "subject": "Chantier Nord",
            "participants": 2,
            "numbers": ["33766660673", "33612345678"],
        }
        assert parse_group(row) == NORD

    def test_survives_a_row_without_numbers(self) -> None:
        assert parse_group({"jid": "1@g.us", "subject": "X"}) == Group("1@g.us", "X", ())

    def test_label_falls_back_to_the_jid(self) -> None:
        assert Group("1@g.us", "").label == "1@g.us"


class TestFindByName:
    def test_exact_name(self) -> None:
        assert find_group(ALL, name="Chantier Nord").group is NORD

    def test_partial_name(self) -> None:
        assert find_group(ALL, name="No").group is NORD

    def test_ignores_case_and_accents(self) -> None:
        assert find_group(ALL, name="chàntier sud").group is SUD

    def test_matches_on_a_word_inside_the_subject(self) -> None:
        assert find_group(ALL, name="dupont").group is DUPONT

    def test_a_fragment_shared_by_two_groups_is_ambiguous(self) -> None:
        lookup = find_group(ALL, name="Chantier")

        assert lookup.group is None
        assert lookup.ambiguous
        assert set(lookup.candidates) == {NORD, SUD}

    def test_an_exact_name_wins_over_a_mere_fragment(self) -> None:
        precise = Group("4@g.us", "Nord", ())
        # "Nord" is the whole name of one group and part of another's.
        assert find_group([*ALL, precise], name="Nord").group is precise

    def test_unknown_name_finds_nothing(self) -> None:
        lookup = find_group(ALL, name="Bretagne")

        assert lookup.group is None
        assert lookup.candidates == ()
        assert not lookup.ambiguous


class TestFindByNumber:
    def test_full_number(self) -> None:
        assert find_group(ALL, number="33700000001").group is SUD

    def test_partial_number(self) -> None:
        assert find_group(ALL, number="337000").group is SUD

    def test_formatted_number(self) -> None:
        assert find_group(ALL, number="+33 7 00 00 00 01").group is SUD

    def test_national_form_with_a_country_code(self) -> None:
        assert find_group(ALL, number="07 00 00 00 01", country_code="33").group is SUD

    def test_a_number_in_two_groups_is_ambiguous(self) -> None:
        lookup = find_group(ALL, number="33766660673")

        assert lookup.group is None
        assert set(lookup.candidates) == {NORD, DUPONT}

    def test_unknown_number_finds_nothing(self) -> None:
        assert find_group(ALL, number="33999999999").candidates == ()


class TestFindByBoth:
    def test_the_pair_from_the_request(self) -> None:
        """« No; 337; message » must reach « Chantier Nord »."""
        assert find_group(ALL, name="No", number="337").group is NORD

    def test_both_criteria_must_match(self) -> None:
        # The number belongs to Dupont, the name to Nord: nothing satisfies both.
        assert find_group(ALL, name="Nord", number="33788888888").candidates == ()

    def test_the_name_disambiguates_a_shared_number(self) -> None:
        assert find_group(ALL, name="Dupont", number="33766660673").group is DUPONT

    def test_the_number_disambiguates_a_shared_name(self) -> None:
        assert find_group(ALL, name="Chantier", number="337000").group is SUD

    def test_two_empty_criteria_find_nothing(self) -> None:
        assert find_group(ALL).candidates == ()

    def test_the_weakest_criterion_rates_the_group(self) -> None:
        """A group matching both halves well beats one matching a half perfectly."""
        loose = Group("5@g.us", "Nordique", ("33700000009",))
        # "Nord" is a prefix of both subjects, but only NORD's number matches
        # as a prefix too; loose only matches "337" deep inside its number.
        assert find_group([NORD, loose], name="Nord", number="337666").group is NORD


class TestTierOrder:
    def test_exact_beats_prefix_beats_contains(self) -> None:
        assert Tier.EXACT < Tier.PREFIX < Tier.CONTAINS


class FakeBridge:
    """A bridge client that counts calls and can be told to fail."""

    def __init__(self, rows: list[dict[str, Any]], fail_with: Exception | None = None) -> None:
        self.rows = rows
        self.fail_with = fail_with
        self.calls = 0

    def groups(self) -> list[dict[str, Any]]:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.rows


def make_directory(bridge: FakeBridge, ttl: float = 300.0) -> tuple[GroupDirectory, list[float]]:
    """A directory on a fake clock — the list is the mutable 'now'."""
    now = [0.0]
    directory = GroupDirectory(bridge, ttl_seconds=ttl, clock=lambda: now[0])  # type: ignore[arg-type]
    return directory, now


class TestDirectory:
    def test_first_call_fetches(self) -> None:
        bridge = FakeBridge([{"jid": "1@g.us", "subject": "Nord", "numbers": ["33766660673"]}])
        directory, _ = make_directory(bridge)

        snapshot = directory.snapshot()

        assert bridge.calls == 1
        assert snapshot.cached is False
        assert snapshot.groups == (Group("1@g.us", "Nord", ("33766660673",)),)

    def test_second_call_is_served_from_the_cache(self) -> None:
        bridge = FakeBridge([{"jid": "1@g.us", "subject": "Nord"}])
        directory, _ = make_directory(bridge)

        directory.snapshot()
        snapshot = directory.snapshot()

        assert bridge.calls == 1
        assert snapshot.cached is True

    def test_the_cache_expires(self) -> None:
        bridge = FakeBridge([{"jid": "1@g.us", "subject": "Nord"}])
        directory, now = make_directory(bridge, ttl=300.0)

        directory.snapshot()
        now[0] = 301.0

        assert directory.snapshot().cached is False
        assert bridge.calls == 2

    def test_force_ignores_a_fresh_cache(self) -> None:
        bridge = FakeBridge([{"jid": "1@g.us", "subject": "Nord"}])
        directory, _ = make_directory(bridge)

        directory.snapshot()
        directory.snapshot(force=True)

        assert bridge.calls == 2

    def test_invalidate_forces_the_next_fetch(self) -> None:
        bridge = FakeBridge([{"jid": "1@g.us", "subject": "Nord"}])
        directory, _ = make_directory(bridge)

        directory.snapshot()
        directory.invalidate()
        directory.snapshot()

        assert bridge.calls == 2

    def test_a_bridge_failure_surfaces(self) -> None:
        bridge = FakeBridge([], fail_with=BridgeError("cannot list groups: boom"))
        directory, _ = make_directory(bridge)

        with pytest.raises(BridgeError):
            directory.snapshot()
