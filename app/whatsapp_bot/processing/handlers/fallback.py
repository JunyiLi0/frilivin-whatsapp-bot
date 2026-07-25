"""Last handler in the chain: observes, never answers.

Sits at the very end so that ``ctx.sent_count`` tells it whether anybody
replied. Messages nothing came back for are logged at ``info`` — that log line
is the shortlist of behaviours still missing.
"""

from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler

PREVIEW_LENGTH = 120


class FallbackLogHandler(Handler):
    priority = 1000
    stop_propagation = False

    def match(self, msg: InboundMessage) -> bool:
        return True

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        preview = msg.body[:PREVIEW_LENGTH]
        if ctx.sent_count:
            ctx.logger.debug("message_handled", replies=ctx.sent_count, preview=preview)
        else:
            ctx.logger.info(
                "message_unhandled",
                chat_jid=msg.chat_jid,
                from_jid=msg.from_jid,
                is_group=msg.is_group,
                type=msg.type,
                preview=preview,
            )
        return None
