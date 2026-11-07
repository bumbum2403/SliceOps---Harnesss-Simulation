"""The GUI server: a thin shell around the same engine the CLI drives.

The browser never calls a model and never computes game logic. It receives the
event stream and renders it. That constraint is what makes the GUI trustworthy
as a teaching tool: if something is on screen, it happened, and it is in the
log.

One run at a time (SPEC section 15), so the server holds a single active run
rather than a job table.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import MENU_PATH, PROJECT_ROOT, RUNS_DIR, RunConfig
from .events import Event, EventBus, read_jsonl
from .runner import run_shift

WEB_DIR = PROJECT_ROOT / "web"

# SSE comment frames keep proxies and idle browsers from dropping the stream
# while a model is thinking.
HEARTBEAT_SECONDS = 15.0


class StartRun(BaseModel):
    """What the browser's START button sends.

    Two fields, same as the CLI: the shift has no options. What happens is in
    shop/AGENTS.md and what it costs is in shop/menu.json.
    """

    order_text: str = "a margherita please"
    budget_usd: float = 1.0

    def to_config(self) -> RunConfig:
        return RunConfig(order_text=self.order_text, budget_usd=self.budget_usd)


class ActiveRun:
    """The one shift currently in flight, if any."""

    def __init__(self, run_id: str, bus: EventBus, task: asyncio.Task) -> None:
        self.run_id = run_id
        self.bus = bus
        self.task = task

    @property
    def finished(self) -> bool:
        return self.task.done()


class _NoStoreStatic(StaticFiles):
    """Static files that the browser is never allowed to keep.

    Without an explicit Cache-Control, Chrome applies heuristic freshness and
    will reuse a cached game.js for minutes without revalidating. Because
    index.html and game.js expire independently, that produces the worst
    possible failure: a fresh page wired to a stale script, so a button that
    exists in the markup has no listener and does nothing when clicked. A dev
    GUI that is edited every few minutes has nothing to gain from caching and
    everything to lose.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Papa's Model Pizzeria")
    state: dict[str, ActiveRun | None] = {"active": None}

    # ---- starting a shift ---------------------------------------------

    @app.post("/api/runs")
    async def start_run(request: StartRun) -> dict[str, str]:
        active = state["active"]
        if active is not None and not active.finished:
            raise HTTPException(409, f"run {active.run_id} is still in progress")

        try:
            config = request.to_config()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        # The id is minted here, before the shift starts, so the browser can
        # subscribe to a run that has not emitted anything yet - and the same
        # id is handed to run_shift, so the log's name and its contents agree.
        from .state import new_run_id

        run_id = new_run_id()
        bus = EventBus(run_id, RUNS_DIR / f"{run_id}.jsonl")

        async def shift() -> None:
            try:
                await run_shift(config, bus=bus, run_id=run_id)
            except Exception as exc:  # noqa: BLE001 - the browser must be told
                bus.emit("run_failed", reason=f"{type(exc).__name__}: {exc}",
                         summary={})
                bus.close()

        task = asyncio.create_task(shift())
        state["active"] = ActiveRun(run_id, bus, task)
        return {"run_id": run_id}

    # ---- the menu ------------------------------------------------------

    @app.get("/api/menu")
    async def get_menu() -> dict[str, Any]:
        """The same shop/menu.json the sensors judge against.

        Served rather than copied into the page on purpose: a second hand-kept
        menu in the GUI would drift from the one the shop referees with, and
        the card on the wall would start lying about prices. Read per request,
        so editing menu.json shows up on a refresh.
        """
        from .menu import Menu

        return Menu.load(MENU_PATH).as_dict()

    # ---- who is on shift, and in what order ------------------------------

    @app.get("/api/chain")
    async def get_chain() -> dict[str, Any]:
        """The chain and the cast, read from shop/ exactly as the runner reads
        them.

        The GUI needs both before any run exists: the stage has to know who to
        draw and what order they act in. Hard-coding either in the page would
        let it show `haiku` for a card that had since been moved to `sonnet`,
        or keep animating a step somebody deleted from the chain - a lie about
        the one thing this GUI is built to teach. A live run overrides it from
        `run_started`, which is what that run actually used.
        """
        from .roles import load_chain, load_roles

        roles = load_roles()
        return {
            "cast": {role_id: role.model for role_id, role in roles.items()},
            "chain": [
                {"step": s.index, "agent": s.agent, "job": s.job,
                 "hands_to": s.hands_to}
                for s in load_chain()
            ],
        }

    # ---- reading runs --------------------------------------------------

    @app.get("/api/runs")
    async def list_runs() -> list[dict[str, Any]]:
        """Past runs, newest first, for the replay dropdown."""
        runs = []
        for path in sorted(RUNS_DIR.glob("*.jsonl"), reverse=True):
            try:
                events = read_jsonl(path)
            except (OSError, json.JSONDecodeError):
                continue  # a half-written log from a crashed run
            if not events:
                continue
            started = next((e for e in events if e.type == "run_started"), None)
            ended = next((e for e in reversed(events)
                          if e.type in ("run_finished", "run_failed")), None)
            summary = (ended.payload.get("summary") if ended else {}) or {}
            runs.append({
                "run_id": path.stem,
                "order_text": (started.payload.get("order_text") if started else ""),
                "phase": summary.get("phase"),
                "ok": summary.get("ok"),
                "cost_usd": summary.get("cost_usd"),
                "events": len(events),
                "active": state["active"] is not None
                and state["active"].run_id == path.stem
                and not state["active"].finished,
            })
        return runs

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        """Server-sent events: history first, then live, then close.

        The same endpoint serves a live run and a finished one, so the browser
        uses one code path for playing and replaying.
        """
        active = state["active"]
        if active is not None and active.run_id == run_id and not active.finished:
            stream = _live_stream(active.bus)
        else:
            path = RUNS_DIR / f"{run_id}.jsonl"
            if not path.exists():
                raise HTTPException(404, f"no run log for {run_id}")
            stream = _replay_stream(read_jsonl(path))

        return StreamingResponse(stream, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering
        })

    # ---- the page ------------------------------------------------------

    if WEB_DIR.is_dir():
        app.mount("/static", _NoStoreStatic(directory=WEB_DIR), name="static")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html",
                                headers={"Cache-Control": "no-store"})

    return app


def _sse(event: Event) -> str:
    return f"data: {json.dumps(asdict(event), default=str)}\n\n"


async def _live_stream(bus: EventBus) -> AsyncIterator[str]:
    """History, then live events, in order and without duplicates."""
    # Subscribe BEFORE snapshotting history, so an event emitted in between is
    # captured by the queue rather than lost; the seq check then drops the
    # duplicate.
    queue = bus.subscribe()
    history = bus.events
    for event in history:
        yield _sse(event)
    last_seq = history[-1].seq if history else -1

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break           # run over: close the stream
            if event.seq > last_seq:
                yield _sse(event)
                last_seq = event.seq
    finally:
        bus.unsubscribe(queue)


async def _replay_stream(events: list[Event]) -> AsyncIterator[str]:
    """A finished run: send everything, then close.

    Pacing is the browser's job, not the server's - the animation queue already
    paces the stage so fast events stay readable.
    """
    for event in events:
        yield _sse(event)
