"""``!envoi`` — one recipient per line, each with its own message.

This is what the bot exists for: an operator writes a CSV-shaped message to the
dedicated number and it is fanned out.

    !envoi
    33766793050; Bonjour, la réunion est déplacée
    120363000000000000@g.us; Compte rendu envoyé
    nord; Message pour l'alias « nord »

Everything here is deliberately conservative. Only the JIDs listed in
``BROADCAST_ADMIN_JIDS`` may trigger it; an oversized batch is refused whole
rather than half-sent; and the operator always gets a receipt naming what left
and what did not.
"""

from __future__ import annotations

from dataclasses import dataclass

from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.logging import get_logger
from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler
from whatsapp_bot.processing.jids import display_name, normalise_jid, resolve_jid

SEPARATOR = ";"
COMMENT_PREFIX = "#"
ALIAS_STATE_PREFIX = "broadcast:alias:"
COUNTER_KEY = "broadcast:sent"


@dataclass(frozen=True)
class Entry:
    """One readable ``destination ; text`` line."""

    line_number: int
    destination: str
    text: str


@dataclass(frozen=True)
class LineError:
    """One line the operator needs to fix, named by its number."""

    line_number: int
    reason: str


def parse_entries(body: str, command: str) -> tuple[list[Entry], list[LineError]]:
    """Split a broadcast body into entries and rejected lines.

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

        destination, separator, text = line.partition(SEPARATOR)
        # partition, not split: only the first ";" separates, so the message
        # itself is free to contain semicolons.
        if not separator:
            errors.append(LineError(number, "séparateur « ; » manquant"))
            continue

        destination, text = destination.strip(), text.strip()
        if not destination:
            errors.append(LineError(number, "destinataire vide"))
        elif not text:
            errors.append(LineError(number, "message vide"))
        else:
            entries.append(Entry(number, destination, text))

    return entries, errors


class BroadcastHandler(Handler):
    # After PingHandler, before GroupRelayHandler. Swallows the message: a
    # command is never also data.
    priority = 20
    stop_propagation = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.command = self.settings.broadcast_command.strip().lower()

        admins = (normalise_jid(entry) for entry in self.settings.broadcast_admin_list)
        self.admins = {jid for jid in admins if jid}

        # No admins means no broadcast: the same guard GroupRelayHandler uses
        # when it has no target.
        self.enabled = bool(self.settings.broadcast_enabled and self.command and self.admins)
        self.max_recipients = self._effective_cap()

    def _effective_cap(self) -> int:
        """Cap the batch at what the per-minute quota can actually absorb.

        ``LiveSender`` spends a quota token when a message is *queued*, not when
        it leaves, and a refused send is dropped with only a warning. A cap
        above that budget would therefore deliver the head of a batch and lose
        the tail without telling anyone. The receipt costs one token too, hence
        the -1.
        """
        budget = max(1, self.settings.rate_limit_per_minute - 1)
        cap = min(self.settings.broadcast_max_recipients, budget)
        if cap < self.settings.broadcast_max_recipients:
            get_logger("whatsapp_bot.handler", handler=type(self).__name__).info(
                "broadcast_cap_clamped",
                requested=self.settings.broadcast_max_recipients,
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
            ctx.logger.warning("broadcast_refused", from_jid=msg.from_jid)
            return None

        entries, errors = parse_entries(msg.body, self.command)

        if not entries and not errors:
            return [self._reply(msg, self._usage())]

        if len(entries) > self.max_recipients:
            ctx.logger.warning(
                "broadcast_too_large", recipients=len(entries), cap=self.max_recipients
            )
            return [self._reply(msg, self._too_large(len(entries)))]

        sent: list[str] = []
        failures = list(errors)

        for entry in entries:
            jid = resolve_jid(self._alias(ctx, entry.destination) or entry.destination)
            if jid is None:
                failures.append(
                    LineError(
                        entry.line_number, f"destinataire « {entry.destination} » non reconnu"
                    )
                )
                continue
            # handler is set by hand: run_pipeline only stamps the outbounds a
            # handler *returns*, and these are sent directly so the receipt can
            # report an accurate count.
            ctx.send(Outbound(jid=jid, text=entry.text, handler=self.name))
            sent.append(jid)

        failures.sort(key=lambda failure: failure.line_number)

        if sent:
            previous = int(ctx.get_state(COUNTER_KEY) or "0")
            ctx.set_state(COUNTER_KEY, str(previous + len(sent)))

        ctx.logger.info("broadcast_done", sent=len(sent), failed=len(failures))
        return [self._reply(msg, self._receipt(sent, failures))]

    # -- alias resolution ------------------------------------------------------

    def _alias(self, ctx: Context, destination: str) -> str | None:
        """Look up ``destination`` as an alias — state table first, then .env.

        The state table wins so an alias can be added or corrected on a running
        bot without a redeploy.
        """
        key = destination.strip().lower()
        if not key:
            return None
        stored = ctx.get_state(f"{ALIAS_STATE_PREFIX}{key}")
        if stored:
            return stored
        return self.settings.broadcast_alias_map.get(key)

    # -- operator-facing text --------------------------------------------------

    def _reply(self, msg: InboundMessage, text: str) -> Outbound:
        return Outbound(jid=msg.chat_jid, text=text, quoted_id=msg.id, handler=self.name)

    def _usage(self) -> str:
        return (
            "📤 Diffusion — un destinataire par ligne :\n\n"
            f"{self.command}\n"
            "33766793050; Bonjour, la réunion est déplacée\n"
            "120363000000000000@g.us; Compte rendu envoyé\n\n"
            "• numéro international sans « + », JID de groupe, ou alias\n"
            f"• {self.max_recipients} destinataires maximum par envoi\n"
            "• « # » en début de ligne pour un commentaire"
        )

    def _too_large(self, count: int) -> str:
        return (
            f"⛔ {count} destinataires, maximum {self.max_recipients} par envoi.\n"
            "Rien n'a été envoyé — découpe ta liste en plusieurs messages."
        )

    def _receipt(self, sent: list[str], failures: list[LineError]) -> str:
        lines: list[str] = []

        if sent:
            lines.append(f"📤 {len(sent)} message(s) en file d'envoi :")
            lines.extend(f"  ✅ {display_name(jid)}" for jid in sent)
        else:
            lines.append("📭 Aucun message envoyé.")

        if failures:
            lines.append("")
            lines.append(f"⚠️ {len(failures)} ligne(s) ignorée(s) :")
            lines.extend(
                f"  • ligne {failure.line_number} : {failure.reason}" for failure in failures
            )

        if sent:
            lines.append("")
            lines.append("⏳ Chaque message part avec 2-8 s d'écart.")

        return "\n".join(lines)
