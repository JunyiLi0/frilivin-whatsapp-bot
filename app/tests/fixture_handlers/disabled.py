from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler


class DisabledHandler(Handler):
    priority = 1

    def __init__(self) -> None:
        self.enabled = False

    def match(self, msg: InboundMessage) -> bool:
        return True

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        return None
