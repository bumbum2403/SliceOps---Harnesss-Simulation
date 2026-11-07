"""Sensors: what each step is graded on, and how the error reads.

Two things are asserted throughout: that a wrong answer fails, and that the
message a model will read names the offending value. A sensor that fails
correctly but says "invalid" has only done half its job, because the agent's
next attempt is then a guess.
"""

from __future__ import annotations

from sliceops import sensors
from sliceops.menu import Menu, OrderItem
from sliceops.state import ServedLine, TicketLine


# ---- step 1: the order -------------------------------------------------

def test_a_valid_order_passes(menu: Menu, margherita: OrderItem):
    assert sensors.check_place_order(menu, [margherita]) == []


def test_a_pizza_that_is_not_on_the_menu_is_named_with_the_alternatives(menu: Menu):
    errors = sensors.check_place_order(menu, [OrderItem(pizza="hawaiian")])
    assert any("'hawaiian' is not on the menu" in e for e in errors)
    assert any("margherita" in e for e in errors), "list what we do have"


def test_an_empty_order_is_rejected(menu: Menu):
    assert sensors.check_place_order(menu, []) != []


def test_too_many_items_is_rejected(menu: Menu):
    items = [OrderItem(pizza="margherita")] * (menu.max_items + 1)
    assert any("at most" in e for e in sensors.check_place_order(menu, items))


def test_a_quantity_outside_the_limit_is_rejected(menu: Menu):
    errors = sensors.check_place_order(
        menu, [OrderItem(pizza="margherita", quantity=menu.max_quantity + 1)])
    assert any("quantity" in e for e in errors)


def test_removing_a_topping_the_pizza_lacks_is_allowed(menu: Menu):
    """A shop that refuses "margherita, no olives" is wrong about shops, not
    about pizza. The counter mentions it instead."""
    item = OrderItem(pizza="margherita", remove=("olive",))
    assert sensors.check_place_order(menu, [item]) == []


def test_removing_something_the_shop_has_never_heard_of_is_rejected(menu: Menu):
    item = OrderItem(pizza="margherita", remove=("pineapple",))
    assert sensors.check_place_order(menu, [item]) != []


def test_a_pointless_removal_is_a_note_not_a_refusal(menu: Menu):
    item = OrderItem(pizza="margherita", remove=("olive",))
    notes = sensors.pointless_removals(menu, [item])
    assert len(notes) == 1
    assert "never comes with olive" in notes[0]


def test_a_real_removal_produces_no_note(menu: Menu):
    item = OrderItem(pizza="margherita", remove=("basil",))
    assert sensors.pointless_removals(menu, [item]) == []


# ---- step 2: the ticket ------------------------------------------------

def _correct_ticket(menu: Menu, items):
    pricing = menu.price_order(items)
    lines = [TicketLine(item=i.pizza, quantity=i.quantity, amount=a)
             for i, a in zip(items, pricing.line_amounts)]
    return lines, pricing


def test_a_correct_ticket_passes(menu: Menu, margherita: OrderItem):
    lines, pricing = _correct_ticket(menu, [margherita])
    assert sensors.check_ring_up(menu, [margherita], lines, pricing.subtotal,
                                 pricing.tax, pricing.total) == []


def test_a_wrong_line_amount_names_the_line_and_the_rule(menu: Menu,
                                                         margherita: OrderItem):
    lines, pricing = _correct_ticket(menu, [margherita])
    wrong = [lines[0].model_copy(update={"amount": lines[0].amount + 1})]
    errors = sensors.check_ring_up(menu, [margherita], wrong, pricing.subtotal,
                                   pricing.tax, pricing.total)
    assert any("line 1" in e and "pricing rule" in e for e in errors)


def test_the_error_does_not_hand_over_the_right_answer(menu: Menu,
                                                       margherita: OrderItem):
    """An agent told "expected 249" can satisfy the till without ever having
    read the menu, and then the sensor has measured nothing."""
    lines, pricing = _correct_ticket(menu, [margherita])
    wrong = [lines[0].model_copy(update={"amount": 1})]
    errors = sensors.check_ring_up(menu, [margherita], wrong, pricing.subtotal,
                                   pricing.tax, pricing.total)
    assert not any(str(pricing.line_amounts[0]) in e for e in errors)


def test_a_wrong_tax_is_caught_on_its_own(menu: Menu, margherita: OrderItem):
    lines, pricing = _correct_ticket(menu, [margherita])
    errors = sensors.check_ring_up(menu, [margherita], lines, pricing.subtotal,
                                   pricing.tax + 1, pricing.total)
    assert any("tax" in e for e in errors)


def test_a_ticket_with_the_wrong_number_of_lines_fails_early(menu: Menu,
                                                             margherita: OrderItem):
    errors = sensors.check_ring_up(menu, [margherita], [], 0, 0, 0)
    assert len(errors) == 1, "say the one thing that is wrong, not five"
    assert "one line per item" in errors[0]


# ---- step 3: the bench -------------------------------------------------

def test_the_right_ingredients_in_the_right_order_pass(menu: Menu,
                                                       margherita: OrderItem):
    line = ServedLine(item="margherita", ingredients=menu.ingredients(margherita))
    assert sensors.check_serve(menu, [margherita], [line]) == []


def test_the_right_ingredients_in_the_wrong_order_say_so(menu: Menu,
                                                         margherita: OrderItem):
    """Distinguished from a wrong list on purpose: "wrong order" is a one-word
    fix and "missing mozzarella" is a different mistake entirely."""
    scrambled = tuple(reversed(menu.ingredients(margherita)))
    line = ServedLine(item="margherita", ingredients=scrambled)
    errors = sensors.check_serve(menu, [margherita], [line])
    assert len(errors) == 1 and "wrong order" in errors[0]


def test_a_missing_ingredient_is_named(menu: Menu, margherita: OrderItem):
    short = menu.ingredients(margherita)[:-1]
    line = ServedLine(item="margherita", ingredients=short)
    errors = sensors.check_serve(menu, [margherita], [line])
    assert any("missing extra cheese" in e for e in errors)


def test_cooking_a_removed_ingredient_back_in_is_caught(menu: Menu):
    """The failure that matters most: the customer said no basil and got basil.
    """
    item = OrderItem(pizza="margherita", remove=("basil",))
    line = ServedLine(item="margherita",
                      ingredients=("tomato sauce", "mozzarella", "basil"))
    errors = sensors.check_serve(menu, [item], [line])
    assert any("basil is not in this margherita" in e for e in errors)


def test_a_pointless_removal_does_not_change_what_the_chef_must_cook(menu: Menu):
    """The leniency at the counter must not leak into the kitchen's sensor."""
    item = OrderItem(pizza="margherita", remove=("olive",))
    line = ServedLine(item="margherita", ingredients=menu.ingredients(item))
    assert sensors.check_serve(menu, [item], [line]) == []


# ---- step 4: the window ------------------------------------------------

def test_a_reaction_is_required():
    assert sensors.check_pickup("   ") != []
    assert sensors.check_pickup("Looks perfect, thanks.") == []


def test_a_speech_is_not_a_reaction():
    assert sensors.check_pickup("x" * (sensors.MAX_REACTION_CHARS + 1)) != []


def test_paying_the_wrong_amount_is_caught():
    errors = sensors.check_payment(260, 261)
    assert any("260" in e and "261" in e for e in errors)


def test_paying_the_exact_total_passes():
    assert sensors.check_payment(261, 261) == []
