"""Reading a destination the way an operator would write it."""

from __future__ import annotations

import pytest

from whatsapp_bot.processing.jids import display_name, normalise_jid, resolve_jid

GROUP_ID = "120363000000000000"


class TestResolveJid:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            # Bare international numbers — the common case.
            ("33766793050", "33766793050@s.whatsapp.net"),
            ("33784828374", "33784828374@s.whatsapp.net"),
            # …and the ways humans decorate them.
            ("+33766793050", "33766793050@s.whatsapp.net"),
            ("+33 7 66 79 30 50", "33766793050@s.whatsapp.net"),
            ("07-66-79-30-50", "0766793050@s.whatsapp.net"),
            ("(33) 766.79.30.50", "33766793050@s.whatsapp.net"),
            ("  33766793050  ", "33766793050@s.whatsapp.net"),
        ],
    )
    def test_phone_numbers(self, written: str, expected: str) -> None:
        assert resolve_jid(written) == expected

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            # A modern group id is longer than any phone number can be.
            (GROUP_ID, f"{GROUP_ID}@g.us"),
            # Historical form: <creator>-<timestamp>.
            ("33612345678-1612345678", "33612345678-1612345678@g.us"),
        ],
    )
    def test_group_ids(self, written: str, expected: str) -> None:
        assert resolve_jid(written) == expected

    @pytest.mark.parametrize(
        "written",
        [f"{GROUP_ID}@g.us", "33612345678@s.whatsapp.net"],
    )
    def test_explicit_jids_pass_through(self, written: str) -> None:
        assert resolve_jid(written) == written

    @pytest.mark.parametrize(
        "written",
        [
            "",
            "   ",
            "toto",
            "1234567",  # too short to be a phone number
            "33612345678@lid",  # domain we do not know
            "status@broadcast",
            "+33 abc 45",
        ],
    )
    def test_unreadable_destinations(self, written: str) -> None:
        assert resolve_jid(written) is None


class TestNormaliseJid:
    def test_drops_the_device_part(self) -> None:
        assert normalise_jid("33612345678:12@s.whatsapp.net") == "33612345678@s.whatsapp.net"

    def test_accepts_a_bare_number(self) -> None:
        assert normalise_jid("33612345678") == "33612345678@s.whatsapp.net"

    def test_leaves_a_plain_jid_alone(self) -> None:
        assert normalise_jid(f"{GROUP_ID}@g.us") == f"{GROUP_ID}@g.us"

    @pytest.mark.parametrize("written", ["", "   ", "@s.whatsapp.net", "33612345678@"])
    def test_rejects_nonsense(self, written: str) -> None:
        assert normalise_jid(written) is None


class TestDisplayName:
    def test_prefixes_a_phone_number(self) -> None:
        assert display_name("33612345678@s.whatsapp.net") == "+33612345678"

    def test_leaves_a_group_id_bare(self) -> None:
        assert display_name(f"{GROUP_ID}@g.us") == GROUP_ID

    def test_ignores_the_device_part(self) -> None:
        assert display_name("33612345678:12@s.whatsapp.net") == "+33612345678"
