"""The event log contract.

`replay` can only reproduce a run if everything the renderers need survives the
trip to disk and back. These tests guard that round trip.
"""

from __future__ import annotations

import asyncio

from sliceops.events import EventBus, read_jsonl


def test_events_are_sequenced_and_stamped():
    bus = EventBus(run_id="r1", path=None)
    first = bus.emit("run_started", order_text="pizza")
    second = bus.emit("tool_called", agent="marco", tool="get_menu", args={})

    assert (first.seq, second.seq) == (0, 1)
    assert first.run_id == "r1"
    assert first.agent is None
    assert second.agent == "marco"
    assert second.payload == {"tool": "get_menu", "args": {}}
    assert second.ts >= first.ts


def test_a_run_round_trips_through_jsonl(tmp_path):
    path = tmp_path / "run.jsonl"
    bus = EventBus(run_id="r2", path=path)
    bus.emit("run_started", order_text="large margherita")
    bus.emit("state_changed", from_phase="DOOR", to_phase="ORDER_PLACED",
             snapshot={"phase": "ORDER_PLACED"})
    bus.emit("run_finished", papa_final_line="SHIFT COMPLETE: A7F3, rated 5/5")
    bus.close()

    assert read_jsonl(path) == bus.events


def test_the_log_is_flushed_as_it_goes(tmp_path):
    """A crashed run must still leave a readable log, so every emit flushes."""
    path = tmp_path / "run.jsonl"
    bus = EventBus(run_id="r3", path=path)
    bus.emit("run_started")
    assert len(read_jsonl(path)) == 1        # readable before close()


def test_subscribers_receive_events_live_and_a_none_at_the_end():
    async def scenario():
        bus = EventBus(run_id="r4", path=None)
        queue = bus.subscribe()
        bus.emit("papa_text", text="Marco, you're up.")
        bus.close()
        return [await queue.get(), await queue.get()]

    first, terminator = asyncio.run(scenario())
    assert first.type == "papa_text"
    assert terminator is None      # end of stream, so the GUI can close the SSE


def test_a_late_subscriber_misses_nothing_because_history_is_kept():
    bus = EventBus(run_id="r5", path=None)
    bus.emit("run_started")
    bus.emit("papa_text", text="hello")
    # The server replays `events` before attaching the live queue.
    assert [e.type for e in bus.events] == ["run_started", "papa_text"]
