"""The menu: prices and recipes, which are the two things a sensor grades."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sliceops.menu import Menu, OrderItem, round_half_up


def test_rounding_is_half_up_and_not_bankers():
    """Python's round() is banker's rounding, so round(12.5) is 12 while the
    written rule says 13. A cashier following the rule would be marked wrong
    by the sensor on .5 cases only - the validator and the instructions
    disagreeing in a rare branch, which is the bug class this project is about.
    """
    assert round_half_up(Decimal("12.5")) == 13
    assert round_half_up(Decimal("13.5")) == 14
    assert round(12.5) == 12  # the trap, stated out loud


def test_prices_survive_json_without_becoming_floats(menu: Menu):
    assert menu.tax_rate == Decimal("0.05")
    assert menu.tax_rate * 100 == Decimal("5.00")


def test_a_line_is_the_pizza_plus_its_extras_times_quantity(menu: Menu):
    item = OrderItem(pizza="margherita", quantity=2, extras=("extra cheese",))
    assert menu.line_amount(item) == (199 + 50) * 2


def test_removing_a_topping_refunds_nothing(menu: Menu):
    plain = OrderItem(pizza="margherita")
    stripped = OrderItem(pizza="margherita", remove=("basil",))
    assert menu.line_amount(plain) == menu.line_amount(stripped)


def test_the_whole_order_is_priced_in_one_place(menu: Menu, margherita: OrderItem):
    pricing = menu.price_order([margherita])
    assert pricing.line_amounts == (249,)
    assert pricing.subtotal == 249
    assert pricing.tax == 12          # 12.45, half up to the rupee
    assert pricing.total == 261


def test_ingredients_keep_the_menu_order_with_extras_last(menu: Menu):
    item = OrderItem(pizza="margherita", extras=("extra cheese",), remove=("basil",))
    assert menu.ingredients(item) == ("tomato sauce", "mozzarella", "extra cheese")


def test_removing_an_ingredient_the_pizza_lacks_changes_nothing(menu: Menu):
    plain = menu.ingredients(OrderItem(pizza="margherita"))
    asked = menu.ingredients(OrderItem(pizza="margherita", remove=("olive",)))
    assert plain == asked


def test_an_extra_that_is_already_in_the_recipe_is_not_listed_twice(menu: Menu):
    """Otherwise "extra mozzarella" on a farmhouse produces a two-mozzarella
    list that the chef's sensor would then have to accept or reject arbitrarily.
    """
    item = OrderItem(pizza="farmhouse", extras=("mushroom",))
    ingredients = menu.ingredients(item)
    assert len(ingredients) == len(set(ingredients))


def test_the_agents_menu_is_the_card_and_nothing_derived(menu: Menu):
    """Handing the cashier finished totals, or the chef finished ingredient
    lists, would delete the work their sensors exist to grade."""
    served = menu.as_dict()
    assert set(served) == {"currency", "tax_rate", "max_items", "max_quantity",
                           "pizzas", "toppings", "not_served"}


def test_the_shipped_menu_is_three_pizzas_and_four_toppings(menu: Menu):
    assert len(menu.pizzas) == 3
    assert len(menu.toppings) == 4


def test_every_recipe_uses_ingredients_the_shop_can_name(menu: Menu):
    """A base ingredient nobody can order as an extra is fine; one that is
    spelled differently in two places is how a removal silently stops working.
    """
    for name, spec in menu.pizzas.items():
        assert len(spec.base) == len(set(spec.base)), name
        for ingredient in spec.base:
            assert ingredient == ingredient.lower().strip(), (name, ingredient)


def test_an_extra_topping_is_not_prefixed_twice(menu: Menu):
    """Shown live as "extra extra cheese" on every order carrying one."""
    assert "extra extra" not in OrderItem(
        pizza="margherita", extras=("extra cheese",)).describe()
    assert "extra mushroom" in OrderItem(
        pizza="margherita", extras=("mushroom",)).describe()


def test_a_description_names_everything_the_customer_asked_for(menu: Menu):
    line = OrderItem(pizza="pepperoni", quantity=2, extras=("extra cheese",),
                     remove=("pepperoni",)).describe()
    assert "2 x pepperoni" in line and "extra cheese" in line and "no pepperoni" in line
