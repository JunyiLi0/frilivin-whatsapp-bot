from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler


class ZetaHandler(Handler):
    priority = 5
    stop_propagation = True

    def match(self, msg: InboundMessage) -> bool:
        return True

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        return None


class AbstractCommand(Handler):
    """Implements only half the contract, so it stays abstract."""

    priority = 30

    def match(self, msg: InboundMessage) -> bool:
        return True


class ConcreteCommand(AbstractCommand):
    """Indirect subclass: the registry has to walk the tree, not just one level."""

    priority = 40

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        return None
