"""The menu: ground truth for what is sold, what it costs and what is in it.

Every number the cashier is judged against and every ingredient list the chef
is judged against is computed here, so a sensor and the agent it grades can
never disagree about what "correct" means.

Why Decimal everywhere: the pricing rule says round half up. Python's built-in
round() is banker's rounding (round half to EVEN), so round(10.5) is 10 while
the rule says 11. A cashier following the written rule would then be marked
wrong by the sensor for a .5 case only. That class of harness bug - the
validator and the instructions disagreeing in a rare branch - is exactly what
this project exists to teach, so it is avoided on purpose.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict


def round_half_up(value: Decimal) -> int:
    """Round to a whole rupee, half away from zero. Never banker's rounding."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


class PizzaSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    price: int
    base: tuple[str, ...]
    """The recipe, in the order the chef should build it."""


class OrderItem(BaseModel):
    """One line of a customer's order, in exact menu vocabulary."""

    model_config = ConfigDict(frozen=True)

    pizza: str
    quantity: int = 1
    extras: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    def describe(self) -> str:
        """One line a person would actually say. Shown in the CLI and the GUI.

        A topping already called "extra cheese" is not prefixed again: the
        first version read "extra extra cheese" on every order that had one.
        """
        parts = [f"{self.quantity} x {self.pizza}"]
        if self.extras:
            parts.append("with " + ", ".join(
                e if e.startswith("extra") else f"extra {e}" for e in self.extras))
        if self.remove:
            parts.append("no " + ", ".join(self.remove))
        return ", ".join(parts)


class Pricing(BaseModel):
    """The one true price of an order."""

    model_config = ConfigDict(frozen=True)

    line_amounts: tuple[int, ...]
    subtotal: int
    tax: int
    total: int


class Menu(BaseModel):
    """shop/menu.json, parsed. Immutable: a shift never edits the menu."""

    model_config = ConfigDict(frozen=True)

    currency: str
    tax_rate: Decimal
    max_items: int
    max_quantity: int
    pizzas: dict[str, PizzaSpec]
    toppings: dict[str, int]
    not_served: tuple[str, ...] = ()
    """Whole categories this shop does not do. Ground truth for saying no:
    without it a model asked for a cold coffee either invents a drinks list or
    substitutes a pizza, and both are worse than "we only do pizza"."""

    # ---- loading -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Menu":
        # parse_float=Decimal keeps 0.05 from ever becoming 0.05000000000000001.
        # Going through float first would silently corrupt every price.
        raw = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        return cls.model_validate(raw)

    def as_dict(self) -> dict:
        """The menu as the agents see it - the whole card, nothing derived.

        The cashier is handed prices, not totals, and the chef is handed
        recipes, not finished ingredient lists. Computing either one for them
        would delete the work their sensor exists to grade.
        """
        return json.loads(self.model_dump_json())

    # ---- pricing -------------------------------------------------------

    def extras_price(self, extras: Sequence[str]) -> int:
        """Flat per topping. Removals give no discount."""
        return sum(self.toppings[t] for t in extras)

    def line_amount(self, item: OrderItem) -> int:
        """(pizza price + extras) x quantity."""
        return (self.pizzas[item.pizza].price + self.extras_price(item.extras)) * item.quantity

    def price_order(self, items: Sequence[OrderItem]) -> Pricing:
        amounts = [self.line_amount(i) for i in items]
        subtotal = sum(amounts)
        tax = round_half_up(Decimal(subtotal) * self.tax_rate)
        return Pricing(line_amounts=tuple(amounts), subtotal=subtotal,
                       tax=tax, total=subtotal + tax)

    # ---- cooking -------------------------------------------------------

    def ingredients(self, item: OrderItem) -> tuple[str, ...]:
        """base - removed + extras, as the chef should list it.

        Ordered, not a set: the chef is told to keep the base order and put
        extras at the end, so the sensor has to check the same thing the card
        asked for. Grading an ordered instruction with an unordered comparison
        is how a rule quietly stops being a rule.
        """
        kept = tuple(i for i in self.pizzas[item.pizza].base if i not in item.remove)
        added = tuple(e for e in item.extras if e not in kept)
        return kept + added
