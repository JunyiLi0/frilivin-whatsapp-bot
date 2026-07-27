"""``!envoigroupe`` — one group per line, designated by name and/or number.

Groups are what the operator actually writes to, but a group is identified by
an 18-digit JID nobody retypes. So a line names it the way a human would: part
of the group's name, part of the phone number of any of its members, then the
message.

    !envoigroupe
    Chantier Nord; 33766660673; Livraison décalée à jeudi
    ; 0612345678; Merci de confirmer
    Dupont; ; Le devis est parti

Either of the first two fields may be left empty; what remains must designate
**exactly one** group. Nothing is sent on a doubt: a line matching several
groups, or none, is reported back with its reason instead of being guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

from whatsapp_bot.bridge_client import BridgeError
from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.groups import (
    MIN_NUMBER_DIGITS,
    Group,
    GroupDirectory,
    Lookup,
    digits_only,
    find_group,
)
from whatsapp_bot.logging import get_logger
from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler
from whatsapp_bot.processing.jids import normalise_jid

SEPARATOR = ";"
COMMENT_PREFIX = "#"
COUNTER_KEY = "group_broadcast:sent"
#: Groups listed in an error or a usage reply before the list is cut short.
MAX_LISTED = 6


@dataclass(frozen=True)
class Entry:
    """One readable ``name ; number ; message`` line."""

    line_number: int
    name: str
    number: str
    text: str

    @property
    def criteria(self) -> str:
        """The search terms, as the operator wrote them."""
        parts = []
        if self.name:
            parts.append(f"nom « {self.name} »")
        if self.number:
            parts.append(f"numéro « {self.number} »")
        return " + ".join(parts)


@dataclass(frozen=True)
class LineError:
    """One line the operator needs to fix, named by its number."""

    line_number: int
    reason: str


def parse_entries(body: str, command: str) -> tuple[list[Entry], list[LineError]]:
    """Split an ``!envoigroupe`` body into entries and rejected lines.

    Pure function: no context, no I/O, so the format is testable on its own.
    Blank lines and ``#`` comments are skipped silently; everything else either
    becomes an :class:`Entry` or an explained :class:`LineError`.
    """
    entries: list[Entry] = []
    errors: list[LineError] = []

    for number, raw in enumerate(body.splitlines(), start=1):
        line = raw.strip()

        if number == 1 and line.lower().startswith(command):
            # The list may start on the command line itself or on the next one.
            line = line[len(command) :].strip()

        if not line or line.startswith(COMMENT_PREFIX):
            continue

        # Only the first two ";" separate, so the message keeps its own.
        parts = line.split(SEPARATOR, 2)
        if len(parts) < 3:
            errors.append(LineError(number, "format attendu : nom ; numéro ; message"))
            continue

        name, phone, text = (part.strip() for part in parts)
        if not name and not phone:
            errors.append(LineError(number, "nom et numéro vides — il en faut au moins un"))
        elif not text:
            errors.append(LineError(number, "message vide"))
        else:
            entries.append(Entry(number, name, phone, text))

    return entries, errors


class GroupBroadcastHandler(Handler):
    # Right after BroadcastHandler: both are operator commands, and the leading
    # token tells them apart. Swallows the message — a command is never data.
    priority = 25
    stop_propagation = True

    def __init__(
        self, settings: Settings | None = None, directory: GroupDirectory | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.command = self.settings.group_broadcast_command.strip().lower()
        self.country_code = digits_only(self.settings.phone_country_code)

        admins = (normalise_jid(entry) for entry in self.settings.group_broadcast_admin_list)
        self.admins = {jid for jid in admins if jid}

        # Same guard as BroadcastHandler: no admin, no command. Writing to a
        # group is exactly as irreversible as writing to a person.
        self.enabled = bool(self.settings.group_broadcast_enabled and self.command and self.admins)
        self.max_recipients = self._effective_cap()
        self.directory = directory or GroupDirectory(
            ttl_seconds=self.settings.group_directory_ttl_seconds
        )

    def _effective_cap(self) -> int:
        """Cap the batch at what the per-minute quota can actually absorb.

        Identical reasoning to ``BroadcastHandler``: a token is spent when a
        message is *queued*, and a refused send is dropped with a warning, so a
        cap above the budget would deliver the head of a batch and lose the
        tail. The receipt costs one token too, hence the -1.
        """
        budget = max(1, self.settings.rate_limit_per_minute - 1)
        cap = min(self.settings.group_broadcast_max_recipients, budget)
        if cap < self.settings.group_broadcast_max_recipients:
            get_logger("whatsapp_bot.handler", handler=type(self).__name__).info(
                "group_broadcast_cap_clamped",
                requested=self.settings.group_broadcast_max_recipients,
                applied=cap,
                rate_limit_per_minute=self.settings.rate_limit_per_minute,
            )
        return cap

    def match(self, msg: InboundMessage) -> bool:
        return msg.is_private and msg.type == "text" and msg.command() == self.command

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        if normalise_jid(msg.from_jid) not in self.admins:
            # Silent on purpose: an unknown sender learns nothing about what
            # this number can do, and the refusal costs no quota.
            ctx.logger.warning("group_broadcast_refused", from_jid=msg.from_jid)
            return None

        entries, errors = parse_entries(msg.body, self.command)

        if not entries and not errors:
            return [self._reply(msg, self._usage(ctx))]

        if len(entries) > self.max_recipients:
            ctx.logger.warning(
                "group_broadcast_too_large", recipients=len(entries), cap=self.max_recipients
            )
            return [self._reply(msg, self._too_large(len(entries)))]

        try:
            snapshot = self.directory.snapshot()
        except BridgeError as exc:
            ctx.logger.error("group_directory_unavailable", error=str(exc))
            return [self._reply(msg, self._directory_down())]

        resolved = self._resolve(entries, snapshot.groups)

        # A group created, renamed or joined since the last fetch would look
        # like a typo. Never announce a miss on a cached list without checking.
        if snapshot.cached and any(lookup.group is None for _, lookup in resolved):
            try:
                snapshot = self.directory.snapshot(force=True)
            except BridgeError as exc:
                # The cached answer is still worth reporting, misses included.
                ctx.logger.warning("group_directory_refresh_failed", error=str(exc))
            else:
                resolved = self._resolve(entries, snapshot.groups)

        sent: list[Group] = []
        failures = list(errors)

        for entry, lookup in resolved:
            if lookup.group is None:
                failures.append(LineError(entry.line_number, self._why(entry, lookup)))
                continue
            # handler is set by hand: run_pipeline only stamps the outbounds a
            # handler *returns*, and these are sent directly so the receipt can
            # report an accurate count.
            ctx.send(Outbound(jid=lookup.group.jid, text=entry.text, handler=self.name))
            sent.append(lookup.group)

        failures.sort(key=lambda failure: failure.line_number)

        if sent:
            previous = int(ctx.get_state(COUNTER_KEY) or "0")
            ctx.set_state(COUNTER_KEY, str(previous + len(sent)))

        ctx.logger.info(
            "group_broadcast_done",
            sent=len(sent),
            failed=len(failures),
            known_groups=len(snapshot.groups),
        )
        return [self._reply(msg, self._receipt(sent, failures))]

    # -- resolution ------------------------------------------------------------

    def _resolve(
        self, entries: list[Entry], groups: tuple[Group, ...]
    ) -> list[tuple[Entry, Lookup]]:
        return [(entry, self._lookup(entry, groups)) for entry in entries]

    def _lookup(self, entry: Entry, groups: tuple[Group, ...]) -> Lookup:
        if self._unusable_number(entry) is not None:
            return Lookup(None)
        return find_group(
            groups, name=entry.name, number=entry.number, country_code=self.country_code
        )

    def _unusable_number(self, entry: Entry) -> str | None:
        """Why this number cannot be searched with, or None when it can.

        A fragment shorter than :data:`MIN_NUMBER_DIGITS` is refused rather than
        matched against every group at once — the operator would get an
        ambiguity report listing the whole address book.
        """
        if not entry.number:
            return None
        fragment = digits_only(entry.number)
        if not fragment:
            return f"numéro « {entry.number} » illisible"
        if len(fragment) < MIN_NUMBER_DIGITS:
            return (
                f"numéro « {entry.number} » trop court "
                f"({MIN_NUMBER_DIGITS} chiffres minimum pour chercher)"
            )
        return None

    def _why(self, entry: Entry, lookup: Lookup) -> str:
        """The reason a line was not sent, in the operator's own terms."""
        unusable = self._unusable_number(entry)
        if unusable is not None:
            return unusable
        if lookup.ambiguous:
            names = ", ".join(group.label for group in lookup.candidates[:MAX_LISTED])
            if len(lookup.candidates) > MAX_LISTED:
                names += ", …"
            return (
                f"{len(lookup.candidates)} groupes correspondent à {entry.criteria} "
                f"({names}) — précisez"
            )
        return f"aucun groupe ne correspond à {entry.criteria}"

    # -- operator-facing text --------------------------------------------------

    def _reply(self, msg: InboundMessage, text: str) -> Outbound:
        return Outbound(jid=msg.chat_jid, text=text, quoted_id=msg.id, handler=self.name)

    def _usage(self, ctx: Context) -> str:
        lines = [
            "📤 Envoi à des groupes — une ligne par groupe :",
            "",
            self.command,
            "Chantier Nord; 33766660673; Livraison décalée à jeudi",
            "; 0612345678; Merci de confirmer",
            "Dupont; ; Le devis est parti",
            "",
            "• 1er champ : nom du groupe, 2e : numéro d'un de ses membres",
            "• les deux acceptent un fragment (« No » trouve « Nord »)",
            "• l'un des deux peut rester vide, jamais les deux",
            "• un seul groupe doit correspondre, sinon la ligne est rejetée",
            f"• {self.max_recipients} groupes maximum par envoi",
        ]

        known = self._known_groups(ctx)
        if known:
            lines.append("")
            lines.append(f"Groupes connus ({len(known)}) :")
            lines.extend(f"  • {group.label}" for group in known[:MAX_LISTED])
            if len(known) > MAX_LISTED:
                lines.append(f"  • … et {len(known) - MAX_LISTED} autre(s)")
        return "\n".join(lines)

    def _known_groups(self, ctx: Context) -> list[Group]:
        """The group list for the usage memo — absent rather than fatal."""
        try:
            return list(self.directory.snapshot().groups)
        except BridgeError as exc:
            ctx.logger.warning("group_directory_unavailable", error=str(exc))
            return []

    def _too_large(self, count: int) -> str:
        return (
            f"⛔ {count} groupes, maximum {self.max_recipients} par envoi.\n"
            "Rien n'a été envoyé — découpe ta liste en plusieurs messages."
        )

    def _directory_down(self) -> str:
        return (
            "⛔ Impossible de récupérer la liste des groupes auprès de WhatsApp.\n"
            "Rien n'a été envoyé — réessaie dans un instant."
        )

    def _receipt(self, sent: list[Group], failures: list[LineError]) -> str:
        lines: list[str] = []

        if sent:
            lines.append(f"📤 {len(sent)} message(s) en file d'envoi :")
            lines.extend(f"  ✅ {group.label}" for group in sent)
        else:
            lines.append("📭 Aucun message envoyé.")

        if failures:
            lines.append("")
            lines.append(f"⚠️ {len(failures)} ligne(s) rejetée(s) :")
            lines.extend(
                f"  • ligne {failure.line_number} : {failure.reason}" for failure in failures
            )

        if sent:
            lines.append("")
            lines.append("⏳ Chaque message part avec 2-8 s d'écart.")

        return "\n".join(lines)
