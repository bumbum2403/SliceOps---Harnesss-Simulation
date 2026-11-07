# Contributing to SliceOps

SliceOps is a teaching project. It exists so that somebody can read an entire
agent harness in an afternoon and point at the file responsible for every
behaviour they see. That goal is the review standard: a change that makes the
shop smarter but the harness harder to follow is a change in the wrong
direction.

## Getting set up

```bash
uv sync                  # creates ./.venv from uv.lock
uv run pytest            # 108 tests, no network, under a second
uv run sliceops chain    # validates the chain against the cards, free
uv run sliceops replay 20261101-2338-g9jd   # a committed run, free
```

Nothing above costs money or touches the network. You can get a long way
without an API key.

To run a live shift you need Claude credentials - either a `claude.ai`
subscription login or `ANTHROPIC_API_KEY`. A clean shift is about **$0.15**:

```bash
uv run sliceops run "one farmhouse, no onion, with olive" --budget 0.60
```

`--budget` is required. It is a hard cap the SDK enforces, and it is required
precisely so that a runaway loop is a small bill rather than a story.

## The shape of the thing

| layer | where | rule |
|---|---|---|
| the chain | `shop/AGENTS.md` | the pipe table **is** the program; the harness walks its rows |
| the workers | `shop/.claude/agents/*.md` | one card = one worker's whole identity |
| the prices | `shop/menu.json` | the only source of prices and recipes |
| the harness | `sliceops/` | assembles context from those files and nothing else |
| the stage | `web/` | renders the event log, and only the event log |

The best first contributions are in the top three rows. Add a pizza, swap a
`model:`, reorder the chain - then run `uv run sliceops chain` and watch the
projected cost change before you spend anything.

## Four rules that are not style preferences

**1. Tests never touch the network.** Game logic lives in `menu.py`,
`state.py` and `sensors.py` as pure functions, and that is what the tests
exercise. If your change can only be verified by a live run, it is probably in
the wrong file. Sensors in particular are pure: the function the live run calls
is the function the test calls.

**2. The page renders events.** `web/` never calls a model and never computes
a price. If a number is on screen it came from an event or from `/api/menu`. A
price computed in JavaScript is a second rounding rule, and it will eventually
disagree with the Python one.

**3. The event log is the only source of truth for display.** `replay` has to
be able to reproduce a run from its log alone. If something appears in a live
run but not in a replay, the log is incomplete - treat that as the bug, not as
a quirk of replay.

**4. Nothing enters an agent's context implicitly.** `setting_sources` is off
and `strict_mcp_config` is on, so the SDK will not load a stray `CLAUDE.md` or
somebody's personal MCP server into a worker. If an agent knows something, a
reviewer must be able to point at the file it came from. Both flags were turned
on because of real incidents; `docs/sdk-notes.md` has them.

## Cost is a review concern

An agentic loop re-sends the whole conversation every round trip, so a step's
input is about `(tool_calls + 1) x context`. **Adding a tool call to a role
card costs a whole context, not a tool result.** If a PR adds a tool or grows
the brief, say what it did to the numbers:

```bash
uv run python scripts/probe_context.py   # three haiku calls, well under a cent
```

Three quarters of every step's context is Claude Code's own system prompt,
before SliceOps says a word. Our entire instruction stack is the other quarter,
which is the thing you are actually editing.

## Comments

Comment the *why*, especially for harness decisions. `tools=[]` and
`strict_mcp_config=True` look like boilerplate and are not; each one has an
incident behind it. A comment that explains what the next line does is noise, a
comment that explains why the obvious alternative was rejected is the point.

## Pull requests

- Keep `uv run pytest` green, and add a test for behaviour you changed.
- Update `README.md` if you changed how a run is started, what it costs, or
  what the page shows. An outside reader has nothing else to go on.
- Dependencies are deliberately few: claude-agent-sdk, pydantic, typer, rich,
  fastapi, uvicorn, pytest. A browser library loaded from a CDN in `web/` is
  not a dependency here and does not touch `uv.lock`. Adding a Python
  dependency needs a reason in the PR description.
- Describe what you actually ran. "Tests pass, plus one live shift at
  `--budget 0.60`, $0.14" is worth more than a paragraph of intent.

## Things that would be genuinely interesting

- A fifth step in the chain - adding a row to a markdown table should be all it
  takes, and if it is not, that is a bug in the harness.
- A second shop: a different `shop/` directory with its own brief, cards and
  menu, driven by the same `sliceops/`.
- Sensors for cases the current ones miss. They are pure functions with no
  network, so they are the cheapest place in the repo to be thorough.
- Anything that makes a failure legible. The lock, the gate and the attempt
  counter are the three ways a shift can go wrong, and the stage could say more
  about all three.

By contributing you agree your work is licensed under the MIT License.
