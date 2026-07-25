"""The handler contract.

A behaviour is a subclass of :class:`Handler` dropped into
``whatsapp_bot/processing/handlers/``. The registry finds it automatically, the
worker runs every matching handler in ``priority`` order, and stops early if one
of them sets ``stop_propagation``.

Handlers never talk to the bridge, to Redis or to the rate limiter directly:
everything goes through the :class:`Context` they are handed.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol

import structlog

from whatsapp_bot import db as db_module
from whatsapp_bot.config import Settings
from whatsapp_bot.logging import get_logger
from whatsapp_bot.models import InboundMessage, Outbound


class Sender(Protocol):
    """Delivers an outbound message and returns its id.

    The worker injects an implementation that enforces the rate limit, writes
    the message ledger and calls the bridge; tests inject one that simply
    records what was sent.
    """

    def __call__(self, out: Outbound, *, reply_to: str | None = None) -> str: ...


@dataclass
class Context:
    """Everything a handler is allowed to touch."""

    db: sqlite3.Connection
    config: Settings
    logger: structlog.stdlib.BoundLogger
    sender: Sender
    #: id of the inbound message being processed, used as the ``reply_to`` link
    inbound_id: str | None = None
    #: outbound messages emitted so far for this inbound message
    sent: list[str] = field(default_factory=list)

    @property
    def sent_count(self) -> int:
        """How many messages were already sent while processing this message.

        Lets a low-priority fallback stay silent when someone else answered.
        """
        return len(self.sent)

    def send(self, out: Outbound) -> str:
        """Queue an outbound message and return its id.

        Returns immediately: the bridge applies the human-like delay on its
        side, so a handler can send without eating into the job budget.
        """
        outbound_id = self.sender(out, reply_to=self.inbound_id)
        self.sent.append(outbound_id)
        return outbound_id

    # -- key/value state, handy for counters, cursors, per-chat memory --------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        return db_module.get_state(self.db, key, default)

    def set_state(self, key: str, value: str) -> None:
        db_module.set_state(self.db, key, value)


class Handler(ABC):
    """Base class for every behaviour.

    Subclasses must be constructible without arguments — the registry
    instantiates them with ``cls()``.
    """

    #: lower runs first; ties are broken by class name so the order is stable
    priority: int = 100
    #: when True, no handler after this one runs for the current message
    stop_propagation: bool = False
    #: set to False (typically in ``__init__``) to opt out without deleting the file
    enabled: bool = True

    @property
    def name(self) -> str:
        return type(self).__name__

    @abstractmethod
    def match(self, msg: InboundMessage) -> bool:
        """Return True if this handler wants to process ``msg``."""

    @abstractmethod
    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        """Process ``msg``.

        Return the messages to send — the runner passes each one to
        ``ctx.send`` — or ``None`` to send nothing. Handlers may also call
        ``ctx.send`` themselves when they need the outbound id.
        """

    def __repr__(self) -> str:
        return f"<{self.name} priority={self.priority} stop={self.stop_propagation}>"


def build_logger(handler_name: str, message_id: str) -> structlog.stdlib.BoundLogger:
    return get_logger("whatsapp_bot.handler", handler=handler_name, message_id=message_id)
