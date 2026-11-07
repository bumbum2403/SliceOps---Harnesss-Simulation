"""The event log: the single contract between engine, CLI, replay and GUI.

SPEC section 9: the event log is the ONLY source of truth for display. The CLI,
`replay` and the browser all render from these events and nothing else. That is
what makes `replay` a real test - if the renderers can reproduce a run from the
log alone, the log is complete.

Events are append-only, sequence-numbered, and written as one JSON object per
line to runs/<run_id>.jsonl as they happen, so a crashed run still leaves a
readable trace.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    ts: float
    run_id: str
    type: str
    agent: str | None
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            seq=data["seq"],
            ts=data["ts"],
            run_id=data["run_id"],
            type=data["type"],
            agent=data.get("agent"),
            payload=data.get("payload") or {},
        )


class EventBus:
    """Collects a run's events, writes them to disk and fans them out live.

    Subscribers get an unbounded queue each. Unbounded is deliberate: dropping
    an event to apply backpressure would break the "log is the only truth"
    contract, and one shift produces a few hundred events at most.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path
        self._events: list[Event] = []
        self._subscribers: list[asyncio.Queue[Event | None]] = []
        self._listeners: list[Callable[[Event], None]] = []
        self._file = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")

    # ---- producing -----------------------------------------------------

    def emit(self, type: str, agent: str | None = None, **payload: Any) -> Event:
        event = Event(
            seq=len(self._events),
            ts=time.time(),
            run_id=self.run_id,
            type=type,
            agent=agent,
            payload=payload,
        )
        self._events.append(event)

        if self._file is not None:
            self._file.write(event.to_json() + "\n")
            self._file.flush()  # a crashed run must still leave a usable log

        # Synchronous listeners run in emit order, which is what lets the CLI
        # render a live run and a replay through exactly the same code path.
        for listener in self._listeners:
            listener(event)

        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def add_listener(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    def close(self) -> None:
        """Signal end-of-stream to subscribers and release the file."""
        for queue in self._subscribers:
            queue.put_nowait(None)
        if self._file is not None:
            self._file.close()
            self._file = None

    # ---- consuming -----------------------------------------------------

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def subscribe(self) -> asyncio.Queue[Event | None]:
        """A live feed. The caller gets `None` once the run is over."""
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def find(self, type: str) -> list[Event]:
        return [e for e in self._events if e.type == type]


def read_jsonl(path: Path) -> list[Event]:
    """Load a past run. The input to `replay` and `compare`."""
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(Event.from_dict(json.loads(line)))
    return events
