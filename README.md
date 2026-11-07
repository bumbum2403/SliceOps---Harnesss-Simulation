# SliceOps

A pizza counter run by four agent steps, built to learn harness engineering.

A customer orders, a cashier prices it, a chef cooks it, the customer collects
it and pays. Each step is a different model with a different card, and none of
them can do another's job.

The point is not the pizza. The point is that **everything around the model is
a file you can read**. Anthropic's SDK supplies the inner harness - the agent
loop, the tool protocol, the model call. The outer harness is this repo, and it
is small enough to hold in your head.

---

## Run it

```bash
git clone https://github.com/bumbum2403/SliceOps---Harnesss-Simulation.git
cd SliceOps---Harnesss-Simulation

uv sync                 # once: creates ./.venv, installs from uv.lock
uv run sliceops chain   # who runs, in what order, and what it will cost
uv run sliceops menu    # what the shop sells
uv run sliceops run "one margherita, no basil, extra cheese" --budget 0.60
uv run sliceops replay  # re-render the last run from its log
```

`chain` and `menu` are free - they only read files. `run` spends real money, so
it always takes `--budget`, a hard cap the SDK enforces. A clean shift costs
about **$0.15** and takes about 23 seconds.

One finished run ships with the repo, so you can see the whole thing before
spending anything:

```bash
uv run sliceops replay 20261101-2338-g9jd
```

```
1. marco haiku · order ──────────────────────────────
  handed: "one margherita, no basil, extra cheese"
  · get_menu
  · place_order
    DOOR -> ORDERED
  => ORDERED NM673
  2 tool calls · in 6 out 182 · $0.003 · 5015ms
```

`replay` drives the same renderer from the log alone. If something shows up
live but not in a replay, the log is incomplete - that is the test, not a
convenience.

---

## The four files that are the program

```
shop/
├── AGENTS.md                 the shared brief, and the chain
├── menu.json                 prices and recipes
└── .claude/agents/
    ├── marco.md              the customer   (haiku)
    ├── sophia.md             the cashier    (sonnet)
    └── chef.md               the cook       (opus)
```

`shop/AGENTS.md` holds the chain, and the harness runs exactly these rows:

| step | agent  | job     | hands to |
|------|--------|---------|----------|
| 1    | marco  | order   | sophia   |
| 2    | sophia | ring_up | chef     |
| 3    | chef   | cook    | marco    |
| 4    | marco  | pickup  | -        |

Change a row and the shift changes. There is no manager agent and no model
decides who goes next, so the order of events is something you edit rather than
something you hope for. `sliceops chain` checks the table against the cards
before a run starts, so a chain that hands to an agent with no card fails for
free instead of on step three of four.

Each card declares one worker's whole identity:

```yaml
---
name: sophia
description: Sophia, the cashier...
model: sonnet
tools: mcp__shop__get_menu, mcp__shop__get_order, mcp__shop__ring_up
jobs: ring_up
---
```

They are in Claude Code's own subagent format, so the same card works if you
point the CLI at this folder instead.

---

## How a step runs

Each step is its own session with one agent in it. The agent is handed the
shared brief, its own card, and the one line the previous worker ended with -
and nothing else. It cannot see the previous step's reasoning. What it can see
is the shared state, through Shop tools.

```python
ClaudeAgentOptions(
    cwd=str(SHOP_DIR),
    system_prompt=f"{brief}\n\n---\n\n{role.body}",
    model=role.model,            # from the card
    tools=[],                    # no file access, no shell, no Agent tool
    mcp_servers={"shop": shop_server},
    strict_mcp_config=True,
    allowed_tools=list(role.tools),   # from the card
)
```

`setting_sources` is deliberately left off, so the SDK does not go looking for
a `CLAUDE.md` or an agents directory of its own. Nothing reaches an agent's
context that this repo did not put there explicitly. If an agent knows
something, you can point at the file it came from.

---

## The Shop referees

Agents propose, the Shop disposes. Order ids are minted by the Shop, prices are
checked against `menu.json`, and phases advance only through declared
transitions:

```
DOOR -> ORDERED -> PRICED -> SERVED -> PAID
```

Three enforcement layers, in increasing severity:

1. **the phase gate** - wrong turn. Free: being handed the wrong step is the
   harness's mistake, not the agent's.
2. **attempts** - wrong content. The error names the rule that was broken and
   never the right answer, so a retry has to be work rather than a copy.
3. **the lock** - three strikes and the tool is gone for the rest of the run.

Sensors live in `sliceops/sensors.py` and are pure functions. The same one the
live run uses is the one the tests exercise.

---

## What a shift costs, and why

```
step            model   turns  tools      in  fresh  cached       $
marco·order     haiku       3      2  37,904      6  37,898  0.0027
sophia·ring_up  sonnet      4      3  37,849      6  37,843  0.0466
chef·cook       opus        4      3  37,604      6  37,598  0.0902
marco·pickup    haiku       4      3  50,611      8  50,603  0.0027

163,968 input tokens over 15 API turns - 100% of it context re-sent and
served from cache, 26 charged fresh.
```

Three things worth reading off that table:

**`turns == tool_calls + 1`, exactly.** An agentic loop re-sends the whole
conversation on every round trip, so a step's input is about
`(tool_calls + 1) x context`. One extra tool call costs a whole context, not a
tool result.

**Almost none of it is charged fresh.** 26 tokens out of 163,968. That is why
a shift moving this many tokens costs fifteen cents - and it means the number
to watch is *turns*, not tokens.

**The bill is model choice.** Chef and Sophia moved fewer tokens than Marco's
pickup step and cost 25x more, because their cards say `opus` and `sonnet`.
`model:` in a role card is the biggest cost lever here, and it is one word in
a file.

A probe of each instruction layer says where the context comes from. Run it
yourself - three haiku calls, well under a cent:

```bash
uv run python scripts/probe_context.py
```

| layer | input tokens | added |
|---|---|---|
| the CLI alone: no system prompt, no tools | **9,492** | +9,492 |
| + `shop/AGENTS.md` + `marco.md` | 10,604 | +1,112 |
| + the 5 shop tools Marco may call | **12,478** | +1,874 |

So three quarters of what every step carries is Claude Code's own system
prompt - the inner harness - before SliceOps says a word. Everything in
`shop/` put together is the other quarter.

That baseline is not a constant. It was 9,223 when first measured and 9,492 a
week later, on the same code: the inner harness is versioned by someone else
and changes under you. Re-run the probe rather than trusting the table.

---

## Where everything lives

| file | what it is |
|---|---|
| `sliceops/roles.py` | reads `shop/` - the cards and the chain table |
| `sliceops/runner.py` | the outer harness: walks the chain, one session per step |
| `sliceops/menu.py` | prices and recipes: the ground truth sensors grade against |
| `sliceops/state.py` | the Shop: phases, gates, attempts, the lock |
| `sliceops/sensors.py` | pure validators, one per step |
| `sliceops/tools.py` | the 8 shop tools, and their descriptions |
| `sliceops/events.py` | the JSONL event log - the only source of truth for display |
| `sliceops/telemetry.py` | SDK messages in, events out |
| `sliceops/render.py` | the terminal picture, shared by `run` and `replay` |
| `sliceops/server.py`, `web/` | the browser game |
| `docs/sdk-notes.md` | SDK behaviour verified by live runs, and why things diverge |

Run logs land in `runs/<id>.jsonl`, one JSON object per line, flushed as they
happen so a crashed run still leaves a readable trace.

```bash
uv run pytest       # 108 tests, no network, under a second
```

---

## Watch it

```bash
uv run sliceops play     # http://127.0.0.1:8765
```

**You are Marco.** Type what you want, or press `BUILD…` and click the menu
together - that writes a *sentence*, not an order. Marco still has to read the
menu and call `place_order` himself, which is his job and what his sensor
grades. Posting a finished order from the browser would delete it.

The stage renders the event log and nothing else. It never calls a model and
never computes a price; if a number is on screen it came from an event or from
`/api/menu`. Each beat holds long enough to read - a report line stays up
between 2 and 7 seconds depending on its length - and the **slip** that travels
between two workers is the hand-off itself: the one line the next worker is
given.

The rail on the right is the shift as a conversation:

```
marco  -> shop                                          get_menu
  What have you got today?
    shop -> marco
      3 pizzas, 4 toppings. No drinks, sides, desserts.
marco  -> shop                                       place_order
  "I'd like one pepperoni with extra cheese, but hold the pepperoni."
    shop -> marco
      Order 8BTK9 is in.
marco  -> sophia
  ORDERED 8BTK9
```

Worth knowing what that is and is not. **The agents do not talk to each other.**
Inside a step an agent talks to the Shop in structured tool arguments; the only
natural language that crosses between two workers is the one report line each
one ends with - which is exactly what the hand-off slip carries.

So the page marks the difference. Words the model actually wrote are verbatim,
in quotes and italic. Everything else is the renderer putting a sentence around
arguments that are already in the event, with the raw tool name kept in the
margin. Nothing in a dressed line is computed by the page: `379 and 19 tax -
398 altogether` reads three fields straight out of the `ring_up` call.

`WATCH AGAIN` replays the run you just watched, free. The dropdown replays any
past run, and `?run=<id>` links straight to one.

---

## Contributing

Issues and pull requests are welcome. The repo is a teaching project, so the
bar is less "is this feature good" and more "does this still read like
something you can follow end to end".

```bash
uv sync
uv run pytest            # 108 tests, no network, under a second
uv run sliceops chain    # validates the chain against the cards, free
```

Two rules do most of the work:

**The tests never touch the network.** All game logic lives in `menu.py`,
`state.py` and `sensors.py` as pure functions, and that is what the tests
exercise. If a change can only be verified by a live run, it is probably in the
wrong file.

**The page renders events and nothing else.** `web/` never calls a model and
never computes a price. A number on screen came from an event or from
`/api/menu`; a price computed in JavaScript is a second rounding rule that will
eventually disagree with the first.

The things most worth changing are files, not code: edit the chain table in
`shop/AGENTS.md`, swap a `model:` in a role card, or add a pizza to
`shop/menu.json`, and run `uv run sliceops chain` to see what it cost before
you spend anything. See `CONTRIBUTING.md` for the longer version.

## Licence

MIT. See `LICENSE`.
