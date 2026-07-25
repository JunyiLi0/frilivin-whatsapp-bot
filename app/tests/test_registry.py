"""Discovery rules: what gets picked up, in what order, and what is ignored."""

from __future__ import annotations

import pytest

from whatsapp_bot.processing.handlers.fallback import FallbackLogHandler
from whatsapp_bot.processing.handlers.ping import PingHandler
from whatsapp_bot.processing.registry import (
    DEFAULT_PACKAGE,
    discover_handlers,
    get_handlers,
    reset_registry,
)

FIXTURES = "fixture_handlers"


def names(handlers: list) -> list[str]:  # type: ignore[type-arg]
    return [handler.name for handler in handlers]


def test_discovers_handlers_from_the_fixture_package() -> None:
    found = names(discover_handlers(FIXTURES))
    assert "AlphaHandler" in found
    assert "ZetaHandler" in found


def test_sorts_by_priority() -> None:
    found = names(discover_handlers(FIXTURES))
    # ZetaHandler(5) < AlphaHandler(20) < ConcreteCommand(40)
    assert found.index("ZetaHandler") < found.index("AlphaHandler")
    assert found.index("AlphaHandler") < found.index("ConcreteCommand")


def test_walks_the_whole_subclass_tree() -> None:
    """ConcreteCommand extends an intermediate class, not Handler directly."""
    assert "ConcreteCommand" in names(discover_handlers(FIXTURES))


def test_skips_abstract_classes() -> None:
    assert "AbstractCommand" not in names(discover_handlers(FIXTURES))


def test_skips_disabled_handlers() -> None:
    assert "DisabledHandler" not in names(discover_handlers(FIXTURES))


def test_skips_underscore_modules() -> None:
    assert "IgnoredHandler" not in names(discover_handlers(FIXTURES))


def test_survives_a_broken_module() -> None:
    """One unimportable file must not take the other handlers down."""
    found = names(discover_handlers(FIXTURES))
    assert "AlphaHandler" in found
    assert "ZetaHandler" in found


def test_ignores_handlers_declared_outside_the_scanned_package() -> None:
    """The fixture handlers are imported by now, yet must not leak into prod."""
    discover_handlers(FIXTURES)
    production = names(discover_handlers(DEFAULT_PACKAGE))
    assert "AlphaHandler" not in production
    assert "ZetaHandler" not in production


def test_default_package_contains_the_shipped_handlers() -> None:
    found = names(discover_handlers(DEFAULT_PACKAGE))
    assert PingHandler.__name__ in found
    assert FallbackLogHandler.__name__ in found
    # The fallback observes everything, so it has to come last.
    assert found[-1] == FallbackLogHandler.__name__


def test_relay_handler_disables_itself_without_targets() -> None:
    """No RELAY_TARGET_JIDS configured: nothing to relay to."""
    assert "GroupRelayHandler" not in names(discover_handlers(DEFAULT_PACKAGE))


def test_relay_handler_appears_once_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from whatsapp_bot.config import get_settings

    monkeypatch.setenv("RELAY_KEYWORDS", "urgent")
    monkeypatch.setenv("RELAY_TARGET_JIDS", "120363000000000000@g.us")
    get_settings.cache_clear()

    assert "GroupRelayHandler" in names(discover_handlers(DEFAULT_PACKAGE))


def test_broadcast_handler_disables_itself_without_admins() -> None:
    """No BROADCAST_ADMIN_JIDS configured: nobody may broadcast."""
    assert "BroadcastHandler" not in names(discover_handlers(DEFAULT_PACKAGE))


def test_broadcast_handler_appears_once_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from whatsapp_bot.config import get_settings

    monkeypatch.setenv("BROADCAST_ADMIN_JIDS", "33612345678")
    get_settings.cache_clear()

    assert "BroadcastHandler" in names(discover_handlers(DEFAULT_PACKAGE))


def test_get_handlers_is_cached() -> None:
    first = get_handlers()
    assert get_handlers() is first
    reset_registry()
    assert get_handlers() is not first
