"""The Shop: the referee that owns every piece of state.

The agents propose; this module disposes. Nothing here trusts an agent: order
ids are minted here, phases advance only through declared transitions, and a
tool that keeps failing is taken away.

Three enforcement layers live in this file, in increasing severity:

1. the phase gate  - wrong turn. Free: costs the caller nothing but a retry.
2. attempts        - wrong content. Three strikes and the tool is gone.
3. the lock        - the run is over for that tool, and the shift has failed.

The gate matters even though the harness, not a model, decides who runs next.
An agent handed the wrong step must be told so by the Shop rather than
improvising around it, and that is the difference between a harness bug that
surfaces in one line of the log and one that surfaces as a plausible-looking
receipt for a pizza nobody made.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .config import RunConfig
from .menu import Menu, OrderItem

MAX_ATTEMPTS = 3
"""Sensor failures a single tool may accumulate before it locks."""

# Order ids are read aloud by agents and pasted between steps, so the alphabet
# excludes characters that are easy to confuse (O/0, I/1).
_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_run_id(rng: random.Random | None = None) -> str:
    """A fresh run id.

    Module-level so the GUI server can name a log file before the shift that
    will fill it exists, and then hand the same id to `run_shift`. Minting one
    throwaway Shop for the id and letting the runner mint a second produced a
    log whose filename and contents disagreed.
    """
    rng = rng or random.Random()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    token = "".join(rng.choice(_ID_ALPHABET) for _ in range(4)).lower()
    return f"{stamp}-{token}"


class Phase(str, Enum):
    """The shift's one-way journey, one phase per link in the chain."""

    DOOR = "DOOR"
    ORDERED = "ORDERED"
    PRICED = "PRICED"
    SERVED = "SERVED"
    PAID = "PAID"
    FAILED = "FAILED"


_MAIN_LINE = (Phase.DOOR, Phase.ORDERED, Phase.PRICED, Phase.SERVED, Phase.PAID)


def phase_at_least(current: Phase, minimum: Phase) -> bool:
    if current is Phase.FAILED:
        return False
    return _MAIN_LINE.index(current) >= _MAIN_LINE.index(minimum)


class ShopRefusal(Exception):
    """The Shop said no.

    `kind` is "gate" or "locked", which decides whether the caller loses an
    attempt. Kept as an exception rather than a return value so a tool can
    never forget to check it.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---- what the shift accumulates ---------------------------------------

class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    items: tuple[OrderItem, ...]
    customer_line: str = ""
    """What the customer actually said, kept verbatim for the receipt and the
    GUI. Never parsed by anything: the items are the order."""


class TicketLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str
    quantity: int
    amount: int


class Ticket(BaseModel):
    model_config = ConfigDict(frozen=True)

    lines: tuple[TicketLine, ...]
    subtotal: int
    tax: int
    total: int
    notes: tuple[str, ...] = ()
    """Anything the counter has to say out loud - what we do not sell, a
    removal that changes nothing. Advisory: never blocks a ticket."""


class ServedLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    item: str
    ingredients: tuple[str, ...]


class Served(BaseModel):
    model_config = ConfigDict(frozen=True)

    lines: tuple[ServedLine, ...]


class Pickup(BaseModel):
    model_config = ConfigDict(frozen=True)

    reaction: str


class Payment(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: int
    """What was handed over. The Shop checks it against the ticket, so this is
    always the correct total by the time it exists."""


@dataclass
class Shop:
    """One shift. Created at the door, discarded when the customer leaves."""

    menu: Menu
    config: RunConfig
    run_id: str = ""
    phase: Phase = Phase.DOOR

    order: Order | None = None
    ticket: Ticket | None = None
    served: Served | None = None
    pickup: Pickup | None = None
    payment: Payment | None = None

    attempts: dict[str, int] = field(default_factory=dict)
    locked: set[str] = field(default_factory=set)

    acting_agent: str | None = None
    """Who the Shop believes is calling right now.

    Set by the runner before each step, because the harness knows exactly whose
    turn it is - it just started them. That is a smaller, more reliable claim
    than inferring it from the message stream.
    """

    rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = new_run_id(self.rng)

    def _token(self, n: int) -> str:
        return "".join(self.rng.choice(_ID_ALPHABET) for _ in range(n))

    # ---- gates ---------------------------------------------------------

    def require_unlocked(self, tool: str) -> None:
        """A locked tool stays dead for the rest of the run."""
        if tool in self.locked:
            raise ShopRefusal(
                "locked",
                f"LOCKED: {tool} failed validation {MAX_ATTEMPTS} times. "
                "Report BLOCKED and stop.",
            )

    def require_phase(self, tool: str, required: Phase) -> None:
        """Exact-phase gate for mutating tools.

        Deliberately does NOT consume an attempt: being handed the wrong step
        is the harness's mistake, not the agent's, and the agent should be free
        to report it and stop.
        """
        self.require_unlocked(tool)
        if self.phase is not required:
            raise ShopRefusal(
                "gate",
                f"Not your turn. The shop is in phase {self.phase.value}. "
                f"{tool} only works in phase {required.value}.",
            )

    def require_phase_at_least(self, tool: str, minimum: Phase) -> None:
        """Read-tool gate: this phase or any later one."""
        self.require_unlocked(tool)
        if not phase_at_least(self.phase, minimum):
            raise ShopRefusal(
                "gate",
                f"Not your turn. The shop is in phase {self.phase.value}. "
                f"{tool} needs phase {minimum.value} or later.",
            )

    # ---- attempts ------------------------------------------------------

    def register_failure(self, tool: str) -> int:
        """Record one sensor failure. Locks the tool on the third.

        Returns the attempt number just used, so the caller can report
        "attempt 2 of 3" to the agent.
        """
        count = self.attempts.get(tool, 0) + 1
        self.attempts[tool] = count
        if count >= MAX_ATTEMPTS:
            self.locked.add(tool)
            self.phase = Phase.FAILED
        return count

    def attempts_left(self, tool: str) -> int:
        return max(0, MAX_ATTEMPTS - self.attempts.get(tool, 0))

    # ---- transitions ---------------------------------------------------

    def place_order(self, items, customer_line: str) -> Order:
        self.order = Order(
            order_id=self._token(5),
            items=tuple(items),
            customer_line=customer_line,
        )
        self.phase = Phase.ORDERED
        return self.order

    def ring_up(self, lines, subtotal: int, tax: int, total: int, notes=()) -> Ticket:
        self.ticket = Ticket(lines=tuple(lines), subtotal=subtotal, tax=tax,
                             total=total, notes=tuple(notes))
        self.phase = Phase.PRICED
        return self.ticket

    def serve(self, lines) -> Served:
        self.served = Served(lines=tuple(lines))
        self.phase = Phase.SERVED
        return self.served

    def pick_up(self, reaction: str) -> Pickup:
        self.pickup = Pickup(reaction=reaction)
        return self.pickup

    def pay(self, amount: int) -> Payment:
        self.payment = Payment(amount=amount)
        self.phase = Phase.PAID
        return self.payment

    # ---- reading -------------------------------------------------------

    @property
    def order_id(self) -> str | None:
        return self.order.order_id if self.order else None

    def next_expected_step(self) -> str:
        """Plain English for whoever reads a refusal."""
        return {
            Phase.DOOR: "marco places the order",
            Phase.ORDERED: "sophia rings the order up",
            Phase.PRICED: "chef cooks and serves the ticket",
            Phase.SERVED: "marco picks the order up and pays",
            Phase.PAID: "the shift is over",
            Phase.FAILED: "nothing: the shift has failed",
        }[self.phase]

    def snapshot(self) -> dict[str, Any]:
        """Everything a renderer needs, as plain JSON-able data."""
        return {
            "phase": self.phase.value,
            "order": self.order.model_dump(mode="json") if self.order else None,
            "ticket": self.ticket.model_dump(mode="json") if self.ticket else None,
            "served": self.served.model_dump(mode="json") if self.served else None,
            "pickup": self.pickup.model_dump(mode="json") if self.pickup else None,
            "payment": self.payment.model_dump(mode="json") if self.payment else None,
            "attempts": dict(self.attempts),
            "locked": sorted(self.locked),
        }
