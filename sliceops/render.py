"""Terminal rendering: one event stream, one picture.

Shared by `run` and `replay` on purpose. A live run and a replayed log go
through exactly this class, so if something is visible live but missing from a
replay, the log is incomplete - which is the bug worth finding.

Nothing here knows the rules. It is handed events and prints them.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

AGENT_COLOUR = {
    "marco": "orange3",
    "sophia": "cyan",
    "chef": "white",
}

PHASE_COLOUR = {
    "DOOR": "dim",
    "ORDERED": "orange3",
    "PRICED": "cyan",
    "SERVED": "green",
    "PAID": "bold green",
    "FAILED": "bold red",
}


def colour(agent: str | None) -> str:
    return AGENT_COLOUR.get(agent or "", "magenta")


def short_model(model: str) -> str:
    for family in ("haiku", "sonnet", "opus"):
        if family in str(model):
            return family
    return str(model)


class ShiftRenderer:
    """Prints a shift as it happens. One method per event type."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self.cast: dict[str, str] = {}
        self._thinking_shown: set[str] = set()

    def handle(self, event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event.agent, event.payload)

    # ---- the run -------------------------------------------------------

    def _on_run_started(self, agent, payload: dict[str, Any]) -> None:
        self.cast = payload.get("cast") or {}
        self.console.print()
        self.console.print(Rule(f"[bold]SliceOps[/bold]  run {payload.get('run_id')}"))
        self.console.print(f'  customer says: [italic]"{payload.get("order_text")}"[/italic]')

        chain = payload.get("chain") or []
        hops = []
        for step in chain:
            name = step["agent"]
            model = short_model(self.cast.get(name, "?"))
            hops.append(f"[{colour(name)}]{name}[/] [dim]{model}·{step['job']}[/dim]")
        self.console.print("  chain: " + " [dim]->[/dim] ".join(hops))
        self.console.print(f"  [dim]budget ${payload.get('budget_usd')} · "
                           f"auth {payload.get('auth')}[/dim]")
        self.console.print()

    def _on_run_finished(self, agent, payload: dict[str, Any]) -> None:
        self._summary(payload.get("summary") or {})

    def _on_run_failed(self, agent, payload: dict[str, Any]) -> None:
        if reason := payload.get("reason"):
            self.console.print(f"  [bold red]run failed[/bold red] {reason}")
        self._summary(payload.get("summary") or {})

    # ---- the chain -----------------------------------------------------

    def _on_step_started(self, agent, payload: dict[str, Any]) -> None:
        name = payload.get("agent", agent)
        tint = colour(name)
        self.console.print(Rule(
            f"[{tint}]{payload.get('step')}. {name}[/{tint}] "
            f"[dim]{short_model(payload.get('model', ''))} · {payload.get('job')}[/dim]",
            align="left", style="dim"))
        handed = (payload.get("handed") or "").strip()
        if handed:
            self.console.print(f'  [dim]handed:[/dim] [italic]"{_clip(handed, 120)}"[/italic]')

    def _on_step_finished(self, agent, payload: dict[str, Any]) -> None:
        report = payload.get("report") or "(said nothing)"
        self.console.print(f"  [{colour(agent)}]=>[/] {report}")
        self.console.print(
            f"  [dim]{payload.get('tool_calls', 0)} tool calls · "
            f"in {payload.get('in_tokens', 0)} out {payload.get('out_tokens', 0)} · "
            f"${payload.get('cost_usd', 0)} · {payload.get('wall_ms', 0)}ms[/dim]")

    # ---- what an agent did ---------------------------------------------

    def _on_agent_thinking(self, agent, payload: dict[str, Any]) -> None:
        # One line per step: thinking is useful as a signal that reasoning
        # happened, and unreadable as a wall of text in a terminal.
        text = _clip(payload.get("text", ""), 160)
        if not text or agent in self._thinking_shown:
            return
        self._thinking_shown.add(agent)
        self.console.print(f"  [dim italic]{text}[/dim italic]")

    def _on_tool_called(self, agent, payload: dict[str, Any]) -> None:
        self.console.print(f"  [dim]·[/dim] [{colour(agent)}]{payload.get('tool')}[/]")

    def _on_tool_returned(self, agent, payload: dict[str, Any]) -> None:
        """What came back. Terse here; the browser dresses it as dialogue."""
        skip = ("tool",)
        summary = ", ".join(
            f"{k}={', '.join(map(str, v))}" if isinstance(v, list) else f"{k}={v}"
            for k, v in payload.items()
            if k not in skip and v is not None and v != []
        )
        self.console.print(f"    [dim]{_clip(summary, 110)}[/dim]")

    def _on_sensor_failed(self, agent, payload: dict[str, Any]) -> None:
        self.console.print(
            f"    [red]x {payload.get('tool')} failed "
            f"(attempt {payload.get('attempt')}/3)[/red]")
        for error in payload.get("errors") or []:
            self.console.print(f"      [red]- {error}[/red]")

    def _on_gate_rejected(self, agent, payload: dict[str, Any]) -> None:
        self.console.print(
            f"    [yellow]gate {payload.get('tool')}: shop is "
            f"{payload.get('phase')}, needs {payload.get('required_phase')}[/yellow]")

    def _on_order_note(self, agent, payload: dict[str, Any]) -> None:
        for note in payload.get("notes") or []:
            self.console.print(f"    [cyan]note: {note}[/cyan]")

    def _on_state_changed(self, agent, payload: dict[str, Any]) -> None:
        to_phase = payload.get("to_phase", "")
        self.console.print(
            f"    [dim]{payload.get('from_phase')} ->[/dim] "
            f"[{PHASE_COLOUR.get(to_phase, 'white')}]{to_phase}[/]")

    # ---- the summary ---------------------------------------------------

    def _summary(self, summary: dict[str, Any]) -> None:
        if not summary:
            return
        self.console.print()

        table = Table(box=None, pad_edge=False, header_style="dim")
        table.add_column("step"); table.add_column("model", style="dim")
        table.add_column("turns", justify="right")
        table.add_column("tools", justify="right")
        table.add_column("in", justify="right")
        table.add_column("fresh", justify="right", style="dim")
        table.add_column("cached", justify="right", style="dim")
        table.add_column("out", justify="right"); table.add_column("$", justify="right")
        table.add_column("time", justify="right")

        totals = {"in": 0, "fresh": 0, "cached": 0, "out": 0, "turns": 0}
        for step in summary.get("steps") or []:
            name = step["agent"]
            cached = step.get("in_cache_read", 0) + step.get("in_cache_write", 0)
            totals["in"] += step.get("in_tokens", 0)
            totals["fresh"] += step.get("in_fresh", 0)
            totals["cached"] += cached
            totals["out"] += step.get("out_tokens", 0)
            totals["turns"] += step.get("turns", 0)
            table.add_row(
                f"[{colour(name)}]{name}·{step['job']}[/]",
                short_model(self.cast.get(name, "?")),
                str(step.get("turns", 0)),
                str(step.get("tool_calls", 0)),
                f"{step.get('in_tokens', 0):,}",
                f"{step.get('in_fresh', 0):,}", f"{cached:,}",
                f"{step.get('out_tokens', 0):,}",
                f"{step.get('cost_usd', 0):.4f}",
                f"{step.get('wall_ms', 0) / 1000:.1f}s")
        self.console.print(table)

        # The line that explains the bill. A step does not pay for its context
        # once; it pays on every turn, and nearly all of it comes back as a
        # cache read. Without this the input column just looks wrong.
        if totals["in"]:
            share = 100 * totals["cached"] / totals["in"]
            self.console.print(
                f"\n  [dim]{totals['in']:,} input tokens over {totals['turns']} "
                f"API turns - {share:.0f}% of it context re-sent and served "
                f"from cache, {totals['fresh']:,} charged fresh.[/dim]")

        phase = summary.get("phase", "?")
        verdict = "[bold green]SHIFT COMPLETE[/bold green]" if summary.get("ok") \
            else "[bold red]SHIFT FAILED[/bold red]"
        snapshot = summary.get("snapshot") or {}
        if payment := snapshot.get("payment"):
            verdict += f"  [dim]paid {payment.get('amount')}[/dim]"
        self.console.print(f"\n  {verdict}  [dim]ended {phase} · "
                           f"${summary.get('cost_usd', 0)}[/dim]")
        self.console.print(f"  [dim]runs/{summary.get('run_id')}.jsonl[/dim]\n")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
