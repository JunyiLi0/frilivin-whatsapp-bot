"""Turning what an operator typed into a WhatsApp JID.

Lives outside ``processing/handlers/`` on purpose: the registry only scans that
package, so nothing here is ever mistaken for a behaviour.

A destination can be written as a bare phone number, a formatted phone number, a
group id or a full JID, and every handler that lets a human name a recipient
needs the same rules — hence one module rather than a copy per handler.
"""

from __future__ import annotations

import re

GROUP_SUFFIX = "@g.us"
USER_SUFFIX = "@s.whatsapp.net"
# WhatsApp's privacy-preserving address family. It identifies a real user, but
# carries no phone number, so it can never be derived — only passed through.
LID_SUFFIX = "@lid"

KNOWN_SUFFIXES = (GROUP_SUFFIX, USER_SUFFIX, LID_SUFFIX)

# E.164 caps a phone number at 15 digits; group ids are ~18. That gap is what
# lets a bare number be told apart from a group id.
MIN_PHONE_DIGITS = 8
MAX_PHONE_DIGITS = 15

# Punctuation people put in phone numbers: +33 (0)7-66.79.30.50
_PHONE_NOISE = re.compile(r"[\s+.()\-]")
# Historical group ids: <creator>-<timestamp>
_LEGACY_GROUP = re.compile(r"^\d{5,}-\d{5,}$")


def resolve_jid(destination: str) -> str | None:
    """Return the JID ``destination`` refers to, or ``None`` if unreadable.

    Never guesses beyond the rules below: a destination the bot cannot read is
    reported back to the operator instead of being silently sent somewhere.
    """
    token = destination.strip()
    if not token:
        return None

    if token.endswith(KNOWN_SUFFIXES):
        return token
    if "@" in token:
        # Some other domain (@broadcast, @newsletter, a typo…). Not our call to fix.
        return None

    # Checked before hyphens are stripped, otherwise 07-66-79-30-50 and
    # 33612345678-1612345678 would end up looking the same.
    if _LEGACY_GROUP.match(token):
        return f"{token}{GROUP_SUFFIX}"

    digits = _PHONE_NOISE.sub("", token)
    if not digits.isdigit():
        return None
    if len(digits) > MAX_PHONE_DIGITS:
        return f"{digits}{GROUP_SUFFIX}"
    if len(digits) < MIN_PHONE_DIGITS:
        return None
    return f"{digits}{USER_SUFFIX}"


def normalise_jid(raw: str) -> str | None:
    """Canonical form of a JID, dropping the device part.

    WhatsApp addresses a specific linked device as ``33612345678:12@s.whatsapp.net``.
    Comparing that to an allowlist entry written by hand would never match, so
    the device suffix goes.
    """
    token = raw.strip()
    if not token:
        return None
    if "@" in token:
        local, _, domain = token.partition("@")
        local = local.split(":", 1)[0]
        if not local or not domain:
            return None
        return f"{local}@{domain}"
    return resolve_jid(token)


def display_name(jid: str) -> str:
    """``33612345678@s.whatsapp.net`` → ``+33612345678``.

    Group ids and LIDs are numeric too, so they are left alone rather than
    being dressed up as phone numbers they are not.
    """
    local = jid.split("@", 1)[0].split(":", 1)[0]
    if jid.endswith((GROUP_SUFFIX, LID_SUFFIX)) or not local.isdigit():
        return local
    return f"+{local}"
