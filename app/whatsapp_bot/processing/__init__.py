"""Message processing: the handler contract, the registry and the handlers."""

from whatsapp_bot.processing.base import Context, Handler, Sender
from whatsapp_bot.processing.registry import discover_handlers, get_handlers, reset_registry

__all__ = [
    "Context",
    "Handler",
    "Sender",
    "discover_handlers",
    "get_handlers",
    "reset_registry",
]
