"""Underscore-prefixed: the registry must not even import this module."""

from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler


class IgnoredHandler(Handler):
    priority = 0

    def match(self, msg: InboundMessage) -> bool:
        return True

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        return None
