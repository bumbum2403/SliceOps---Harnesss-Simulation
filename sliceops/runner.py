"""The outer harness: walking the chain in `shop/AGENTS.md`.

This is the whole of the project's thesis in one loop. Anthropic's SDK runs the
agent - the model call, the tool protocol, the retry on a tool error. Everything
around it is ours and is readable:

* the chain decides who runs, in what order, and who reads their last line
* each step is its own session, with one agent in it
* that agent's context is assembled here, from files, and from nothing else
* the Shop refuses anything the chain did not authorise

Each step is isolated on purpose. A step cannot see the previous step's
reasoning, only the one line it ended with, and the shared state it can read
back through Shop tools. That is what makes the hand-off real rather than a
long conversation with four voices in it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .config import MENU_PATH, RUNS_DIR, SHOP_DIR, RunConfig
from .events import EventBus
from .menu import Menu
from .roles import Role, Step, load_brief, load_chain, load_roles, validate_chain
from .state import Phase, Shop
from .telemetry import StepTelemetry, StepUsage
from .tools import ShopTools, build_shop_server

STEP_MESSAGE = """\
You are on step {index} of {total}. Your job this step is: {job}

{handed_label}:
{handed}

Do that job now, and end with exactly the one line your card specifies."""


@dataclass
class RunResult:
    """What the CLI needs to set an exit code and print a summary."""

    run_id: str
    final_phase: Phase
    steps: list[StepUsage] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.final_phase is Phase.PAID and self.error is None

    @property
    def cost_usd(self) -> float:
        return sum(step.cost_usd for step in self.steps)


def auth_mode() -> str:
    """Which credential the SDK will use. Logged so a run is reproducible."""
    return "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription"


def build_options(
    role: Role,
    brief: str,
    shop_server: Any,
    budget_usd: float,
) -> ClaudeAgentOptions:
    """One worker's whole world, assembled from files.

    Everything this agent will read is named here. There is no second route in:
    `setting_sources` is left off, so the SDK does not go looking for a
    CLAUDE.md or a `.claude/agents` directory of its own, and
    `strict_mcp_config` stops it loading MCP servers from user settings. An
    early live run without that flag put a foreign server's instructions into a
    worker's context, and the customer reported being asked to create a
    document mid-shift.
    """
    # CLAUDE_CODE_SUBAGENT_MODEL silently overrides a card's `model:`, which
    # would make the cast in the log a lie. Removing it from this process's
    # environment is what stops the child inheriting it.
    os.environ.pop("CLAUDE_CODE_SUBAGENT_MODEL", None)

    return ClaudeAgentOptions(
        # The agents' world is shop/, not the repo root. If it were the repo
        # root, every worker would load the builder's dev brief.
        cwd=str(SHOP_DIR),
        # The shared brief first, then this worker's own card. Both are files
        # in shop/ that a human can read before the run.
        system_prompt=f"{brief}\n\n---\n\n{role.body}",
        model=role.model,

        # No built-in tools at all: no file access, no shell, and no Agent tool,
        # so a worker cannot quietly spawn a helper and move work off the chain.
        tools=[],
        mcp_servers={"shop": shop_server},
        strict_mcp_config=True,

        # The card's `tools:` line, enforced by the SDK. This used to need a
        # PreToolUse hook, because one session hosted every agent and
        # allowed_tools is session-wide - it could not say "Sophia may, Chef
        # may not". One agent per session makes the session-wide rule the exact
        # rule, and the hook was deleted.
        allowed_tools=list(role.tools),
        permission_mode="bypassPermissions",

        max_budget_usd=budget_usd,
        thinking={"type": "adaptive"},
    )


async def run_shift(
    config: RunConfig,
    bus: EventBus | None = None,
    on_event: Callable[[Any], None] | None = None,
    run_id: str = "",
) -> RunResult:
    """Run one complete shift and return its summary.

    Emits every event to `bus`. The caller renders from those events only; this
    function returns the summary purely so the CLI can set an exit code.
    """
    menu = Menu.load(MENU_PATH)
    # The GUI names the log file before the shift exists, so it passes the id
    # in. Minting a second one here is how a log ended up whose filename and
    # whose contents named different runs.
    shop = Shop(menu=menu, config=config, run_id=run_id)
    owns_bus = bus is None
    bus = bus or EventBus(shop.run_id, RUNS_DIR / f"{shop.run_id}.jsonl")
    if on_event is not None:
        bus.add_listener(on_event)

    roles = load_roles()
    chain = load_chain()
    brief = load_brief()

    # Checked before a single token is spent. A chain that hands to an agent
    # with no card fails on step 3 of 4 otherwise, after paying for two.
    if problems := validate_chain(chain, roles):
        bus.emit("run_failed", reason="; ".join(problems), summary={})
        if owns_bus:
            bus.close()
        return RunResult(run_id=shop.run_id, final_phase=Phase.FAILED,
                         error="; ".join(problems))

    tools = ShopTools(shop, bus)
    shop_server = build_shop_server(tools)

    bus.emit(
        "run_started",
        order_text=config.order_text,
        run_id=shop.run_id,
        budget_usd=config.budget_usd,
        auth=auth_mode(),
        cast={role_id: role.model for role_id, role in roles.items()},
        chain=[
            {"step": s.index, "agent": s.agent, "job": s.job, "hands_to": s.hands_to}
            for s in chain
        ],
        tools={name: sorted(r.tools) for name, r in roles.items()},
    )

    result = RunResult(run_id=shop.run_id, final_phase=shop.phase)
    handed = config.order_text
    handed_label = "What you came in wanting, in your own words"

    try:
        for step in chain:
            usage = await _run_step(
                step=step, role=roles[step.agent], brief=brief,
                shop=shop, shop_server=shop_server, bus=bus,
                handed=handed, handed_label=handed_label,
                total=len(chain),
                budget_left=max(0.01, config.budget_usd - result.cost_usd),
            )
            result.steps.append(usage)

            if shop.phase is Phase.FAILED:
                result.error = f"{step.agent} locked a tool on step {step.index}"
                break

            # The hand-off: the next worker gets this line and nothing else.
            handed = usage.report
            handed_label = f"What {step.agent} said as they handed this to you"

    except Exception as exc:  # noqa: BLE001 - the log must say what happened
        result.error = f"{type(exc).__name__}: {exc}"
        shop.phase = Phase.FAILED

    result.final_phase = shop.phase

    summary = {
        "run_id": shop.run_id,
        "phase": shop.phase.value,
        "ok": result.ok,
        "cost_usd": round(result.cost_usd, 4),
        "order_text": config.order_text,
        "steps": [
            {"agent": s.agent, "job": s.job, "report": s.report,
             "in_tokens": s.in_tokens, "in_fresh": s.in_fresh,
             "in_cache_read": s.in_cache_read, "in_cache_write": s.in_cache_write,
             "turns": s.turns, "out_tokens": s.out_tokens,
             "cost_usd": round(s.cost_usd, 4), "wall_ms": s.wall_ms,
             "tool_calls": s.tool_calls}
            for s in result.steps
        ],
        "snapshot": shop.snapshot(),
    }
    bus.emit("run_failed" if result.error else "run_finished",
             reason=result.error, summary=summary)
    if owns_bus:
        bus.close()
    return result


async def _run_step(
    step: Step,
    role: Role,
    brief: str,
    shop: Shop,
    shop_server: Any,
    bus: EventBus,
    handed: str,
    handed_label: str,
    total: int,
    budget_left: float,
) -> StepUsage:
    """One link in the chain: one agent, one session, one job."""
    # The Shop is told whose turn it is by the harness that just started them,
    # rather than inferring it from the message stream.
    shop.acting_agent = step.agent

    bus.emit("step_started", agent=step.agent, step=step.index, job=step.job,
             model=role.model, handed=handed)

    telemetry = StepTelemetry(bus, agent=step.agent, job=step.job)
    options = build_options(role, brief, shop_server, budget_left)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(STEP_MESSAGE.format(
            index=step.index, total=total, job=step.job,
            handed_label=handed_label, handed=handed))
        async for message in client.receive_response():
            telemetry.handle(message)

    usage = telemetry.finish()
    shop.acting_agent = None
    bus.emit("step_finished", agent=step.agent, step=step.index, job=step.job,
             report=usage.report, in_tokens=usage.in_tokens,
             in_fresh=usage.in_fresh, in_cache_read=usage.in_cache_read,
             in_cache_write=usage.in_cache_write, turns=usage.turns,
             out_tokens=usage.out_tokens, cost_usd=round(usage.cost_usd, 4),
             wall_ms=usage.wall_ms, tool_calls=usage.tool_calls,
             phase=shop.phase.value)
    return usage
