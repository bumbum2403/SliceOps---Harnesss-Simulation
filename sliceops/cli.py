"""The SliceOps command line.

    sliceops chain    show the chain and the cards, and check them
    sliceops menu     print shop/menu.json as a card
    sliceops run      run one live shift
    sliceops replay   re-render a past run from its event log
    sliceops play     open the browser game
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.rule import Rule
from rich.table import Table

from .config import MENU_PATH, RUNS_DIR, RunConfig
from .render import ShiftRenderer, colour, short_model
from .roles import (
    MODEL_ALIASES, RoleCardError, estimate_tokens, load_brief, load_chain,
    load_roles, validate_chain,
)
from .tools import DESCRIPTIONS

app = typer.Typer(
    add_completion=False,
    help="SliceOps: a four-agent pizza counter, for learning harness engineering.",
    no_args_is_help=True,
)
console = Console()


# ---- chain -------------------------------------------------------------

@app.command("chain")
def chain_command() -> None:
    """Show the chain and the cards that make it, and check them.

    Run this before spending money on a shift. Everything it prints was read
    out of `shop/`, so it is also the quickest way to see what each agent will
    be told and what it will cost to tell them.
    """
    try:
        roles = load_roles()
        chain = load_chain()
        brief = load_brief()
    except RoleCardError as exc:
        console.print(f"[bold red]cannot read shop/[/bold red] {exc}")
        raise typer.Exit(1)

    console.print()
    console.print(Rule("[bold]the chain[/bold]  shop/AGENTS.md", align="left"))
    for step in chain:
        role = roles.get(step.agent)
        model = short_model(role.model) if role else "[red]no card[/red]"
        hands = f"[dim]->[/dim] {step.hands_to}" if step.hands_to else "[dim]-> done[/dim]"
        console.print(f"  {step.index}. [{colour(step.agent)}]{step.agent:<7}[/] "
                      f"[dim]{model:<7}[/dim] {step.job:<9} {hands}")

    console.print()
    console.print(Rule("[bold]the cards[/bold]  shop/.claude/agents/", align="left"))
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("agent"); table.add_column("model"); table.add_column("jobs")
    table.add_column("brief", justify="right"); table.add_column("tools")

    for role in roles.values():
        model = role.model
        if model not in MODEL_ALIASES:
            model = f"[red]{model}[/red]"
        table.add_row(
            f"[{colour(role.id)}]{role.id}[/]", model, ", ".join(role.jobs),
            f"{role.brief_tokens}t",
            "\n".join(t.replace("mcp__shop__", "") for t in role.tools))
    console.print(table)

    # The shared layer is paid once per agent, per run. Four steps means the
    # brief is bought four times, which is the whole reason to keep it short.
    shared = estimate_tokens(brief)
    console.print(f"\n  [dim]shop/AGENTS.md is {shared}t, read by every step: "
                  f"~{shared * len(chain)}t per shift before anyone says "
                  f"anything[/dim]")

    problems = validate_chain(chain, roles) + _tools_not_declared(roles)
    if problems:
        console.print()
        for problem in problems:
            console.print(f"  [bold red]x[/bold red] {problem}")
        raise typer.Exit(1)
    console.print(f"\n  [green]OK[/green] {len(chain)} steps, {len(roles)} cards, "
                  f"{len(DESCRIPTIONS)} shop tools, no problems.\n")


def _tools_not_declared(roles) -> list[str]:
    """A card asking for a tool the shop does not serve.

    Worth catching here because the SDK does not complain: the tool simply
    never appears, and the agent improvises around a capability it was promised.
    """
    known = {f"mcp__shop__{name}" for name in DESCRIPTIONS}
    return [
        f"{role.path.name}: no such tool {tool!r}"
        for role in roles.values() for tool in role.tools if tool not in known
    ]


# ---- menu --------------------------------------------------------------

@app.command("menu")
def menu_command() -> None:
    """Print the menu card.

    The same `shop/menu.json` the agents read and the sensors grade against.
    Edit that file and this changes; nothing else needs to know.
    """
    from .menu import Menu

    menu = Menu.load(MENU_PATH)
    console.print()
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("pizza"); table.add_column(menu.currency, justify="right")
    table.add_column("made of", style="dim")
    for name, spec in menu.pizzas.items():
        table.add_row(name, str(spec.price), ", ".join(spec.base))
    console.print(table)

    extras = "   ".join(f"{name} +{price}" for name, price in menu.toppings.items())
    console.print(f"\n  [dim]extra toppings[/dim]  {extras}")
    console.print(f"  [dim]we do not serve[/dim] {', '.join(menu.not_served)}")
    console.print(f"  [dim]tax {menu.tax_rate} · at most {menu.max_items} items, "
                  f"{menu.max_quantity} of each[/dim]\n")


# ---- run ---------------------------------------------------------------

@app.command("run")
def run(
    order: Annotated[str, typer.Argument(
        help="What the customer asks for, in their own words.")] = "a margherita please",
    budget: Annotated[float, typer.Option(
        help="Hard cap in USD. Every live run should set one.")] = 1.0,
) -> None:
    """Run one live shift: four agents, four steps, real models."""
    from .runner import run_shift

    config = RunConfig(order_text=order, budget_usd=budget)

    # The renderer sees events only - the same path `replay` uses - so whatever
    # is visible here is guaranteed to be in the log.
    renderer = ShiftRenderer(console)
    result = asyncio.run(run_shift(config, on_event=renderer.handle))

    raise typer.Exit(0 if result.ok else 1)


# ---- replay ------------------------------------------------------------

@app.command("replay")
def replay(
    log: Annotated[Path | None, typer.Argument(
        help="A run id, or a path to a .jsonl log. Defaults to the most recent.")] = None,
    speed: Annotated[float, typer.Option(
        help="Playback speed. 0 renders instantly.")] = 0.0,
) -> None:
    """Re-render a past run from its event log.

    This is a real test of the log, not a convenience: it drives the same
    renderer the live run used, from events alone. Anything visible live but
    missing here is missing from the log.
    """
    from .events import read_jsonl

    path = resolve_run(log)
    if path is None:
        if log is not None:
            console.print(f"[red]no run log for[/red] {log}")
        else:
            console.print("[red]no run logs in runs/[/red] - try `sliceops run` first")
        raise typer.Exit(1)

    events = read_jsonl(path)
    renderer = ShiftRenderer(console)
    previous = None
    for event in events:
        if speed > 0 and previous is not None:
            time.sleep(min(2.0, (event.ts - previous) / speed))
        previous = event.ts
        renderer.handle(event)


def resolve_run(arg: Path | None) -> Path | None:
    """A run id or a path, both accepted; None means the most recent run.

    Run ids are what everything else in the project speaks - the log line, the
    end card, the GUI dropdown and `?run=` all name a run by its id - so typing
    one at `replay` should work. Taking the path too means a log that has been
    moved or renamed is still replayable, which matters for bug reports.
    """
    if arg is None:
        return _newest_run()
    if arg.exists():
        return arg
    by_id = RUNS_DIR / f"{arg.name}.jsonl"
    return by_id if by_id.exists() else None


def _newest_run() -> Path | None:
    logs = sorted(RUNS_DIR.glob("*.jsonl"))
    return logs[-1] if logs else None


# ---- play --------------------------------------------------------------

@app.command("play")
def play(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8765,
    open_browser: Annotated[bool, typer.Option(
        "--open/--no-open", help="Open the browser automatically.")] = True,
) -> None:
    """Start the browser game.

    Binds to localhost only: the shop runs real shifts against your own
    subscription, so it is not something to expose on a network.
    """
    import threading
    import webbrowser

    import uvicorn

    from .server import create_app

    url = f"http://{host}:{port}"
    console.print(f"[magenta]SliceOps[/magenta] -> [bold]{url}[/bold]")

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


def main() -> None:
    app()
