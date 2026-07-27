"""The groups the bot belongs to: how they are listed, and how they are named.

``!envoigroupe`` lets an operator designate a group by fragments — a piece of
its name, a piece of one member's phone number — rather than by an 18-digit
JID nobody can remember. Two things are needed for that, and both live here:

* a cached view of the group list, so a batch of lines costs one call to the
  bridge instead of one per line;
* matching rules that either name exactly one group or explain why they could
  not, because sending a message to the wrong group cannot be undone.

Deliberately outside ``processing/handlers/``: the registry only scans that
package, so nothing here is ever mistaken for a behaviour.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from whatsapp_bot.bridge_client import BridgeClient, get_bridge_client

#: Below this, a fragment matches so many numbers that it says nothing.
MIN_NUMBER_DIGITS = 3

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_NON_DIGIT = re.compile(r"\D+")


class Tier(IntEnum):
    """How good a match is — lower is better, and the best tier wins outright.

    Ranking rather than scoring: an operator who types the exact name of a
    group means *that* group, even when the fragment also happens to appear
    inside another one.
    """

    EXACT = 0
    PREFIX = 1
    CONTAINS = 2


@dataclass(frozen=True)
class Group:
    """One WhatsApp group, as the bridge sees it."""

    jid: str
    subject: str = ""
    #: Phone numbers of the members, digits only.
    numbers: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """What to call this group when talking to a human."""
        return self.subject or self.jid


def parse_group(row: dict[str, Any]) -> Group:
    """Build a :class:`Group` from one entry of the bridge's ``/groups``."""
    numbers = row.get("numbers") or []
    return Group(
        jid=str(row.get("jid") or ""),
        subject=str(row.get("subject") or ""),
        numbers=tuple(digits_only(str(number)) for number in numbers if str(number).strip()),
    )


# -- normalisation -------------------------------------------------------------


def fold(text: str) -> str:
    """Comparable form of a group name: no accents, no case, no punctuation.

    « Frilivin — Île-de-France » and "frilivin ile de france" have to match:
    what an operator retypes on a phone keyboard is never the exact subject.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _NON_ALNUM.sub(" ", ascii_only.lower()).strip()


def digits_only(text: str) -> str:
    """``+33 7 66 66 06 73`` → ``33766660673``."""
    return _NON_DIGIT.sub("", text)


def number_variants(fragment: str, country_code: str) -> tuple[str, ...]:
    """The fragment as typed, plus its international form when written ``0…``.

    Numbers are stored the way WhatsApp holds them (``33766660673``) but are
    read out loud, and written down, in national form (``07 66 66 06 73``).
    Without this, the national form would match nothing at all.
    """
    variants = [fragment]
    if country_code and len(fragment) > 1 and fragment.startswith("0"):
        variants.append(f"{country_code}{fragment[1:]}")
    return tuple(dict.fromkeys(variants))


# -- matching ------------------------------------------------------------------


def _name_tier(needle: str, subject: str) -> Tier | None:
    """How well ``needle`` designates a group called ``subject``."""
    if not needle or not subject:
        return None
    if needle == subject:
        return Tier.EXACT
    # A word prefix counts as a prefix: "dupont" names "Chantier Dupont".
    if subject.startswith(needle) or any(word.startswith(needle) for word in subject.split()):
        return Tier.PREFIX
    if needle in subject:
        return Tier.CONTAINS
    return None


def _number_tier(fragments: Sequence[str], numbers: Iterable[str]) -> Tier | None:
    """The best tier reached by any fragment against any member number."""
    best: Tier | None = None
    for number in numbers:
        for fragment in fragments:
            if number == fragment:
                tier = Tier.EXACT
            elif number.startswith(fragment):
                tier = Tier.PREFIX
            elif fragment in number:
                tier = Tier.CONTAINS
            else:
                continue
            best = tier if best is None else min(best, tier)
            if best is Tier.EXACT:
                return best
    return best


@dataclass(frozen=True)
class Lookup:
    """The outcome of designating a group by name and/or number."""

    #: The single group designated, or None when there is nothing to send to.
    group: Group | None
    #: The equally good candidates — one when resolved, several when ambiguous.
    candidates: tuple[Group, ...] = ()

    @property
    def ambiguous(self) -> bool:
        return self.group is None and len(self.candidates) > 1


def find_group(
    groups: Iterable[Group],
    *,
    name: str = "",
    number: str = "",
    country_code: str = "",
) -> Lookup:
    """Return the one group matching ``name`` and ``number``.

    Both criteria are optional but at least one is required; a group must
    satisfy *every* criterion given. Candidates are then ranked by their
    weakest criterion, and only the best rank survives — so an exact name beats
    a group that merely contains the same letters. A tie is left unresolved on
    purpose: the caller reports it rather than picking one.
    """
    needle = fold(name)
    fragment = digits_only(number)
    fragments = number_variants(fragment, country_code) if fragment else ()

    if not needle and not fragments:
        return Lookup(None)

    scored: list[tuple[Tier, Group]] = []
    for group in groups:
        tiers: list[Tier] = []

        if needle:
            tier = _name_tier(needle, fold(group.subject))
            if tier is None:
                continue
            tiers.append(tier)

        if fragments:
            tier = _number_tier(fragments, group.numbers)
            if tier is None:
                continue
            tiers.append(tier)

        # The weakest criterion rates the group: matching one half perfectly is
        # not enough to outrank a group that matches both halves well.
        scored.append((max(tiers), group))

    if not scored:
        return Lookup(None)

    best = min(tier for tier, _ in scored)
    candidates = tuple(group for tier, group in scored if tier == best)
    return Lookup(candidates[0] if len(candidates) == 1 else None, candidates)


# -- the cached directory ------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """A group list, and whether it was served from the cache."""

    groups: tuple[Group, ...]
    cached: bool


class GroupDirectory:
    """The group list, fetched from the bridge and kept for a short while.

    Listing groups goes all the way to WhatsApp, so a batch resolves every one
    of its lines against a single snapshot. The TTL is what bounds how long a
    renamed group, or a group the bot has just joined, stays invisible — and a
    caller that finds nothing can always ask for a fresh one.
    """

    def __init__(
        self,
        client: BridgeClient | None = None,
        *,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self._clock = clock
        self._groups: tuple[Group, ...] | None = None
        self._fetched_at = 0.0

    def snapshot(self, *, force: bool = False) -> Snapshot:
        """Return the known groups, refreshing them when the TTL has expired.

        Raises :class:`~whatsapp_bot.bridge_client.BridgeError` when the bridge
        cannot answer and nothing usable is cached.
        """
        if not force and self._groups is not None and self._clock() - self._fetched_at < self._ttl:
            return Snapshot(self._groups, cached=True)

        groups = tuple(parse_group(row) for row in self._bridge().groups())
        self._groups = groups
        self._fetched_at = self._clock()
        return Snapshot(groups, cached=False)

    def invalidate(self) -> None:
        self._groups = None
        self._fetched_at = 0.0

    def _bridge(self) -> BridgeClient:
        # Built on first use, never at import time: instantiating a handler must
        # not depend on the bridge being reachable.
        if self._client is None:
            self._client = get_bridge_client()
        return self._client
