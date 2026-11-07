"""Where a step's context actually comes from.

The README quotes three numbers: what one step carries before SliceOps says a
word, what the brief and a role card add, and what the tool schemas add. They
are measured, not estimated, and this is the thing that measures them - so that
anyone who edits `shop/AGENTS.md` or adds a tool can see what it cost.

It works by building the same options the runner builds and then removing one
layer at a time, so the probe cannot drift from the real thing: layer 3 *is*
`build_options`.

Each layer is one real haiku call with a one-word prompt, so the output is
negligible and the input token count is the thing being measured. Three calls,
well under a cent.

    uv run python scripts/probe_context.py

Note that layer 1 is not ours. It is Claude Code's own system prompt - the
inner harness - and it is the largest of the three.
"""

from __future__ import annotations

import anyio
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient

from sliceops.config import MENU_PATH, SHOP_DIR, RunConfig
from sliceops.events import EventBus
from sliceops.menu import Menu
from sliceops.roles import load_brief, load_roles
from sliceops.runner import build_options
from sliceops.state import Shop
from sliceops.tools import ShopTools, build_shop_server

PROBE = "Say OK."
BUDGET = 0.05


async def measure(options: ClaudeAgentOptions) -> int:
    """Input tokens on the first assistant message: the context it was handed."""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(PROBE)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage) and message.usage:
                usage = message.usage
                return (
                    usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                )
    return 0


async def main() -> None:
    menu = Menu.load(MENU_PATH)
    shop = Shop(menu=menu, config=RunConfig())
    server = build_shop_server(ShopTools(shop=shop, bus=EventBus(run_id="probe")))
    marco = load_roles()["marco"]

    # Layer 3 is the real thing, unmodified. The other two are it, less a layer.
    full = build_options(marco, load_brief(), server, BUDGET)

    # strict_mcp_config matters here as much as it does in a real run. Without
    # it these two layers load whatever MCP servers the developer happens to
    # have in their user settings, and the "bare" baseline measures their
    # machine rather than the CLI. First run of this probe did exactly that
    # and reported a baseline 4,400 tokens too high - see Deviation 6.
    bare = ClaudeAgentOptions(
        cwd=str(SHOP_DIR),
        model=marco.model,
        tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        permission_mode="bypassPermissions",
        max_budget_usd=BUDGET,
    )
    carded = ClaudeAgentOptions(
        cwd=str(SHOP_DIR),
        system_prompt=full.system_prompt,
        model=marco.model,
        tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        permission_mode="bypassPermissions",
        max_budget_usd=BUDGET,
    )

    layers = [
        ("the CLI alone: no system prompt, no tools", bare),
        ("+ shop/AGENTS.md + marco.md", carded),
        (f"+ the {len(marco.tools)} shop tools marco may call", full),
    ]

    baseline = previous = 0
    print(f"{'layer':<46} {'tokens':>8} {'added':>8}")
    for label, options in layers:
        total = await measure(options)
        print(f"{label:<46} {total:>8,} {total - previous:>+8,}")
        baseline = baseline or total
        previous = total

    ours = previous - baseline
    print(f"\n{baseline:,} of {previous:,} is the inner harness "
          f"({baseline / previous:.0%}). Everything in shop/ is {ours:,}.")


if __name__ == "__main__":
    anyio.run(main)
