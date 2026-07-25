"""The chain itself: order, short-circuiting, error propagation, bookkeeping."""

from __future__ import annotations

import pytest
from factories import FakeSender, make_message

from whatsapp_bot.models import InboundMessage, Outbound
from whatsapp_bot.processing.base import Context, Handler
from whatsapp_bot.worker.tasks import HandlerFailure, run_pipeline


class Recorder(Handler):
    """Test handler that logs its own execution into a shared list."""

    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        priority: int = 100,
        stop: bool = False,
        matches: bool = True,
        replies: list[str] | None = None,
        explode: bool = False,
    ) -> None:
        self._name = name
        self._calls = calls
        self.priority = priority
        self.stop_propagation = stop
        self._matches = matches
        self._replies = replies or []
        self._explode = explode

    @property
    def name(self) -> str:
        return self._name

    def match(self, msg: InboundMessage) -> bool:
        return self._matches

    def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound] | None:
        self._calls.append(self._name)
        if self._explode:
            raise ValueError("handler exploded")
        return [Outbound(jid=msg.chat_jid, text=reply) for reply in self._replies]


def test_runs_every_matching_handler_in_order(context: Context) -> None:
    calls: list[str] = []
    handlers = [
        Recorder("first", calls, priority=10),
        Recorder("second", calls, priority=20),
        Recorder("third", calls, priority=30),
    ]

    result = run_pipeline(make_message(), context, handlers)

    assert calls == ["first", "second", "third"]
    assert result.executed == ["first", "second", "third"]
    assert result.stopped_by is None


def test_skips_handlers_that_do_not_match(context: Context) -> None:
    calls: list[str] = []
    handlers = [
        Recorder("yes", calls),
        Recorder("no", calls, matches=False),
    ]

    result = run_pipeline(make_message(), context, handlers)

    assert calls == ["yes"]
    assert result.executed == ["yes"]


def test_stop_propagation_cuts_the_chain(context: Context) -> None:
    calls: list[str] = []
    handlers = [
        Recorder("before", calls, priority=10),
        Recorder("stopper", calls, priority=20, stop=True),
        Recorder("after", calls, priority=30),
    ]

    result = run_pipeline(make_message(), context, handlers)

    assert calls == ["before", "stopper"]
    assert result.stopped_by == "stopper"
    assert "after" not in result.executed


def test_outbound_messages_are_sent_and_attributed(context: Context, sender: FakeSender) -> None:
    handlers = [Recorder("talker", [], replies=["un", "deux"])]

    result = run_pipeline(make_message(), context, handlers)

    assert sender.texts == ["un", "deux"]
    assert sender.handlers == ["talker", "talker"]
    assert sender.reply_to == ["msg-1", "msg-1"]
    assert result.sent == 2
    assert context.sent_count == 2


def test_an_explicit_handler_name_is_preserved(context: Context, sender: FakeSender) -> None:
    class Delegating(Recorder):
        def run(self, msg: InboundMessage, ctx: Context) -> list[Outbound]:
            return [Outbound(jid=msg.chat_jid, text="hi", handler="SomethingElse")]

    run_pipeline(make_message(), context, [Delegating("wrapper", [])])

    assert sender.handlers == ["SomethingElse"]


def test_a_failing_handler_is_named_in_the_error(context: Context) -> None:
    handlers = [Recorder("boom", [], explode=True)]

    with pytest.raises(HandlerFailure) as excinfo:
        run_pipeline(make_message(), context, handlers)

    assert excinfo.value.handler == "boom"
    assert isinstance(excinfo.value.original, ValueError)


def test_a_failing_match_is_also_attributed(context: Context) -> None:
    class BadMatch(Recorder):
        def match(self, msg: InboundMessage) -> bool:
            raise KeyError("bad match")

    with pytest.raises(HandlerFailure) as excinfo:
        run_pipeline(make_message(), context, [BadMatch("picky", [])])

    assert excinfo.value.handler == "picky"


def test_handlers_returning_none_send_nothing(context: Context, sender: FakeSender) -> None:
    class Silent(Recorder):
        def run(self, msg: InboundMessage, ctx: Context) -> None:
            return None

    result = run_pipeline(make_message(), context, [Silent("quiet", [])])

    assert sender.messages == []
    assert result.sent == 0
    assert result.executed == ["quiet"]


def test_an_empty_chain_is_not_an_error(context: Context) -> None:
    result = run_pipeline(make_message(), context, [])

    assert result.executed == []
    assert result.stopped_by is None
    assert result.sent == 0


def test_the_logger_is_restored_after_the_run(context: Context) -> None:
    before = context.logger
    run_pipeline(make_message(), context, [Recorder("x", [])])
    assert context.logger is before


def test_context_state_round_trips(context: Context) -> None:
    assert context.get_state("missing") is None
    assert context.get_state("missing", "fallback") == "fallback"

    context.set_state("cursor", "42")
    assert context.get_state("cursor") == "42"

    context.set_state("cursor", "43")
    assert context.get_state("cursor") == "43"
