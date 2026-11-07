# SDK notes

What the **installed** SDK actually does, as opposed to what any document says
it does. Every claim here was checked by running something.

| | |
|---|---|
| `claude-agent-sdk` | **0.2.174** |
| Bundled CLI | **2.1.310** (`claude_agent_sdk/_bundled/claude`) |
| Python | 3.12.13 (project-local `.venv`, uv-managed) |
| Auth | **subscription login** (`claude.ai`, firstParty). `ANTHROPIC_API_KEY` not set |

## How to read this file

It is a **dated notebook, in order**, not a description of the code as it
stands. That is deliberate: several of its entries are findings that caused the
design to change, and the finding is the useful part.

Two things will trip you up if you read it as documentation:

- Everything before **"After the orchestrator (2026-11-01)"** describes a
  design with a manager agent, **Papa**, who chose which worker to delegate to.
  He was removed. The chain table in `shop/AGENTS.md` replaced him, and
  `hooks.py` went with him. If you want today's architecture, start at that
  heading, or read `README.md`.
- It cites a `SPEC.md` and a `scripts/smoke_subagent.py` that no longer exist.
  They were the scaffolding this was built against; the findings outlived them.

The entries that are still live, whatever the date on them: Deviations 1-8,
the `no-store` note, and everything from "After the orchestrator" onwards.

---

# Phase 0: what the installed SDK does

Verified against the smoke harness: **13/13 facts**, cost $0.0308, 5.6s.

## Checklist

| # | Fact | Status | Evidence |
|---|---|---|---|
| 1 | Filesystem subagents load from `<cwd>/.claude/agents/` | **VERIFIED** | init frame `agents=['echo']` with `cwd=<smoke>`, `setting_sources=["project"]` |
| 1 | `shop/CLAUDE.md` loads into **subagents** | **VERIFIED** | house-rules token `PINEAPPLE-7731` arrived as a `ping` argument from the subagent |
| 2 | Frontmatter `tools:` accepts in-process MCP names | **VERIFIED** | `tools: mcp__smoke__ping` → handler ran in-process |
| 3 | Frontmatter `model:` aliases honoured per subagent | **VERIFIED** | `model: haiku` → subagent messages reported `claude-haiku-5-5` |
| 4 | Exact Python option names | **VERIFIED** | see table below |
| 5 | Subagent messages carry `parent_tool_use_id`; usage exposed | **VERIFIED** | parent id matched the delegation `tool_use.id`; `usage` present on both Papa's and the subagent's messages |
| 6 | Delegation tool name | **DIFFERENT** | see Deviation 1 |
| 7 | `PreToolUse` / `SubagentStop` hooks identify the caller | **VERIFIED, better than spec** | see Deviation 3 |
| 8 | In-process MCP tools need `ClaudeSDKClient` | **VERIFIED** | streaming client; handler executed in this process |
| 9 | Foreground vs background delegation | **VERIFIED** | default is foreground; report returned inline, `is_error=False` |
| 10 | Env vars behave as described | **VERIFIED** | `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` reduced `agents` from `['claude','Explore','general-purpose','Plan','statusline-setup']` to `['echo']`. All three vars present in CLI 2.1.310 |
| 11 | Which auth is active | **VERIFIED** | `claude auth status` → `claude.ai` subscription |

## Option names (SPEC 11.4) — confirmed on `ClaudeAgentOptions`

| Need | Option | Note |
|---|---|---|
| Runtime cwd | `cwd: str \| Path` | |
| Load `CLAUDE.md` + `.claude/agents` | `setting_sources: ["project"]` | **must** include `"project"`, else neither loads |
| Papa's brief | `system_prompt: str` | plain `str` = custom prompt |
| Built-in tool set | `tools: list[str]` | `[]` disables all built-ins **including delegation** |
| Auto-approve | `allowed_tools: list[str]` | |
| Block a tool | `disallowed_tools: list[str]` | see Deviation 2 |
| Budget cap | `max_budget_usd: float` | |
| Env for subprocess | `env: dict[str,str]` | |
| Hooks | `hooks: dict[HookEvent, list[HookMatcher]]` | |
| In-process tools | `mcp_servers: {"shop": create_sdk_mcp_server(...)}` | |
| Thinking | `thinking: {"type": "adaptive"\|"enabled"\|"disabled"}` | `max_thinking_tokens` is deprecated |
| Subagent transcript | `forward_subagent_text: bool` | see Deviation 4 |

Hook events available: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`,
**`SubagentStart`**, `PermissionRequest`.

---

## Deviations from the spec

### Deviation 1 — the delegation tool answers to **both** names, in different places

SPEC 11.6 said the tool is named `Agent`, with `Task` as the older name. Reality
on CLI 2.1.310 is that both are live simultaneously, in different parts of the
protocol:

- the **init frame** lists it as `"Task"`
- **`tool_use` blocks** name it `"Agent"`
- the **`tools` option** accepts `"Agent"` and resolves it to `Task`

**Consequence:** telemetry must match `{"Agent", "Task"}` when detecting a
delegation — not as legacy tolerance, but because this build genuinely uses both.
Any check against the init listing must look for `Task`.

### Deviation 2 — `disallowed_tools` blocks, but does not hide, MCP tools

SPEC section 10 says to restrict Papa's tools and "verify from the init message
and fail the run if anything else is present". Only half of that works:

| | built-in tools | in-process MCP tools |
|---|---|---|
| Restrict with `tools=[...]` | yes, listing is exact | not covered |
| Hidden from init listing | yes | **no** — every `mcp_servers` tool is listed for the main agent |
| Call actually blocked | yes | **yes**, by `disallowed_tools` (handler never fires; model sees "Permission to use mcp__shop__ring_up has been denied") |

**Consequence for Phase 3 (revised during the build):** `disallowed_tools` turned
out to be unusable here after all. It is a **session-level** permission rule, so it
cannot express "Papa may not call `ring_up`, but Sophia may" - disallowing the tool
would disable it for the whole session, cashier included. So the implemented design
is:

- built-ins: `tools=["Agent"]`, asserted exactly against the init frame at startup
- shop tools: **the `PreToolUse` guard alone**, which is per-agent because the hook
  input carries `agent_type` (Deviation 3)

The spec calls the guard "defence in depth". For MCP tools it is not: it is the only
per-agent enforcement available, and the only place a violation becomes observable.

This does not weaken experiment 9 ("give Papa `ring_up`"): removing the tool from
`disallowed_tools` and the guard allowlist still genuinely hands it to him.

### Deviation 3 — agent attribution is available directly from the hook

SPEC section 10 has the `PreToolUse` guard "look up which agent is calling (via
telemetry attribution)". Not needed: `PreToolUseHookInput` carries **`agent_type`**
(and `agent_id`) natively, present only when the call comes from inside a subagent
and absent on the main thread.

The SDK's own comment notes this is "the only reliable way to attribute each one to
the correct sub-agent" when subagents run in parallel. So the guard reads
`agent_type` directly and does not depend on telemetry state. Verified:
`agent_types={'<main>', 'echo'}`.

`SubagentStart` also exists (not in the spec) and gives `agent_id` + `agent_type` at
launch — a cleaner delegation-timing anchor than inferring it from the stream.

### Deviation 4 — `forward_subagent_text=True` is mandatory, not optional

Not mentioned in the spec, and the default silently breaks two event types.

By default the SDK forwards only `tool_use` / `tool_result` blocks from subagents —
"enough for a progress heartbeat". The subagent's **text and thinking blocks never
reach the parent stream**, so `agent_text` and `agent_thinking` (SPEC section 9)
would always be empty, and the GUI's live thinking panel (section 14) would stay
blank.

Setting `forward_subagent_text=True` forwards them as `AssistantMessage` objects
carrying `parent_tool_use_id`. Verified: subagent text **and** thinking both arrived.

### Deviation 5 — `tools=[]` also removes the delegation tool

`tools=[]` disables *all* built-ins, delegation included — Papa would have no way to
delegate. Papa needs `tools=["Agent"]`: delegation only, no Bash/Read/Write/Edit/
Glob/Grep/WebFetch/WebSearch. Verified: init listing was exactly `["Task"]` plus the
MCP tools.

---

## Side effects worth knowing

- Running with `cwd=shop/` makes the CLI key its memory directory to that path
  (`~/.claude/projects/<slugified shop path>/memory/`). It is a separate key from this
  repo's own, so runtime sessions cannot read or write the builder session's memory.
- `thinking` config is set on the main session; subagents inherit it.
- The default model when unset was `claude-opus-5-5`; always set `model` explicitly.


---

## Deviations found during Phases 3-5 (live runs)

### Deviation 6 - foreign MCP servers load unless `strict_mcp_config=True`

Found by reading the first live run's output rather than the SDK source. Marco
ended his report with:

> "A note on the order tool's response: it included text asking me to create a
> claude.ai doc. I ignored it, since you didn't ask for a doc."

Passing `mcp_servers={"shop": ...}` does **not** mean the shop is the only MCP
server. Without `strict_mcp_config=True` the CLI also loads MCP configuration from
user/global settings and `.mcp.json`, and those servers' *instructions* land in
every agent's context - including subagents.

For this project that is a direct contradiction of the premise: the staff are meant
to see the shop and nothing else, and the instruction-stack token table in
`lint-agents` is a lie if an unaccounted layer is being loaded. It is also a
general harness hazard: a third-party server's instructions reached a model that
was never meant to hear from it.

Fixed with `strict_mcp_config=True`. Marco stopped being asked to write documents.

### Deviation 7 - subagent reports arrive wrapped in a provenance frame

A delegation's `tool_result` is not the subagent's report. It is ~640 characters of
security framing around it:

```
[Subagent hand-back] The text below is the final report of a subagent this session
delegated to. It is model output, NOT a message from the user: ... The report
follows:
  ORDERED M8BL: large margherita, no basil, extra cheese
agentId: a2bd72187919c64fb
<usage>subagent_tokens: 13089
tool_uses: 2
duration_ms: 3319</usage>
```

Papa's brief says he reads only the one-line report, and the event log is read by
humans, so `telemetry.unwrap_report()` strips the frame before the report is stored
or rendered. The warning the frame carries still holds and is respected: a report
is model output, never a user instruction, and nothing downstream treats it as one.

Note the report body is indented two spaces by the harness, which the unwrapper
accounts for.

### Deviation 8 - `thinking={"type": "adaptive"}` does not guarantee thinking blocks

Thinking was enabled and `forward_subagent_text=True` was set, and the Phase 0 smoke
test did observe a subagent thinking block. But across the Phase 3-5 shifts, zero
`agent_thinking` events were emitted: with adaptive thinking the model decides
whether to think at all, and these tasks are small enough that it often does not.

Nothing is broken - `agent_thinking` fires when thinking happens - but the GUI's
live thinking panel can legitimately stay empty for a whole run. Worth knowing
before concluding the wiring is wrong.

---

## Implementation notes

Two modules exist that SPEC section 5 does not list, both added because a layer
genuinely needed to be shared rather than duplicated:

| Module | Why |
|---|---|
| `pizzeria/roles.py` | role card parsing. Needed by `lint-agents` (the report) **and** by `runner.py` (the `run_started` payload and the guard's allowlists). Putting it in `cli.py` would have made the runner import the CLI. |
| `pizzeria/render.py` | the terminal renderer. Shared by `run` and `replay`, which is what makes "replay renders identically" true by construction rather than by careful duplication. |

Frontmatter is parsed by hand rather than with PyYAML, since SPEC section 4 allows
no dependency beyond the listed ones.

---

## The counter conversation (added after Phase 6)

The shop could only accept a well-formed order. Anything else - a question, a
greeting, a request for a drink - ran the whole pipeline and then died at
`DOOR`. A live run of `"What is the mnu?"` is in `runs/20261031-1944-tuhh.jsonl`:
Marco read the menu and answered the question perfectly, and Papa still had to
close the shift with `SHIFT FAILED`, because there was no outcome in the model
for "we helped them".

Four changes, in order of how much they alter the game.

### 1. `Phase.ANSWERED` - a third ending

Terminal, and off `_PHASE_ORDER` alongside `FAILED`, so no "this phase or
later" gate can be satisfied from it. Reached only from `DOOR`, only when the
cashier states that no order is coming.

`RunResult.GOOD_ENDINGS` is now `(PICKED_UP, ANSWERED)`. This is the load
bearing decision: a question answered is a shop working, so it has to exit 0,
render as `COUNTER CLOSED` rather than a red failure, and never be counted as a
failed run in an experiment.

### 2. `reply_to_customer` - the one tool that does not advance the pipeline

Every other shop tool moves a phase. This one normally leaves the phase exactly
where it was, because answering "what do you have?" should not commit anybody
to anything - which is what makes *question, then order* a single shift rather
than two runs.

It emits its own `counter_reply` event rather than relying on `state_changed`,
precisely because the usual case changes no state and would otherwise be
invisible to the GUI.

Capped at `MAX_COUNTER_REPLIES = 3` by a gate, not by trust: two models can keep
a polite exchange going until the budget runs out. A gate refusal costs no
attempt, so a chatty cashier is slowed down, never locked out.

`get_shop_state` gained `questions_answered`. Without it a `DOOR` phase looks
identical before and after the conversation, and Papa has no way to tell
"nobody has spoken yet" from "we already talked, now take the order".

### 2a. A tool argument that can only be guessed at will be guessed at

The first version of `reply_to_customer` took `order_follows`: *"true if they
still want to order"*. The first live run of `"What is the menu?"` failed
because of it (`runs/20261031-2010-8rpu.jsonl`). Sophia answered the question
perfectly and set `order_follows: true` - a fair read, since someone asking for
a menu usually buys - so the door stayed open for an order nobody placed, Marco
reported `NOT ORDERED`, and the shift died at `DOOR` again. Nobody did anything
wrong; the argument was unanswerable.

The fix was to stop asking for a prediction. `wants_food` asks about the
sentence in front of her - *do their words name food we could make?* - which is
decidable from the text. `"What is the menu?"` names no food, so it is false,
and the counter closes.

**The general lesson, and the reason this is written down:** an argument whose
honest answer is "I cannot know" does not return an error, it returns a guess,
and the guess looks exactly like knowledge in the log. Tool arguments should ask
about the present.

The same run produced a second fix: `Shop.settle_at_close()`. If the shift ends
at `DOOR` with the counter answered and no order, the Shop records `ANSWERED`
regardless of what anyone predicted. Both facts are already in the log, so this
is a transition, not an inference. A `FAILED` shift is left alone - a locked
tool is a real failure and must keep saying so.

### 3. Removals became advisory

`check_place_order` no longer rejects `removed_toppings` that the pizza never
had. "No olives" on a margherita is the commonest thing a real customer says,
and a referee that calls a foul on a harmless request is simply wrong.

The no-op is reported instead, by `sensors.pointless_removals()` -> an
`order_note` event and `Order.notes` - so the leniency is visible rather than a
silent shrug. Extras stay strict: asking for a topping that does not exist is
still an error, because that one is a real vocabulary mistake.

`menu.toppings_applied()` is a set difference, so a pointless removal cannot
leak into what the Chef is graded on. There is a test pinning exactly that.

### 4. `not_served` in menu.json

Ground truth for saying no. Without it, a model asked for a cold coffee either
invents a drinks list or substitutes a pizza, and both are worse than "we only
do pizza". The decision (taken by the owner) was to stay pizza-only and decline
politely rather than add drinks and sides to the menu.

### Role cards: edited directly, by the owner's explicit instruction

`CLAUDE.md` says never to edit `shop/papa.md` or `shop/.claude/agents/*.md`,
and to propose changes instead. The owner was asked and chose to have them
edited directly. What changed:

| File | Change |
|---|---|
| `shop/papa.md` | a step 0 triage (anything that is not a plain order goes to Sophia first), and a third ending line, `COUNTER CLOSED`. |
| `sophia.md` | split into Job A (answer at the counter) and Job B (ring up). Frontmatter gained `mcp__shop__reply_to_customer`. Her `description:` now mentions answering questions, which is what Papa actually routes on. |
| `marco.md` | removals are no longer conditional; a non-pizza request is left out of `items` and admitted in `customer_line` instead of being substituted; a question in the request is ignored because the counter already handled it. |

### Pending spec sync - closed, by deletion

This entry used to list four places where `SPEC.md` had fallen behind the code.
It was never reconciled: the orchestrator was removed a few days later and
`SPEC.md` was deleted along with the design it described.

Left in as a record of how that goes. A spec maintained alongside working code
loses to the code every time, and the honest resolution was to stop keeping two
accounts of the same system. What replaced it is `shop/AGENTS.md`, which is not
a description of the chain - it **is** the chain, parsed and executed, so it
cannot drift.

### GUI

`GET /api/menu` serves `shop/menu.json` so the board on the wall shows the same
prices the sensors grade against. It adds a `price_table` computed server-side,
which is deliberately **not** part of `Menu.as_dict()`: that is what the agents
read, and handing Sophia a finished price table would delete the arithmetic her
sensor exists to grade. Computing it in the browser instead would have put a
second rounding rule in the project, in a language whose `Math.round` does not
agree with `round_half_up`.

`GET /api/roles` serves each role card's `model:` frontmatter so the nameplates
on stage have a cast before any run exists. The page used to carry its own
`DEFAULT_CAST` literal, which would have gone on reading `opus` for a `chef.md`
that had since been moved to `sonnet` — a lie about the one thing this GUI is
built to teach. The literal survives only as the pre-fetch fallback; the first
`run_started` of a live run or a replay overrides it with the cast that run
actually used, which is the honest answer for a replay of an older shift.

The model badge on each character's chest became a nameplate — name above
model — and moved above the head. SPEC section 11 describes a chest badge, and
on the chest is where a plate is sawn in half: the counter, the till and the
manager's desk are all drawn in front of the cast so their furniture occludes
their legs, which means anything pinned to a body disappears behind it. Above
the hat is the only band clear at every spot on the stage. Speech bubbles were
raised 72px to sit above the plate rather than over it — a bubble that hid the
speaker's name would cover exactly what you want to read at the moment a model
says something.

### Static files are served `no-store`

Found the hard way: a MENU button that was reported as doing nothing, and did
nothing, in a browser that had `index.html` from this build and `game.js` from
the one before it. Neither file carried `Cache-Control`, so Chrome applied
heuristic freshness (roughly a tenth of the file's age) and reused the cached
script without revalidating. Because the two files expire independently, the
failure mode is a fresh page wired to a stale script: the markup has the
button, nothing has the listener, and the click is silently swallowed. There is
no console error to find, and the server logs a request that never happened.

`_NoStoreStatic` in `server.py` sets `Cache-Control: no-store` on every static
file, and the `/` route sets it on `index.html`. A GUI that is edited every few
minutes during a build has nothing to gain from caching and this to lose.

---

# After the orchestrator (2026-11-01)

The manager agent was removed at the owner's instruction, and the chain in
`shop/AGENTS.md` took its place. Everything below was verified by live run
`20261101-1821-vvr9`, which walked all four steps first try for $0.177.

## Verified

| # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | `tools=[]` still serves MCP tools | **VERIFIED** | all four steps called `mcp__shop__*` with no built-in tools at all |
| 2 | A step runs with no `setting_sources` | **VERIFIED** | `system_prompt` as a string was the entire context; the shop still worked |
| 3 | `allowed_tools` per session is an exact per-agent allowlist | **VERIFIED** | each step held only its card's tools; no hook needed |
| 4 | A card's `model:` is honoured per session | **VERIFIED** | haiku $0.003, sonnet $0.06, opus $0.11 in the same run |

(1) and (3) together are what let `hooks.py` be deleted. The `PreToolUse` guard
existed only because one session hosted every agent and `allowed_tools` is
session-wide, so it could not say "Sophia may, Chef may not". One agent per
session makes the session-wide rule the exact rule.

(2) matters more than it looks. With `setting_sources` off, the SDK does not go
looking for a `CLAUDE.md` or a `.claude/agents` directory of its own, so there
is no second, invisible route by which instructions reach a worker. Everything
an agent reads was put there by `roles.py`. `shop/.claude/agents/` keeps that
path anyway, because the cards stay drop-in compatible with Claude Code's own
subagent format - but nothing loads them except us.

## Two things only the live run could show

**A report is a line, not a block.** Every card says "end with exactly one
line", and the models did - but the chef put a summary paragraph and the report
line in the *same* text block, so taking the last block handed Marco a
paragraph with `SERVED NM673` buried at the end. `StepUsage.report` now takes
the last non-empty *line*. The general shape: a model obeying an instruction
about its output does not imply a block boundary where you imagined one.

**Cache reads are input tokens.** The first summary printed `in 6` for a step
that had just read a 422-token shared brief, because the SDK reports cache
reads in separate usage fields. A project whose whole subject is the cost of an
instruction layer was hiding that cost in its own summary.
`cache_read_input_tokens` and `cache_creation_input_tokens` are now counted.

## Still unverified

Whether the SDK would load `shop/AGENTS.md` by itself if `setting_sources` were
turned back on. It does not matter here - the harness reads and injects that
file - but it is the one assumption nobody has tested, so do not rely on it.

---

# The stage (2026-11-01)

Rebuilt on **GSAP 3.12.5**, loaded from a CDN in `web/index.html`. It is not a
project dependency: nothing enters `uv.lock` or `.venv`, and the browser is the
only thing that fetches it. A game engine (Phaser, Pixi) was considered and
rejected - this stage has no game loop, no physics and no assets, it replays a
finished event log. What it needed was timelines, which is GSAP's core.

GreenSock's own skills are installed at `~/.claude/skills/gsap-*`. Node here is
v12 and their CLI needs >=18, so they were cloned and copied rather than
installed by `npx`. Three of their rules changed the code:

- **timelines, not chained delays.** A walk is now one timeline with the body
  and both arms placed at position 0, so the arms are guaranteed to stop
  swinging exactly when the feet stop, at any duration. The previous version
  used three tweens with matching delays and drifted.
- **`autoAlpha`, not `opacity`.** At 0 it also sets `visibility: hidden`, so a
  faded speech bubble cannot sit invisibly over the character underneath it.
  The markup still ships these elements with `display: none` so nothing flashes
  before GSAP loads; the class is removed once at boot, because `display: none`
  would defeat `autoAlpha` thereafter.
- **reduced motion is a first-class branch.** `REDUCED` collapses every tween
  to zero duration while leaving **every dwell untouched** - someone who asked
  for less motion still gets the full time to read each line. It also makes a
  headless screenshot truthful, because what is painted is where things are.

## Three layers, and why they must stay apart

`web/game.js` is `reduce()` -> `beats()` -> the pacer. The rule is that
**anything visible belongs in a beat**.

This was found by replaying a finished run: the phase chip read `PAID` while
Marco was still walking to the counter, and the cost chip showed the final
total from the first second. A replay delivers its whole history in one burst,
so `reduce()` - which runs synchronously as each event lands - raced the
animation to the end. `reduce()` now handles only what must be true before the
first beat plays: the run id, the cast and the chain. Phase and cost became
beats, the phase one with `ms: 0` so it costs the viewer no time.

## Z-order is the only thing that says which side of a counter you are on

Drawn side-on, a character behind a counter and a character in front of it are
the same picture. Marco therefore lives in his own `#front-cast` group after
the foreground fittings, while Sophia and Chef are drawn before them. Depth by
y-offset alone was tried first and read as "two people working the till".

That split also broke the spotlight selector, which had been rooted at the one
cast group and could then only dim half the shop. The `focused` class moved to
the `<svg>`.

---

# Showing the conversation (2026-11-01)

## `tool_returned`: the log was holding half of every exchange

Until now the log recorded that Marco called `get_menu` and never what he was
given. "The event log is the only source of truth for display" was therefore
false in a way nothing caught: a replay could show the question and could not
show the answer, because the answer had never been written down.

Each handler now emits `tool_returned` with a **summary**, not the payload. The
menu card is a few hundred lines and would otherwise be in the log three times
a run; what a reader needs to follow the conversation is `pizzas`, `toppings`,
`not_served` - a few dozen bytes. A refused call emits nothing, because a gate
refusal is not an exchange.

## Dialogue is a rendering, and the page says so

The GUI now prints each exchange as speech - "What have you got today?" /
"3 pizzas, 4 toppings." - and this is worth being precise about, because it
would be easy to come away believing the agents chat to each other.

They do not. Within a step an agent talks to the Shop in **structured tool
arguments**. The only natural language that crosses between two workers is the
single report line each one ends with, which is why the hand-off slip carries
exactly that and nothing else.

So `web/game.js` keeps two categories visually apart:

- **Verbatim** - words the model wrote: `customer_line`, the pickup `reaction`,
  every hand-off line. Rendered in quotes, italic, with a rule down the side.
- **Dressed** - the renderer putting a sentence around arguments that are
  already in the event. Marco did not say "What have you got today?"; he called
  `get_menu`.

Every dressed line carries its raw tool name in the margin, so what actually
happened is never more than a glance away, and no number in a dressed line is
computed by the page - `379 and 19 tax - 398 altogether` reads three fields
straight out of the `ring_up` arguments.

## Two things a live run showed

**The menu board's prices were hanging off the board.** `board-price` was
anchored at x=768 while the board itself ends at x=760, with a 316px gulf
between each name and its price. Names and prices now sit in a 196px block
centred on the face.

**"extra extra cheese."** `OrderItem.describe()` prefixed every extra with
"extra", so a topping already called `extra cheese` was doubled on every order
carrying one - visible in the CLI, in the GUI and in the log. Note that fixing
it does not change old logs: a replay of a run from before the fix still says
"extra extra cheese", which is correct. The log records what the shop said at
the time.

---

# Token economics (2026-11-01)

The summary used to print one `in` number per step, and it was true but useless:
input is three different things wearing one name. Split, and measured against
a probe of each instruction layer, the bill becomes legible.

## Where a step's context comes from

One haiku call per layer, Marco's card, same cwd and flags the runner uses.
`scripts/probe_context.py` builds layer 3 by calling `build_options` itself and
then takes layers away, so the probe cannot drift from the real thing.

Re-measured 2026-11-07:

| layer | input tokens | added |
|---|---|---|
| the CLI alone: no system prompt, no tools | **9,492** | +9,492 |
| + `shop/AGENTS.md` + `marco.md` | 10,604 | +1,112 |
| + the 5 shop tools Marco may call | **12,478** | +1,874 |

So of the 12.5k Marco carries before he says a word, **9.5k (76%) is the inner
harness** - Claude Code's own system prompt. Our whole instruction stack, the
shared brief plus his card plus five tool schemas, is 2,986 tokens (24%).

**The baseline is not a constant.** It read 9,223 on 2026-11-01 and 9,492 six
days later, on unchanged code: the layer that dominates is versioned by someone
else. Our two layers were byte-identical across the two runs (+1,112 and
+1,874 both times), which is how you can tell the drift is theirs. Quote the
probe, not the table.

### The probe caught Deviation 6 in itself

First run of the rewritten probe reported a baseline of 13,637 and a layer 3
*below* layer 2 - impossible for nested layers. Cause: layers 1 and 2 were
hand-built options that omitted `strict_mcp_config=True`, so they loaded the
developer's own MCP servers from user settings and measured this machine
instead of the CLI. Layer 3 was `build_options`, which sets the flag, so it
alone was clean.

Worth recording because it is the same failure as the live one in Deviation 6,
found by arithmetic rather than by a customer complaining: a context you did
not assemble is a context you cannot account for.

That is worth sitting with on a project whose subject is instruction layers.
`sliceops chain` prints the cost of the layers we wrote; it cannot print the
one that dominates, because that one is not ours and not in `shop/`.

## Why 160,000 tokens for a four-step shift

Run `20261101-2338-g9jd`, "one pepperoni, no mozzarella, with olive":

| step | model | turns | tools | in | fresh | cached | $ |
|---|---|---|---|---|---|---|---|
| marco·order | haiku | 3 | 2 | 37,904 | 6 | 37,898 | 0.0027 |
| sophia·ring_up | sonnet | 4 | 3 | 37,849 | 6 | 37,843 | 0.0466 |
| chef·cook | opus | 4 | 3 | 37,604 | 6 | 37,598 | 0.0902 |
| marco·pickup | haiku | 4 | 3 | 50,611 | 8 | 50,603 | 0.0027 |

Two facts fall straight out:

**`turns == tool_calls + 1`, exactly, in every step.** An agentic loop re-sends
the entire conversation on every round trip, so a step's input is roughly
`(tool_calls + 1) x context`. Marco's 3 x 12,635 lands on top of the 12,209 the
probe measured. One extra tool call costs a whole context, not a tool result.

**100% of it is cache reads** - 26 fresh tokens across the whole shift. That is
the only reason 164k tokens costs fifteen cents. It also means the number to
watch is *turns*, not tokens: another tool call adds ~12k to the input column
and almost nothing to the bill.

**The bill is model choice, not tokens.** Chef and Sophia moved *fewer* tokens
than Marco's pickup step and cost 25x more, because their cards say `opus` and
`sonnet`. `model:` in a role card is the single biggest cost lever in this
repo, and it is one word in a file.

## `num_turns` comes from the SDK

First version counted `AssistantMessage`s. That over-reports: one round trip
can arrive as several messages when the SDK splits thinking from text, which
made Sophia look like 7 turns when she had taken 5. `ResultMessage.num_turns`
is the SDK's own count and is what the summary now uses. Logs written before
this have no `turns` field.
