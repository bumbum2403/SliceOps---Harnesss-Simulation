"""Shared fixtures. Nothing here touches the network or the SDK."""

from __future__ import annotations

import random

import pytest

from sliceops.config import MENU_PATH, RunConfig
from sliceops.events import EventBus
from sliceops.menu import Menu, OrderItem
from sliceops.state import Shop
from sliceops.tools import ShopTools


@pytest.fixture(scope="session")
def menu() -> Menu:
    return Menu.load(MENU_PATH)


@pytest.fixture
def config() -> RunConfig:
    return RunConfig(order_text="a margherita please")


@pytest.fixture
def shop(menu: Menu, config: RunConfig) -> Shop:
    # A seeded RNG makes order ids deterministic, so tests can assert on them.
    return Shop(menu=menu, config=config, run_id="test-run",
                rng=random.Random(1234))


@pytest.fixture
def bus() -> EventBus:
    return EventBus(run_id="test-run", path=None)  # path=None: no file written


@pytest.fixture
def tools(shop: Shop, bus: EventBus) -> ShopTools:
    return ShopTools(shop, bus)


@pytest.fixture
def margherita() -> OrderItem:
    """One pizza with both an extra and a removal: the interesting case for
    pricing and for the chef's ingredient list at the same time."""
    return OrderItem(pizza="margherita", quantity=1,
                     extras=("extra cheese",), remove=("basil",))
