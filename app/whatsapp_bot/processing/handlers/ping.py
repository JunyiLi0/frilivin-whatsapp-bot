"""``!ping`` → ``pong``. The smallest possible handler, useful as a template."""

from __future__ import annotations

from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler


class PingHandler(Handler):
    # Runs early and swallows the message: a command is never also data.
    priority = 10
    stop_propagation = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.enabled = self.settings.ping_enabled

    def match(self, msg: InboundMessage) -> bool:
        return msg.command() == "!ping"

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        ctx.logger.info("ping_received", chat_jid=msg.chat_jid)
        return [Outbound(jid=msg.chat_jid, text="pong 🏓", quoted_id=msg.id)]
