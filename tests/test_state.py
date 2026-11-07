"""The Shop as referee: phases, gates, attempts and the lock."""

from __future__ import annotations

import pytest

from sliceops.menu import OrderItem
from sliceops.state import (
    MAX_ATTEMPTS, Phase, ServedLine, Shop, ShopRefusal, TicketLine,
    phase_at_least,
)


def test_a_shift_starts_at_the_door(shop: Shop):
    assert shop.phase is Phase.DOOR
    assert shop.order is None


def test_the_phases_walk_the_chain_in_order(shop: Shop, margherita: OrderItem):
    shop.place_order([margherita], customer_line="a margherita, no basil")
    assert shop.phase is Phase.ORDERED

    shop.ring_up([TicketLine(item="margherita", quantity=1, amount=249)],
                 subtotal=249, tax=12, total=261)
    assert shop.phase is Phase.PRICED

    shop.serve([ServedLine(item="margherita", ingredients=("tomato sauce",))])
    assert shop.phase is Phase.SERVED

    shop.pick_up(reaction="smells great")
    assert shop.phase is Phase.SERVED, "collecting is not the end; paying is"

    shop.pay(261)
    assert shop.phase is Phase.PAID


def test_order_ids_are_minted_by_the_shop_not_the_agent(shop: Shop,
                                                        margherita: OrderItem):
    order = shop.place_order([margherita], customer_line="")
    assert order.order_id and order.order_id == shop.order_id
    assert "O" not in order.order_id and "0" not in order.order_id


# ---- gates -------------------------------------------------------------

def test_a_tool_called_out_of_turn_is_refused(shop: Shop):
    with pytest.raises(ShopRefusal) as caught:
        shop.require_phase("ring_up", Phase.ORDERED)
    assert caught.value.kind == "gate"
    assert "DOOR" in caught.value.message


def test_a_gate_refusal_costs_no_attempt(shop: Shop):
    """Being handed the wrong step is the harness's mistake, not the agent's.
    Charging an attempt for it would lock a tool over a chain edit."""
    with pytest.raises(ShopRefusal):
        shop.require_phase("ring_up", Phase.ORDERED)
    assert shop.attempts_left("ring_up") == MAX_ATTEMPTS


def test_a_read_gate_accepts_this_phase_or_any_later_one(shop: Shop,
                                                         margherita: OrderItem):
    shop.place_order([margherita], customer_line="")
    shop.require_phase_at_least("get_order", Phase.ORDERED)   # exact
    shop.ring_up([], subtotal=0, tax=0, total=0)
    shop.require_phase_at_least("get_order", Phase.ORDERED)   # later


def test_a_failed_shift_is_past_no_phase_at_all(shop: Shop):
    shop.phase = Phase.FAILED
    assert not phase_at_least(Phase.FAILED, Phase.DOOR)


# ---- attempts and the lock ---------------------------------------------

def test_three_sensor_failures_lock_the_tool_and_fail_the_shift(shop: Shop):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert shop.register_failure("ring_up") == attempt
    assert "ring_up" in shop.locked
    assert shop.phase is Phase.FAILED


def test_a_locked_tool_stays_dead(shop: Shop):
    for _ in range(MAX_ATTEMPTS):
        shop.register_failure("ring_up")
    with pytest.raises(ShopRefusal) as caught:
        shop.require_unlocked("ring_up")
    assert caught.value.kind == "locked"


def test_attempts_are_counted_per_tool(shop: Shop):
    shop.register_failure("ring_up")
    assert shop.attempts_left("ring_up") == MAX_ATTEMPTS - 1
    assert shop.attempts_left("serve_order") == MAX_ATTEMPTS


# ---- what a renderer is told -------------------------------------------

def test_every_phase_names_the_step_that_comes_next(shop: Shop):
    for phase in Phase:
        shop.phase = phase
        assert shop.next_expected_step()


def test_a_snapshot_is_plain_json_able_data(shop: Shop, margherita: OrderItem):
    shop.place_order([margherita], customer_line="hi")
    snapshot = shop.snapshot()
    assert snapshot["phase"] == "ORDERED"
    assert snapshot["order"]["items"][0]["pizza"] == "margherita"
    assert snapshot["ticket"] is None

    import json
    json.dumps(snapshot)  # raises if anything in here is not serialisable


def test_a_run_id_can_be_minted_before_the_shift_that_fills_it(menu, config):
    """The GUI names the log file first, then hands the id to the runner.

    Found live: the server minted one throwaway Shop for the id and the runner
    minted a second, so runs/<a>.jsonl contained events claiming to be run <b>,
    and the end card linked to a file that did not exist.
    """
    from sliceops.state import new_run_id

    run_id = new_run_id()
    assert Shop(menu=menu, config=config, run_id=run_id).run_id == run_id
    assert new_run_id() != "" and " " not in new_run_id()


def test_a_shop_given_no_id_still_mints_one(menu, config):
    assert Shop(menu=menu, config=config).run_id
