"""Fake messages and a fake sender — the raw material of the pipeline tests."""

from __future__ import annotations

from whatsapp_bot.models import InboundMessage, Outbound

DEFAULT_SENDER_JID = "33612345678@s.whatsapp.net"
DEFAULT_GROUP_JID = "120363000000000000@g.us"


def make_message(
    text: str = "hello",
    *,
    message_id: str = "msg-1",
    is_group: bool = False,
    from_jid: str = DEFAULT_SENDER_JID,
    chat_jid: str | None = None,
    msg_type: str = "text",
    quoted_id: str | None = None,
    timestamp: int = 1700000000,
) -> InboundMessage:
    """Build a plausible inbound message."""
    return InboundMessage(
        id=message_id,
        from_jid=from_jid,
        chat_jid=chat_jid or (DEFAULT_GROUP_JID if is_group else from_jid),
        is_group=is_group,
        timestamp=timestamp,
        type=msg_type,
        text=text,
        quoted_id=quoted_id,
    )


class FakeSender:
    """Records what handlers send. Can be told to blow up instead."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.messages: list[Outbound] = []
        self.reply_to: list[str | None] = []
        self.fail_with = fail_with

    def __call__(self, out: Outbound, *, reply_to: str | None = None) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.messages.append(out)
        self.reply_to.append(reply_to)
        return f"out-{len(self.messages)}"

    @property
    def texts(self) -> list[str]:
        return [message.text for message in self.messages]

    @property
    def jids(self) -> list[str]:
        return [message.jid for message in self.messages]

    @property
    def handlers(self) -> list[str | None]:
        return [message.handler for message in self.messages]
