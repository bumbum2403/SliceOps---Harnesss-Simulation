"""Turning one agent's SDK message stream into Shop events.

This module got much smaller when the orchestrator went away, and the reason is
worth keeping: with a manager agent, the SDK hands back one flat stream for the
manager *and* every agent it spawned, and the only way to tell them apart is
`parent_tool_use_id` on each message. Attribution was guesswork built on an SDK
field.

Now each step of the chain is its own session with one agent in it, so the
harness already knows whose message this is - it started them. Attribution is a
constructor argument instead of an inference, and there is no case left where
it can be wrong.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolUseBlock,
)

from .events import EventBus


@dataclass
class StepUsage:
    """What one step of the chain cost."""

    agent: str
    job: str

    # Input is three different things wearing one name, and lumping them
    # together hides the only interesting fact about an agent's bill: a step
    # does not pay for its context once, it pays for it on every turn. Two
    # tool calls means three API turns, each one re-sending everything said
    # so far. Nearly all of it arrives as a cache read.
    in_fresh: int = 0
    """Tokens charged at the full input rate: the first turn, and whatever is
    new on later turns (the tool result that just came back)."""
    in_cache_read: int = 0
    """Context re-sent on a later turn and served from cache. Cheap per token
    - and the bulk of every bill here."""
    in_cache_write: int = 0
    """Context written to the cache so later turns can read it."""

    out_tokens: int = 0
    cost_usd: float = 0.0
    wall_ms: int = 0
    tool_calls: int = 0
    turns: int = 0
    """Round trips, as the SDK counts them in `ResultMessage.num_turns`.

    Counted here rather than inferred from the message stream: one round trip
    can arrive as several AssistantMessages (thinking and text split), so
    counting those over-reports it. This is the multiplier on everything
    above - a step pays for its whole context on every turn.
    """
    text: list[str] = field(default_factory=list)

    @property
    def in_tokens(self) -> int:
        """Every input token the step consumed, however it was charged."""
        return self.in_fresh + self.in_cache_read + self.in_cache_write

    @property
    def report(self) -> str:
        """The agent's last line: what the next worker is handed.

        The last non-empty *line*, not the last block. Every card says "end
        with exactly one line", and models honour that - but they put the
        prose and the line in one block, so taking the block hands the next
        worker a paragraph. A live run showed the chef handing Marco his whole
        summary with `SERVED NM673` buried at the end of it.
        """
        for line in reversed("\n".join(self.text).splitlines()):
            if line.strip():
                return line.strip()
        return ""


class StepTelemetry:
    """Reads one step's messages and writes them to the bus."""

    def __init__(self, bus: EventBus, agent: str, job: str) -> None:
        self.bus = bus
        self.usage = StepUsage(agent=agent, job=job)
        self._started = time.monotonic()

    def handle(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    self.bus.emit("agent_thinking", agent=self.usage.agent,
                                  text=block.thinking)
                elif isinstance(block, TextBlock):
                    self.usage.text.append(block.text)
                    self.bus.emit("agent_text", agent=self.usage.agent,
                                  text=block.text)
                elif isinstance(block, ToolUseBlock):
                    # The call itself is emitted by the tool handler, which
                    # knows the arguments survived validation. Counting here
                    # would double-count every retry.
                    self.usage.tool_calls += 1

        elif isinstance(message, ResultMessage):
            usage = getattr(message, "usage", None) or {}
            # Cache reads are input tokens the step really did consume. Left
            # out, a step that read a 400-token brief from cache reports
            # "in 6", and the instruction-layer cost this project exists to
            # make visible disappears from its own summary.
            self.usage.in_fresh += int(usage.get("input_tokens", 0) or 0)
            self.usage.in_cache_read += int(
                usage.get("cache_read_input_tokens", 0) or 0)
            self.usage.in_cache_write += int(
                usage.get("cache_creation_input_tokens", 0) or 0)
            self.usage.out_tokens += int(usage.get("output_tokens", 0) or 0)
            self.usage.turns += int(getattr(message, "num_turns", 0) or 0)
            self.usage.cost_usd += float(getattr(message, "total_cost_usd", 0.0) or 0.0)

    def finish(self) -> StepUsage:
        self.usage.wall_ms = int((time.monotonic() - self._started) * 1000)
        return self.usage
