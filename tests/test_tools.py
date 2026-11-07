"""The shop tools, driven directly - no SDK, no network, no models.

Every test here calls the same handler a live agent calls, so a green run is
evidence about the real thing rather than about a mock.
"""

from __future__ import annotations

import json

from sliceops.events import EventBus
from sliceops.state import Phase
from sliceops.tools import DESCRIPTIONS, ShopTools


def run(coro):
    """Tiny driver so the tests stay plain functions.

    A pytest-asyncio dependency would buy nothing here: these handlers never
    await anything but each other.
    """
    import asyncio
    return asyncio.run(coro)


def body(result: dict) -> str:
    return result["content"][0]["text"]


def payload(result: dict) -> dict:
    return json.loads(body(result))


def failed(result: dict) -> bool:
    return bool(result.get("is_error"))


# ---- the happy path, end to end ----------------------------------------

def order_args():
    return {"items": [{"pizza": "margherita", "quantity": 1,
                       "extras": ["extra cheese"], "remove": ["basil"]}],
            "customer_line": "one margherita, no basil, extra cheese"}


def walk_the_chain(tools: ShopTools) -> dict:
    """Drive all four steps with correct arguments. Returns the last result."""
    menu = tools.shop.menu
    run(tools.place_order(order_args()))

    items = tools.shop.order.items
    pricing = menu.price_order(items)
    run(tools.ring_up({
        "lines": [{"item": i.pizza, "quantity": i.quantity, "amount": a}
                  for i, a in zip(items, pricing.line_amounts)],
        "subtotal": pricing.subtotal, "tax": pricing.tax, "total": pricing.total}))

    run(tools.serve_order({
        "lines": [{"item": i.pizza, "ingredients": list(menu.ingredients(i))}
                  for i in items]}))

    run(tools.pickup_order({"reaction": "Exactly what I asked for."}))
    return run(tools.pay({"amount": pricing.total}))


def test_a_clean_shift_walks_door_to_paid(tools: ShopTools):
    result = walk_the_chain(tools)
    assert not failed(result)
    assert tools.shop.phase is Phase.PAID
    assert tools.shop.payment.amount == tools.shop.ticket.total


def test_a_clean_shift_trips_no_sensors(tools: ShopTools, bus: EventBus):
    walk_the_chain(tools)
    assert bus.find("sensor_failed") == []
    assert tools.shop.locked == set()


def test_every_step_of_the_chain_leaves_a_state_change_in_the_log(tools: ShopTools,
                                                                  bus: EventBus):
    walk_the_chain(tools)
    moves = [(e.payload["from_phase"], e.payload["to_phase"])
             for e in bus.find("state_changed")]
    assert moves == [("DOOR", "ORDERED"), ("ORDERED", "PRICED"),
                     ("PRICED", "SERVED"), ("SERVED", "PAID")]


# ---- the gate ----------------------------------------------------------

def test_a_tool_called_out_of_turn_is_refused_and_says_why(tools: ShopTools):
    result = run(tools.ring_up({"lines": [], "subtotal": 0, "tax": 0, "total": 0}))
    assert failed(result)
    assert "Not your turn" in body(result)
    assert "DOOR" in body(result)


def test_a_gate_refusal_costs_no_attempt(tools: ShopTools):
    run(tools.serve_order({"lines": []}))
    assert tools.shop.attempts_left("serve_order") == 3


def test_reading_an_order_before_one_exists_is_refused(tools: ShopTools):
    assert failed(run(tools.get_order({})))


def test_paying_without_collecting_first_is_refused(tools: ShopTools):
    menu = tools.shop.menu
    run(tools.place_order(order_args()))
    items = tools.shop.order.items
    pricing = menu.price_order(items)
    run(tools.ring_up({
        "lines": [{"item": i.pizza, "quantity": i.quantity, "amount": a}
                  for i, a in zip(items, pricing.line_amounts)],
        "subtotal": pricing.subtotal, "tax": pricing.tax, "total": pricing.total}))
    run(tools.serve_order({
        "lines": [{"item": i.pizza, "ingredients": list(menu.ingredients(i))}
                  for i in items]}))

    result = run(tools.pay({"amount": pricing.total}))
    assert failed(result) and "pickup_order" in body(result)


# ---- sensors and the lock ----------------------------------------------

def test_a_bad_order_is_rejected_with_fixable_errors(tools: ShopTools):
    result = run(tools.place_order(
        {"items": [{"pizza": "hawaiian"}], "customer_line": "a hawaiian"}))
    assert failed(result)
    assert "attempt 1 of 3" in body(result)
    assert "hawaiian" in body(result)
    assert tools.shop.phase is Phase.DOOR, "a rejected order is not an order"


def test_three_bad_attempts_lock_the_tool_and_fail_the_shift(tools: ShopTools):
    for _ in range(3):
        result = run(tools.place_order(
            {"items": [{"pizza": "hawaiian"}], "customer_line": "x"}))
    assert "LOCKED" in body(result)
    assert tools.shop.phase is Phase.FAILED


def test_a_locked_tool_refuses_even_a_correct_call(tools: ShopTools):
    for _ in range(3):
        run(tools.place_order({"items": [{"pizza": "hawaiian"}], "customer_line": "x"}))
    assert "LOCKED" in body(run(tools.place_order(order_args())))


def test_a_retry_after_a_rejection_still_works(tools: ShopTools):
    run(tools.place_order({"items": [{"pizza": "hawaiin"}], "customer_line": "x"}))
    result = run(tools.place_order(order_args()))
    assert not failed(result)
    assert tools.shop.phase is Phase.ORDERED


def test_malformed_arguments_name_the_field_not_a_traceback(tools: ShopTools):
    result = run(tools.place_order(
        {"items": [{"pizza": "margherita", "quantity": "two"}],
         "customer_line": "x"}))
    assert failed(result)
    assert "quantity" in body(result)
    assert "Traceback" not in body(result)


# ---- what each step can see --------------------------------------------

def test_the_menu_the_agents_read_has_no_finished_answers_in_it(tools: ShopTools):
    served = payload(run(tools.get_menu({})))
    assert "price_table" not in served
    assert "total" not in served


def test_the_cashier_sees_the_order_but_not_a_total_yet(tools: ShopTools):
    run(tools.place_order(order_args()))
    order = payload(run(tools.get_order({})))
    assert order["items"][0]["pizza"] == "margherita"
    assert "total" not in order, "that is the number she is here to work out"


def test_the_customer_is_told_the_total_rather_than_remembering_it(tools: ShopTools):
    walk_the_chain(tools)
    order = payload(run(tools.get_order({})))
    assert order["total"] == tools.shop.ticket.total


def test_the_chef_gets_the_ticket_only_once_it_is_priced(tools: ShopTools):
    run(tools.place_order(order_args()))
    assert failed(run(tools.get_ticket({})))


# ---- advisory notes ----------------------------------------------------

def test_a_pointless_removal_is_noted_and_the_order_still_goes_through(
        tools: ShopTools, bus: EventBus):
    result = run(tools.place_order(
        {"items": [{"pizza": "margherita", "remove": ["olive"]}],
         "customer_line": "margherita, no olives"}))
    assert not failed(result)
    assert tools.shop.phase is Phase.ORDERED
    notes = bus.find("order_note")
    assert notes and "never comes with olive" in notes[0].payload["notes"][0]


# ---- the instruction layer ---------------------------------------------

def test_every_tool_description_is_short_enough_to_be_read(tools: ShopTools):
    """These are the prompt closest to the moment of action. A description
    nobody finishes reading is a description that is not doing its job."""
    for name, text in DESCRIPTIONS.items():
        assert len(text.split()) < 60, f"{name} is {len(text.split())} words"


def test_every_tool_the_shop_serves_has_a_handler_and_a_description(tools: ShopTools):
    for name in DESCRIPTIONS:
        assert callable(getattr(tools, name, None)), name


# ---- both halves of every exchange -------------------------------------

def test_every_successful_tool_records_what_it_handed_back(tools: ShopTools,
                                                           bus: EventBus):
    """Until this existed the log held only half of each exchange: you could
    see that Marco called get_menu and never what he was given, so a replay
    could not show the answer."""
    walk_the_chain(tools)
    run(tools.get_menu({}))
    run(tools.get_order({}))
    run(tools.get_ticket({}))

    called = {e.payload["tool"] for e in bus.find("tool_called")}
    returned = {e.payload["tool"] for e in bus.find("tool_returned")}
    assert called == returned


def test_a_refused_tool_records_no_answer(tools: ShopTools, bus: EventBus):
    """A gate refusal is not an exchange; nothing came back to show."""
    run(tools.ring_up({"lines": [], "subtotal": 0, "tax": 0, "total": 0}))
    assert bus.find("tool_called") and bus.find("tool_returned") == []


def test_the_menu_answer_is_a_summary_not_the_whole_card(tools: ShopTools,
                                                         bus: EventBus):
    """The card is a few hundred lines and would be in the log three times a
    run. The log needs the few facts a reader follows the conversation with."""
    run(tools.get_menu({}))
    payload = bus.find("tool_returned")[0].payload
    assert payload["pizzas"] == ["farmhouse", "margherita", "pepperoni"]
    assert len(json.dumps(payload)) < 400


def test_the_answer_carries_what_a_reader_needs_to_follow_along(tools: ShopTools,
                                                                bus: EventBus):
    walk_the_chain(tools)
    answers = {e.payload["tool"]: e.payload for e in bus.find("tool_returned")}
    assert answers["place_order"]["order_id"] == tools.shop.order_id
    assert answers["ring_up"]["total"] == tools.shop.ticket.total
    assert "margherita" in answers["serve_order"]["lines"][0]
    assert answers["pay"]["paid"] == tools.shop.ticket.total
