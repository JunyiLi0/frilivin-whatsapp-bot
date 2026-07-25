"""Relay private messages carrying a keyword to one or more WhatsApp groups.

This is the behaviour the bot was built for: a dedicated number receives
everything, and what matters is forwarded to the right groups. Configure it with
``RELAY_KEYWORDS`` and ``RELAY_TARGET_JIDS`` in ``.env``; with no target the
handler disables itself at startup.
"""

from __future__ import annotations

from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler

RELAY_COUNTER_KEY = "relay:count"


def display_name(jid: str) -> str:
    """``33612345678@s.whatsapp.net`` → ``+33612345678``.

    Group ids are numeric too, so they are left alone rather than being
    dressed up as phone numbers.
    """
    local = jid.split("@", 1)[0].split(":", 1)[0]
    if jid.endswith("@g.us") or not local.isdigit():
        return local
    return f"+{local}"


class GroupRelayHandler(Handler):
    priority = 50
    # Deliberately does not stop propagation: a relayed message should still be
    # seen by whatever behaviour is added later.
    stop_propagation = False

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.keywords = self.settings.relay_keyword_list
        self.targets = self.settings.relay_target_list
        self.enabled = bool(self.keywords and self.targets)

    def match(self, msg: InboundMessage) -> bool:
        # Private messages only: relaying group traffic to other groups is how
        # you build an accidental loop.
        if not msg.is_private or msg.type != "text":
            return False
        body = msg.body.lower()
        return bool(body) and any(keyword in body for keyword in self.keywords)

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        sender = display_name(msg.from_jid)
        text = f"🔁 Relais de {sender}\n\n{msg.body}"

        ctx.logger.info("relaying", sender=sender, targets=len(self.targets))

        previous = int(ctx.get_state(RELAY_COUNTER_KEY) or "0")
        ctx.set_state(RELAY_COUNTER_KEY, str(previous + 1))

        return [Outbound(jid=target, text=text) for target in self.targets]
