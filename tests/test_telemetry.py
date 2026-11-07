"""Telemetry: one agent's message stream turned into events.

Built from fake SDK message objects rather than a live run. The point is the
bookkeeping - who said it, what it cost, which line gets handed on - and that
is a pure function of the message sequence.
"""

from __future__ import annotations

from dataclasses import dataclass

from sliceops.events import EventBus
from sliceops.telemetry import StepTelemetry


@dataclass
class FakeText:
    text: str


@dataclass
class FakeThinking:
    thinking: str


@dataclass
class FakeToolUse:
    name: str


class FakeAssistant:
    def __init__(self, *content):
        self.content = list(content)


class FakeResult:
    def __init__(self, in_tokens=0, out_tokens=0, cost=0.0, num_turns=0):
        self.usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}
        self.total_cost_usd = cost
        self.num_turns = num_turns


def _patch(monkeypatch):
    """Make the isinstance checks in telemetry see these fakes."""
    import sliceops.telemetry as module
    monkeypatch.setattr(module, "AssistantMessage", FakeAssistant)
    monkeypatch.setattr(module, "ResultMessage", FakeResult)
    monkeypatch.setattr(module, "TextBlock", FakeText)
    monkeypatch.setattr(module, "ThinkingBlock", FakeThinking)
    monkeypatch.setattr(module, "ToolUseBlock", FakeToolUse)


def test_text_and_thinking_are_attributed_to_the_step_that_is_running(monkeypatch):
    """Attribution is a constructor argument now, not an inference from
    `parent_tool_use_id`. There is no case left where it can be wrong."""
    _patch(monkeypatch)
    bus = EventBus("test-run", path=None)
    telemetry = StepTelemetry(bus, agent="sophia", job="ring_up")

    telemetry.handle(FakeAssistant(FakeThinking("249 plus tax"), FakeText("ok")))

    assert [e.agent for e in bus.events] == ["sophia", "sophia"]
    assert [e.type for e in bus.events] == ["agent_thinking", "agent_text"]


def test_the_report_is_the_last_line_the_agent_said(monkeypatch):
    """The cards all say "end with exactly one line"; anything before it is the
    model talking to itself, and handing that on would poison the next step."""
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="marco", job="order")

    telemetry.handle(FakeAssistant(FakeText("Let me read the menu first.")))
    telemetry.handle(FakeAssistant(FakeText("  ORDERED ABC12  ")))

    assert telemetry.usage.report == "ORDERED ABC12"


def test_an_agent_that_said_nothing_hands_on_an_empty_line(monkeypatch):
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="chef", job="cook")
    assert telemetry.usage.report == ""


def test_tool_use_is_counted_but_not_emitted_twice(monkeypatch):
    """The tool handler emits `tool_called` when it knows the arguments
    survived validation. Emitting here as well would double-count every retry.
    """
    _patch(monkeypatch)
    bus = EventBus("r", path=None)
    telemetry = StepTelemetry(bus, agent="chef", job="cook")

    telemetry.handle(FakeAssistant(FakeToolUse("mcp__shop__get_menu"),
                                   FakeToolUse("mcp__shop__serve_order")))

    assert telemetry.usage.tool_calls == 2
    assert bus.find("tool_called") == []


def test_usage_accumulates_across_results(monkeypatch):
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="chef", job="cook")

    telemetry.handle(FakeResult(in_tokens=100, out_tokens=20, cost=0.01))
    telemetry.handle(FakeResult(in_tokens=50, out_tokens=10, cost=0.02))

    assert telemetry.usage.in_tokens == 150
    assert telemetry.usage.out_tokens == 30
    assert round(telemetry.usage.cost_usd, 4) == 0.03


def test_finishing_records_wall_time(monkeypatch):
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="marco", job="order")
    assert telemetry.finish().wall_ms >= 0


def test_a_report_is_the_last_line_not_the_last_paragraph(monkeypatch):
    """Found live: the chef ended one block with a summary paragraph and the
    report line under it, and Marco was handed the whole paragraph."""
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="chef", job="cook")

    telemetry.handle(FakeAssistant(FakeText(
        "The bench accepted it: one margherita, basil left off as asked.\n\n"
        "SERVED NM673")))

    assert telemetry.usage.report == "SERVED NM673"


def test_cache_reads_count_as_input_tokens(monkeypatch):
    """Otherwise a step that read the shared brief from cache reports `in 6`,
    and the instruction-layer cost vanishes from the summary that exists to
    show it."""
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="marco", job="order")

    result = FakeResult(in_tokens=6, out_tokens=10)
    result.usage["cache_read_input_tokens"] = 1200
    result.usage["cache_creation_input_tokens"] = 400
    telemetry.handle(result)

    assert telemetry.usage.in_tokens == 1606


def test_input_is_split_into_what_was_charged_how(monkeypatch):
    """Lumping the three together hides the only interesting fact about an
    agent's bill: it pays for its context on every turn, and nearly all of
    that arrives as a cache read."""
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="chef", job="cook")

    result = FakeResult(in_tokens=120, out_tokens=40)
    result.usage["cache_read_input_tokens"] = 24_000
    result.usage["cache_creation_input_tokens"] = 1_400
    telemetry.handle(result)

    assert telemetry.usage.in_fresh == 120
    assert telemetry.usage.in_cache_read == 24_000
    assert telemetry.usage.in_cache_write == 1_400
    assert telemetry.usage.in_tokens == 25_520


def test_turns_come_from_the_sdk_not_from_counting_messages(monkeypatch):
    """Round trips are the multiplier on the input column, so the count has to
    be right. One round trip can arrive as several AssistantMessages - the SDK
    splits thinking from text - so counting those over-reports it. The SDK
    reports `num_turns` itself."""
    _patch(monkeypatch)
    telemetry = StepTelemetry(EventBus("r", path=None), agent="marco", job="order")

    telemetry.handle(FakeAssistant(FakeThinking("which pizza")))
    telemetry.handle(FakeAssistant(FakeToolUse("mcp__shop__get_menu")))
    telemetry.handle(FakeAssistant(FakeToolUse("mcp__shop__place_order")))
    telemetry.handle(FakeAssistant(FakeText("ORDERED ABC12")))
    result = FakeResult()
    result.num_turns = 3
    telemetry.handle(result)

    assert telemetry.usage.turns == 3, "not the four messages that carried them"
    assert telemetry.usage.tool_calls == 2
