# SliceOps — context for Claude

A four-step agent chain at a pizza counter, built to learn harness engineering.
`README.md` is the overview; read it first.

## The thesis

Anthropic's SDK is the inner harness. This repo is the outer one, and all of it
is files you can read:

- `shop/AGENTS.md` holds the chain table. **The harness runs those rows.** No
  manager agent, no model deciding who goes next.
- `shop/.claude/agents/*.md` each declare one worker's whole identity: model,
  tools, jobs, brief.
- `shop/menu.json` is the only source of prices and recipes.

Nothing enters an agent's context that `roles.py` did not read from one of
those files and hand over explicitly. `setting_sources` is off for exactly that
reason — if an agent knows something, you can point at the file.

## Working agreement

- Each step is isolated: its own session, one agent, handed only the previous
  worker's last line plus what Shop tools will tell it.
- All game logic lives in `menu.py`, `state.py`, `sensors.py` and is covered by
  tests that never touch the network.
- The GUI renders events only. It never calls models and never computes game
  logic — a price in JavaScript is a second rounding rule.
- The event log is the only source of truth for display. `replay` must be able
  to reproduce a run from it.
- Every live run uses `--budget`.
- Comments explain *why* a harness decision exists.
- Dependencies: claude-agent-sdk, pydantic, typer, rich, fastapi, uvicorn,
  pytest. A browser library loaded from a CDN in `web/` is not a dependency
  here and does not touch `uv.lock`.

## Build state

The CLI harness is complete and verified live: `run 20261101-1821-vvr9` walked
all four steps first try, zero sensor failures, $0.177. `uv run pytest` is green
(108 tests, no network).

`uv run sliceops play` was rebuilt on GSAP and verified live from the browser:
run `20261101-1851-d3pg`, "one farmhouse, no onion, with olive", $0.138, all
four steps, end card correct.

The GUI's one rule: **it renders events**. The order bar writes a sentence, not
a structured order, so Marco still has to read the menu and place it himself.
Three layers in `web/game.js`, and they must stay separate:

  1. `reduce()` - only what must be true before the first beat plays
  2. `beats()`  - events in, timed stage directions out
  3. the pacer  - drains that queue at reading speed

Anything visible belongs in a beat. A replay delivers its whole history in one
burst, so anything updated in `reduce()` jumps to its final value while the
stage is still on step one.

The rail renders each tool call as dialogue. **The agents do not talk to each
other** - within a step they talk to the Shop in structured arguments, and the
only natural language that crosses between two workers is the one report line
each ends with. So the page keeps two categories apart: words the model wrote
(`customer_line`, the pickup `reaction`, every hand-off line) are verbatim, in
quotes and italic; everything else is the renderer putting a sentence around
arguments already in the event, with the raw tool name in the margin. No number
in a dressed line is computed by the page.

### Cost, when you change something

Measured, not guessed. `uv run python scripts/probe_context.py` re-measures
the layers; `docs/sdk-notes.md` has a full run.

- A step's input is about `(tool_calls + 1) x context` - the loop re-sends the
  whole conversation every round trip. **Adding a tool call to a role card
  costs a whole context**, not a tool result.
- ~9.5k of every step's ~12.5k context is Claude Code's own system prompt. Our
  entire instruction stack - brief, card, tool schemas - is the other quarter.
  That baseline moves when the CLI is upgraded (9,223 -> 9,492 in a week, on
  unchanged code), so re-run the probe rather than quoting the number.
- Almost nothing is charged fresh (26 tokens in a 165k-token shift); it is
  nearly all cache reads. Watch *turns*, not tokens.
- The bill is `model:` in a role card. Chef on opus costs 25x Marco on haiku
  while moving fewer tokens.

### What was removed, and why

The orchestrator is gone. Papa was a manager agent that chose who to delegate
to; the owner's call was that this put the lesson in the wrong place. The chain
table replaced him. Consequences worth knowing:

- `hooks.py` was deleted. Its `PreToolUse` guard existed because one session
  hosted every agent, and `allowed_tools` is session-wide — it could not say
  "Sophia may, Chef may not". One agent per session makes the session-wide rule
  the exact rule, enforced by the SDK.
- Telemetry stopped inferring attribution from `parent_tool_use_id`. The
  harness knows whose message it is — it started them.
- Chaos modes, `--feedback`, `--speed`, pizza sizes, bake time and the counter
  conversation all went with it. `SPEC.md` and `EXPERIMENTS.md` were deleted;
  they described a project that no longer exists.

`docs/sdk-notes.md` records the SDK facts this depends on and how each was
verified. It is a dated notebook, so its early sections describe the
orchestrator design that was later removed; read it forwards, not as a
description of the code today.

## It is a public repo now

`CONTRIBUTING.md` is the door for outside contributors and `README.md` is the
tour. If you change how a run is started, what a step costs, or what the page
renders, those two files are part of the change - an outsider has nothing else
to go on.

One finished run, `runs/20261101-2338-g9jd.jsonl`, is committed deliberately so
`replay` works before anyone spends money. Everything else in `runs/` stays
ignored.
