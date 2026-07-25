"""Automatic discovery of the handlers.

Every module under ``whatsapp_bot/processing/handlers/`` is imported, every
concrete :class:`~whatsapp_bot.processing.base.Handler` subclass declared there
is instantiated, and the result is sorted by ``priority``. Adding a behaviour is
therefore a matter of dropping in one file — nothing to register by hand.

Only classes *defined inside the scanned package* are collected. Walking
``Handler.__subclasses__()`` blindly would also pick up throwaway subclasses
declared in tests or in a REPL, which would make the pipeline depend on what
happens to have been imported.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from whatsapp_bot.logging import get_logger
from whatsapp_bot.processing.base import Handler

DEFAULT_PACKAGE = "whatsapp_bot.processing.handlers"

_cached: list[Handler] | None = None


def _iter_module_names(package: ModuleType) -> Iterator[str]:
    """Yield importable module names inside ``package``, skipping ``_private``."""
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        if info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        yield info.name


def _all_subclasses(root: type[Handler]) -> Iterator[type[Handler]]:
    for subclass in root.__subclasses__():
        yield subclass
        yield from _all_subclasses(subclass)


def discover_handlers(package_name: str = DEFAULT_PACKAGE) -> list[Handler]:
    """Import, instantiate and sort the handlers found in ``package_name``."""
    log = get_logger("whatsapp_bot.registry")
    package = importlib.import_module(package_name)

    for module_name in _iter_module_names(package):
        try:
            importlib.import_module(module_name)
        except Exception:
            # One broken file must not take the whole worker down: the other
            # behaviours keep running and the failure is loud in the logs.
            log.exception("handler_module_import_failed", module=module_name)

    prefix = f"{package_name}."
    handlers: list[Handler] = []
    seen: set[type[Handler]] = set()

    # type-abstract: walking an ABC's subclasses is exactly the point here.
    for cls in _all_subclasses(Handler):  # type: ignore[type-abstract]
        if cls in seen:
            continue
        seen.add(cls)

        if cls.__module__ != package_name and not cls.__module__.startswith(prefix):
            continue
        if inspect.isabstract(cls):
            continue

        try:
            handler = cls()
        except Exception:
            log.exception("handler_instantiation_failed", handler=cls.__name__)
            continue

        if not handler.enabled:
            log.info("handler_disabled", handler=handler.name)
            continue

        handlers.append(handler)

    handlers.sort(key=lambda handler: (handler.priority, handler.name))
    log.info(
        "handlers_loaded",
        count=len(handlers),
        handlers=[f"{handler.name}:{handler.priority}" for handler in handlers],
    )
    return handlers


def get_handlers() -> list[Handler]:
    """Return the process-wide handler chain, discovering it on first use."""
    global _cached
    if _cached is None:
        _cached = discover_handlers()
    return _cached


def reset_registry() -> None:
    """Forget the cached chain — used by tests and by config reloads."""
    global _cached
    _cached = None
