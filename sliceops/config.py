"""Run configuration: everything one shift needs to know before it starts.

Kept free of SDK imports so tests and the pure Shop can build a RunConfig
without pulling in the agent machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

# The agents' runtime cwd. It is deliberately NOT the repo root: if it were,
# every worker would read the builder's dev brief instead of the shop's.
SHOP_DIR = PROJECT_ROOT / "shop"
MENU_PATH = SHOP_DIR / "menu.json"
BRIEF_PATH = SHOP_DIR / "AGENTS.md"
AGENTS_DIR = SHOP_DIR / ".claude" / "agents"
RUNS_DIR = PROJECT_ROOT / "runs"


@dataclass
class RunConfig:
    """One shift's settings.

    Two fields, because the shift itself has no options: the chain in
    `shop/AGENTS.md` decides what happens and `shop/menu.json` decides what it
    costs. Anything that would have lived here is a line in one of those files,
    where you can read it without running anything.
    """

    order_text: str = "a margherita please"
    budget_usd: float = 1.0
    """Hard cap in USD, enforced by the SDK. Every live run sets one."""
