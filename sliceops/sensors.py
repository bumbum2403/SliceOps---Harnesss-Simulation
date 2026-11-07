"""Sensors: pure validators that decide whether an agent did its job.

Every function takes what the agent proposed, compares it to the menu's ground
truth, and returns a list of human-readable errors. Empty list means pass.

Two properties matter and are the reason this is its own module:

* **Pure.** No state, no I/O, no SDK. The same function the live run uses is
  the one the tests exercise, so a green test really does mean a correct run.
* **The error text is a prompt.** It is read by a model that must self-correct,
  so it names the offending value and the rule it broke - never a stack trace,
  never a bare "invalid".

Errors name the rule, not the answer. Handing over the expected number would
make every sensor a free retry and measure nothing: an agent that is told
"expected 448" can satisfy the till without ever having read the menu.
"""

from __future__ import annotations

from collections.abc import Sequence

from .menu import Menu, OrderItem, round_half_up
from .state import ServedLine, TicketLine

MAX_REACTION_CHARS = 200


def _known(name: str, options) -> str:
    """A near-miss list, so a misspelling is one retry rather than three."""
    return f"{name!r} is not on the menu. Available: {', '.join(sorted(options))}"


# ---- step 1: the order -------------------------------------------------

def check_place_order(menu: Menu, items: Sequence[OrderItem]) -> list[str]:
    errors: list[str] = []

    if not items:
        errors.append("the order has no items; a customer who wants nothing "
                      "should not place an order at all")
    if len(items) > menu.max_items:
        errors.append(f"{len(items)} items, but this shop takes at most "
                      f"{menu.max_items} per order")

    for index, item in enumerate(items, start=1):
        where = f"item {index}"
        if item.pizza not in menu.pizzas:
            errors.append(f"{where}: {_known(item.pizza, menu.pizzas)}")
            continue
        if not 1 <= item.quantity <= menu.max_quantity:
            errors.append(f"{where}: quantity {item.quantity} is outside "
                          f"1-{menu.max_quantity}")
        for extra in item.extras:
            if extra not in menu.toppings:
                errors.append(f"{where}: {_known(extra, menu.toppings)}")
        # Removals are NOT checked against the pizza's recipe. Asking for a
        # margherita with no olives is a thing customers say, and a shop that
        # refuses the order over it is wrong about shops, not about pizza.
        # `pointless_removals` turns it into something the counter mentions.
        for removed in item.remove:
            if removed not in menu.toppings and removed not in _all_base(menu):
                errors.append(f"{where}: {removed!r} is not an ingredient this "
                              "shop uses, so it cannot be left off")

    return errors


def _all_base(menu: Menu) -> set[str]:
    return {ingredient for spec in menu.pizzas.values() for ingredient in spec.base}


def pointless_removals(menu: Menu, items: Sequence[OrderItem]) -> list[str]:
    """Removals that change nothing, phrased as something a cashier would say.

    Advisory, never fatal: it is a note on the ticket, not a reason to refuse.
    """
    notes: list[str] = []
    for index, item in enumerate(items, start=1):
        spec = menu.pizzas.get(item.pizza)
        if spec is None:
            continue
        for removed in item.remove:
            if removed not in spec.base:
                notes.append(f"item {index}: {item.pizza} never comes with "
                             f"{removed}, so leaving it off changes nothing")
    return notes


# ---- step 2: the ticket ------------------------------------------------

def check_ring_up(
    menu: Menu,
    items: Sequence[OrderItem],
    lines: Sequence[TicketLine],
    subtotal: int,
    tax: int,
    total: int,
) -> list[str]:
    errors: list[str] = []

    if len(lines) != len(items):
        errors.append(f"the ticket has {len(lines)} lines but the order has "
                      f"{len(items)} items; one line per item")
        return errors

    truth = menu.price_order(items)

    for index, (line, item, expected) in enumerate(
            zip(lines, items, truth.line_amounts), start=1):
        if line.quantity != item.quantity:
            errors.append(f"line {index}: quantity {line.quantity} does not "
                          f"match the order's {item.quantity}")
        if line.amount != expected:
            errors.append(
                f"line {index}: amount {line.amount} does not follow the "
                "pricing rule (pizza price plus each extra topping, times "
                "quantity); re-read the menu and redo this line")

    if subtotal != truth.subtotal:
        errors.append(f"subtotal {subtotal} is not the sum of the line amounts")
    if tax != truth.tax:
        errors.append(f"tax {tax} is not {menu.tax_rate} of the subtotal, "
                      "rounded half up to the rupee")
    if total != truth.total:
        errors.append(f"total {total} is not subtotal plus tax")

    return errors


# ---- step 3: the bench -------------------------------------------------

def check_serve(
    menu: Menu,
    items: Sequence[OrderItem],
    lines: Sequence[ServedLine],
) -> list[str]:
    errors: list[str] = []

    if len(lines) != len(items):
        errors.append(f"{len(lines)} pizzas served but the ticket has "
                      f"{len(items)} lines; one entry per line")
        return errors

    for index, (line, item) in enumerate(zip(lines, items), start=1):
        expected = menu.ingredients(item)
        if tuple(line.ingredients) == expected:
            continue

        got, want = set(line.ingredients), set(expected)
        if got == want:
            errors.append(f"line {index}: right ingredients, wrong order - "
                          "keep the menu's order and put extras at the end")
            continue
        if missing := want - got:
            errors.append(f"line {index}: missing {', '.join(sorted(missing))}")
        if extra := got - want:
            errors.append(f"line {index}: {', '.join(sorted(extra))} is not in "
                          f"this {item.pizza}; build it from the menu recipe, "
                          "minus what was removed, plus what was added")

    return errors


# ---- step 4: the window ------------------------------------------------

def check_pickup(reaction: str) -> list[str]:
    errors: list[str] = []
    if not reaction.strip():
        errors.append("say something about the order; an empty reaction tells "
                      "the shop nothing")
    elif len(reaction) > MAX_REACTION_CHARS:
        errors.append(f"the reaction is {len(reaction)} characters; keep it "
                      f"under {MAX_REACTION_CHARS}")
    return errors


def check_payment(amount: int, total: int) -> list[str]:
    if amount != total:
        return [f"you handed over {amount}, but the ticket total is {total}; "
                "read the total from the Shop rather than remembering it"]
    return []
