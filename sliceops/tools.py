"""The `shop` MCP server: the only way an agent can touch the world.

Served in-process, so tool names reach the models as `mcp__shop__<name>`.

Two things are happening in this file, and only one of them is code.

The **descriptions** are prompts. They are the shortest instruction layer and
the one closest to the moment of action, so they are written for a model that
is deciding whether to call right now: what the tool does, who calls it, when,
and what an error means. Kept under 60 words each.

The **handlers** are deliberately thin. Every rule lives in menu.py, state.py
and sensors.py so the whole game is testable without the SDK. A handler only
sequences the same five steps: announce, gate, validate, mutate, report.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from . import sensors
from .events import EventBus
from .menu import OrderItem
from .state import Phase, ServedLine, Shop, ShopRefusal, TicketLine


def _text(payload: Any) -> dict[str, Any]:
    """A successful MCP result."""
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=1, default=str)
    return {"content": [{"type": "text", "text": body}]}


def _error(message: str) -> dict[str, Any]:
    """A failed MCP result. `is_error` is what makes the model treat it as one."""
    return {"content": [{"type": "text", "text": message}], "is_error": True}


class ShopTools:
    """Tool handlers bound to one shift.

    Separate from the MCP server so tests can call `await tools.ring_up({...})`
    directly, with no CLI subprocess and no network.
    """

    def __init__(self, shop: Shop, bus: EventBus) -> None:
        self.shop = shop
        self.bus = bus

    # ---- shared plumbing ----------------------------------------------

    @property
    def _agent(self) -> str | None:
        return self.shop.acting_agent

    def _announce(self, tool: str, args: dict[str, Any]) -> None:
        self.bus.emit("tool_called", agent=self._agent, tool=tool, args=args)

    def _refuse(self, tool: str, refusal: ShopRefusal, required: Phase | None) -> dict:
        """A gate or lock refusal. It does NOT consume an attempt: being handed
        the wrong step is the harness's mistake, not the agent's."""
        if refusal.kind == "gate":
            self.bus.emit(
                "gate_rejected", agent=self._agent, tool=tool,
                phase=self.shop.phase.value,
                required_phase=required.value if required else None,
            )
        return _error(refusal.message)

    def _sensor_failed(self, tool: str, errors: list[str]) -> dict:
        """Report a validation failure in the voice the agent must act on."""
        attempt = self.shop.register_failure(tool)
        self.bus.emit("sensor_failed", agent=self._agent, tool=tool,
                      errors=errors, attempt=attempt)

        if tool in self.shop.locked:
            # Third strike: the tool is gone and the shift is over.
            return _error(
                f"LOCKED: {tool} failed validation {attempt} times. "
                "Report BLOCKED and stop."
            )

        bullets = "\n".join(f"- {e}" for e in errors)
        return _error(
            f"{tool} failed validation (attempt {attempt} of 3). "
            f"Fix exactly these and call it again:\n{bullets}"
        )

    def _returned(self, tool: str, **summary: Any) -> dict[str, Any]:
        """Record what a tool handed back, then hand it back.

        The log is the only source of truth for display, and until this existed
        it held only half of every exchange: you could see that Marco called
        get_menu and never what he was given. A replay could not show the
        answer because the answer was never written down.

        A summary, not the payload: the menu is a few hundred lines and would
        be in the log three times a run. These are the few facts a reader
        needs to follow the conversation.
        """
        self.bus.emit("tool_returned", agent=self._agent, tool=tool, **summary)
        return summary

    def _sensor_passed(self, tool: str) -> None:
        self.bus.emit("sensor_passed", agent=self._agent, tool=tool)

    def _changed(self, from_phase: Phase) -> None:
        self.bus.emit("state_changed", agent=self._agent,
                      from_phase=from_phase.value,
                      to_phase=self.shop.phase.value,
                      snapshot=self.shop.snapshot())

    # ---- reads ---------------------------------------------------------

    async def get_menu(self, args: dict[str, Any]) -> dict:
        tool = "get_menu"
        self._announce(tool, args)
        menu = self.shop.menu
        self._returned(tool, pizzas=sorted(menu.pizzas),
                       toppings=sorted(menu.toppings),
                       not_served=list(menu.not_served))
        return _text(menu.as_dict())

    async def get_order(self, args: dict[str, Any]) -> dict:
        tool = "get_order"
        self._announce(tool, args)
        try:
            self.shop.require_phase_at_least(tool, Phase.ORDERED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.ORDERED)

        order = self.shop.order
        assert order is not None  # the gate above guarantees it
        payload: dict[str, Any] = {
            "order_id": order.order_id,
            "items": [item.model_dump(mode="json") for item in order.items],
            "customer_line": order.customer_line,
        }
        # Once there is a ticket, the total belongs here too: it is what Marco
        # has to pay, and the one number he must not reconstruct from memory.
        if self.shop.ticket is not None:
            payload["total"] = self.shop.ticket.total
            payload["notes"] = list(self.shop.ticket.notes)
        if self.shop.served is not None:
            payload["served"] = [line.model_dump(mode="json")
                                 for line in self.shop.served.lines]
        self._returned(tool, order_id=order.order_id,
                       items=[item.describe() for item in order.items],
                       total=payload.get("total"))
        return _text(payload)

    async def get_ticket(self, args: dict[str, Any]) -> dict:
        tool = "get_ticket"
        self._announce(tool, args)
        try:
            self.shop.require_phase_at_least(tool, Phase.PRICED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.PRICED)

        order, ticket = self.shop.order, self.shop.ticket
        assert order is not None and ticket is not None
        self._returned(tool, order_id=order.order_id,
                       items=[item.describe() for item in order.items],
                       total=ticket.total)
        return _text({
            "order_id": order.order_id,
            "items": [item.model_dump(mode="json") for item in order.items],
            "lines": [line.model_dump(mode="json") for line in ticket.lines],
            "total": ticket.total,
            "notes": list(ticket.notes),
        })

    # ---- step 1: marco orders ------------------------------------------

    async def place_order(self, args: dict[str, Any]) -> dict:
        tool = "place_order"
        self._announce(tool, args)
        try:
            self.shop.require_phase(tool, Phase.DOOR)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.DOOR)

        try:
            items = [OrderItem.model_validate(raw) for raw in args.get("items") or []]
        except ValidationError as exc:
            return self._sensor_failed(tool, [_pydantic_line(exc)])

        if errors := sensors.check_place_order(self.shop.menu, items):
            return self._sensor_failed(tool, errors)
        self._sensor_passed(tool)

        from_phase = self.shop.phase
        order = self.shop.place_order(
            items=items, customer_line=args.get("customer_line", ""))

        # Advisory, after the order exists: a removal that changes nothing is
        # something the counter mentions, not a reason to refuse a customer.
        if notes := sensors.pointless_removals(self.shop.menu, items):
            self.bus.emit("order_note", agent=self._agent, notes=notes)

        self.bus.emit("order_placed", agent=self._agent,
                      **order.model_dump(mode="json"))
        self._changed(from_phase)
        self._returned(tool, order_id=order.order_id,
                       items=[i.describe() for i in order.items])
        return _text({"order_id": order.order_id,
                      "items": [i.describe() for i in order.items],
                      "notes": notes})

    # ---- step 2: sophia prices it --------------------------------------

    async def ring_up(self, args: dict[str, Any]) -> dict:
        tool = "ring_up"
        self._announce(tool, args)
        try:
            self.shop.require_phase(tool, Phase.ORDERED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.ORDERED)

        order = self.shop.order
        assert order is not None
        try:
            lines = [TicketLine.model_validate(raw) for raw in args.get("lines") or []]
        except ValidationError as exc:
            return self._sensor_failed(tool, [_pydantic_line(exc)])

        subtotal = int(args.get("subtotal", -1))
        tax = int(args.get("tax", -1))
        total = int(args.get("total", -1))

        errors = sensors.check_ring_up(self.shop.menu, order.items, lines,
                                       subtotal, tax, total)
        if errors:
            return self._sensor_failed(tool, errors)
        self._sensor_passed(tool)

        from_phase = self.shop.phase
        ticket = self.shop.ring_up(lines=lines, subtotal=subtotal, tax=tax,
                                   total=total, notes=args.get("notes") or ())
        self.bus.emit("ticket_rung", agent=self._agent,
                      order_id=order.order_id, **ticket.model_dump(mode="json"))
        self._changed(from_phase)
        self._returned(tool, order_id=order.order_id, total=ticket.total,
                       subtotal=ticket.subtotal, tax=ticket.tax)
        return _text({"ticket_for": order.order_id, "total": ticket.total})

    # ---- step 3: chef cooks --------------------------------------------

    async def serve_order(self, args: dict[str, Any]) -> dict:
        tool = "serve_order"
        self._announce(tool, args)
        try:
            self.shop.require_phase(tool, Phase.PRICED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.PRICED)

        order = self.shop.order
        assert order is not None
        try:
            lines = [ServedLine.model_validate(raw) for raw in args.get("lines") or []]
        except ValidationError as exc:
            return self._sensor_failed(tool, [_pydantic_line(exc)])

        if errors := sensors.check_serve(self.shop.menu, order.items, lines):
            return self._sensor_failed(tool, errors)
        self._sensor_passed(tool)

        from_phase = self.shop.phase
        served = self.shop.serve(lines)
        self.bus.emit("order_served", agent=self._agent,
                      order_id=order.order_id, **served.model_dump(mode="json"))
        self._changed(from_phase)
        self._returned(tool, order_id=order.order_id,
                       lines=[f"{line.item}: {', '.join(line.ingredients)}"
                              for line in served.lines])
        return _text({"served": order.order_id,
                      "lines": [line.item for line in served.lines]})

    # ---- step 4: marco collects and pays -------------------------------

    async def pickup_order(self, args: dict[str, Any]) -> dict:
        tool = "pickup_order"
        self._announce(tool, args)
        try:
            self.shop.require_phase(tool, Phase.SERVED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.SERVED)

        reaction = args.get("reaction", "")
        if errors := sensors.check_pickup(reaction):
            return self._sensor_failed(tool, errors)
        self._sensor_passed(tool)

        pickup = self.shop.pick_up(reaction=reaction)
        # No phase change: collecting is not the end of the visit, paying is.
        # Keeping them separate is what makes "walked off without paying" a
        # state the log can show rather than a case nobody modelled.
        self.bus.emit("order_picked_up", agent=self._agent,
                      **pickup.model_dump(mode="json"))
        total = self.shop.ticket.total if self.shop.ticket else 0
        self._returned(tool, amount_due=total)
        return _text({"collected": True, "amount_due": total})

    async def pay(self, args: dict[str, Any]) -> dict:
        tool = "pay"
        self._announce(tool, args)
        try:
            self.shop.require_phase(tool, Phase.SERVED)
        except ShopRefusal as refusal:
            return self._refuse(tool, refusal, Phase.SERVED)

        if self.shop.pickup is None:
            return _error("Not your turn. Collect the order with pickup_order "
                          "before paying for it.")

        ticket = self.shop.ticket
        assert ticket is not None
        amount = int(args.get("amount", -1))
        if errors := sensors.check_payment(amount, ticket.total):
            return self._sensor_failed(tool, errors)
        self._sensor_passed(tool)

        from_phase = self.shop.phase
        payment = self.shop.pay(amount)
        self.bus.emit("order_paid", agent=self._agent,
                      **payment.model_dump(mode="json"))
        self._changed(from_phase)
        self._returned(tool, paid=payment.amount)
        return _text({"paid": payment.amount, "change": 0})


def _pydantic_line(exc: ValidationError) -> str:
    """One readable line from a pydantic failure.

    Models self-correct from a named field far better than from a traceback,
    and pydantic's own repr is neither short nor addressed to a reader.
    """
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "the arguments"
    return f"{where}: {first['msg']}"


# ---- schemas -----------------------------------------------------------

PLACE_ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pizza": {"type": "string"},
                    "quantity": {"type": "integer", "minimum": 1},
                    "extras": {"type": "array", "items": {"type": "string"},
                               "description": "Exact menu topping names to add"},
                    "remove": {"type": "array", "items": {"type": "string"},
                               "description": "Ingredients to leave off"},
                },
                "required": ["pizza"],
            },
        },
        "customer_line": {"type": "string",
                          "description": "What you said out loud, one sentence"},
    },
    "required": ["items", "customer_line"],
}

RING_UP_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "amount": {"type": "integer"},
                },
                "required": ["item", "quantity", "amount"],
            },
        },
        "subtotal": {"type": "integer"},
        "tax": {"type": "integer"},
        "total": {"type": "integer"},
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "Anything the counter should say out loud"},
    },
    "required": ["lines", "subtotal", "tax", "total"],
}

SERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "ingredients": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Menu order, extras last",
                    },
                },
                "required": ["item", "ingredients"],
            },
        },
    },
    "required": ["lines"],
}

PICKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "reaction": {"type": "string", "description": "One sentence, in character"},
    },
    "required": ["reaction"],
}

PAY_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "integer"}},
    "required": ["amount"],
}

# Tool descriptions are the instruction layer closest to the action. Each one
# answers: what it does, who calls it, when, and what an error means.
DESCRIPTIONS = {
    "get_menu": (
        "Read the shop's menu: the pizzas, what each one is made of, their "
        "prices and the extra toppings. It is the only source of prices and "
        "recipes. Call it before ordering, pricing or cooking. If your numbers "
        "later fail validation, read it again before retrying."
    ),
    "place_order": (
        "Marco only. Put your order into the shop and get back its order id. "
        "Use exact menu names. The shop mints the id; never invent one. A "
        "validation error lists what is not on the menu: fix those items and "
        "call again. Works only at the door."
    ),
    "get_order": (
        "Read the order being worked on: items, extras and removals, plus what "
        "the customer said. Once it is priced this also returns the total, and "
        "once it is cooked, what the kitchen actually made. Sophia, Chef and "
        "Marco all call it."
    ),
    "ring_up": (
        "Sophia only. File the priced ticket: one line per item, plus subtotal, "
        "tax and total. Every figure is checked against the menu's pricing "
        "rule. An error names the line and the rule it broke; recompute only "
        "what it names and call again."
    ),
    "get_ticket": (
        "Chef only. Read the kitchen ticket: what was ordered, how much of it, "
        "and anything the counter noted. Pair it with get_menu to build each "
        "pizza. Available once the order has been rung up."
    ),
    "serve_order": (
        "Chef only. Put the finished pizzas on the counter: one entry per "
        "ticket line, each with the ingredients you actually used, in menu "
        "order with extras last. The bench checks every list against the "
        "recipe. An error names the line to fix."
    ),
    "pickup_order": (
        "Marco only. Collect your order from the counter and say what you "
        "think, in one sentence. Call it once the order has been served. It "
        "does not end your visit: you still owe for it."
    ),
    "pay": (
        "Marco only. Hand over the exact total. Read it from get_order rather "
        "than remembering it; the shop checks the amount against the ticket. "
        "Call it after pickup_order. This ends the shift."
    ),
}


def build_shop_server(tools: ShopTools):
    """Wrap the handlers as the in-process MCP server named `shop`.

    Built per run because every handler closes over this shift's Shop and bus.
    """

    def wrap(name: str, schema: dict):
        handler = getattr(tools, name)

        @tool(name, DESCRIPTIONS[name], schema)
        async def _impl(args: dict[str, Any]) -> dict[str, Any]:
            return await handler(args)

        return _impl

    empty = {"type": "object", "properties": {}}

    return create_sdk_mcp_server(
        name="shop",
        tools=[
            wrap("get_menu", empty),
            wrap("place_order", PLACE_ORDER_SCHEMA),
            wrap("get_order", empty),
            wrap("ring_up", RING_UP_SCHEMA),
            wrap("get_ticket", empty),
            wrap("serve_order", SERVE_SCHEMA),
            wrap("pickup_order", PICKUP_SCHEMA),
            wrap("pay", PAY_SCHEMA),
        ],
    )
