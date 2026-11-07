"""Reading `shop/` as data: the identity cards, and the chain that orders them.

This module is the outer harness. Anthropic's SDK supplies the inner one - the
agent loop, the tool protocol, the model call - and everything *around* it is a
file in `shop/` that this module parses:

    shop/AGENTS.md              -> the shared brief, and the chain table
    shop/.claude/agents/<id>.md -> one worker's identity: model, tools, brief

Nothing reaches an agent's context that was not read from one of those files by
this module and handed over explicitly. In particular the SDK's own
`setting_sources` discovery is left off, so there is no second, invisible route
by which instructions arrive. If an agent knows something, you can point at the
file it came from.

Frontmatter and the chain table are parsed by hand rather than with PyYAML: the
formats are a handful of `key: value` lines and one pipe table, and the
dependency list stays short. The parser is strict and names the file it could
not read, which is better than silently loading half a card.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AGENTS_DIR, BRIEF_PATH

MODEL_ALIASES = ("haiku", "sonnet", "opus")
"""Frontmatter model aliases the CLI resolves. Verified in Phase 0: a card
saying `model: haiku` really does run on claude-haiku-5-5."""

_CHARS_PER_TOKEN = 4
"""Rough English heuristic. Good enough to compare one instruction layer
against another, which is all the lint report claims to do."""


def estimate_tokens(text: str) -> int:
    """Approximate token count. Deliberately not exact: the point is relative
    size between instruction layers, not billing."""
    return round(len(text) / _CHARS_PER_TOKEN)


class RoleCardError(Exception):
    """A file in shop/ could not be read. Always names the file."""


@dataclass(frozen=True)
class Role:
    """One worker's identity card: `shop/.claude/agents/<id>.md`.

    Kept in Claude Code's own subagent card format - same directory, same
    frontmatter keys - so a card that works here also works as a real subagent
    if you ever point the CLI at this folder. `jobs` is the one addition, and
    an unknown key is ignored by both readers.
    """

    id: str
    description: str
    model: str
    tools: tuple[str, ...]
    jobs: tuple[str, ...]
    body: str
    path: Path

    @property
    def brief_tokens(self) -> int:
        """What only this worker pays to read their own card."""
        return estimate_tokens(self.body)


@dataclass(frozen=True)
class Step:
    """One row of the chain table in `shop/AGENTS.md`."""

    index: int
    agent: str
    job: str
    hands_to: str | None


def _parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise RoleCardError(f"{path.name}: missing frontmatter (no leading ---)")

    _, _, rest = text.partition("---\n")
    frontmatter, sep, body = rest.partition("\n---")
    if not sep:
        raise RoleCardError(f"{path.name}: frontmatter is not closed with ---")

    fields: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        key, colon, value = line.partition(":")
        if not colon:
            raise RoleCardError(f"{path.name}: cannot read frontmatter line {line!r}")
        fields[key.strip()] = value.strip()
    return fields, body.lstrip("-\n")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_role_card(path: Path) -> Role:
    fields, body = _parse_frontmatter(path)

    missing = [
        key for key in ("name", "description", "model", "tools", "jobs")
        if key not in fields
    ]
    if missing:
        raise RoleCardError(f"{path.name}: frontmatter is missing {', '.join(missing)}")

    return Role(
        id=fields["name"],
        description=fields["description"],
        model=fields["model"],
        tools=_csv(fields["tools"]),
        jobs=_csv(fields["jobs"]),
        body=body,
        path=path,
    )


def load_roles(agents_dir: Path = AGENTS_DIR) -> dict[str, Role]:
    """Every identity card in the shop, keyed by agent id."""
    if not agents_dir.is_dir():
        raise RoleCardError(f"no agents directory at {agents_dir}")
    return {
        role.id: role
        for role in (parse_role_card(p) for p in sorted(agents_dir.glob("*.md")))
    }


def load_brief(path: Path = BRIEF_PATH) -> str:
    """`shop/AGENTS.md` in full: the layer every worker pays for.

    Handed to every agent, chain table included. A worker who can see the whole
    chain knows which step is theirs and who reads their last line, which is
    the difference between handing off and guessing.
    """
    return path.read_text(encoding="utf-8")


def load_chain(path: Path = BRIEF_PATH) -> tuple[Step, ...]:
    """The chain table in `shop/AGENTS.md`, as the list of steps to run.

    This is the program. Editing that table is how you change the shift, and no
    agent gets a say in the order - the harness walks these rows.
    """
    rows: list[Step] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        step, agent, job, hands_to = cells
        # Skips the header row and the |---|---| rule in one test, without
        # needing to know which line number either of them is on.
        if not step.isdigit():
            continue
        rows.append(Step(
            index=int(step),
            agent=agent,
            job=job,
            hands_to=None if hands_to in ("-", "", "none") else hands_to,
        ))

    if not rows:
        raise RoleCardError(f"{path.name}: no chain table found")
    return tuple(rows)


def validate_chain(chain: tuple[Step, ...], roles: dict[str, Role]) -> list[str]:
    """Problems that would make a shift fail halfway through, found up front.

    Returned rather than raised so `lint-agents` can print all of them at once;
    the runner refuses to start if this comes back non-empty.
    """
    problems: list[str] = []

    for position, step in enumerate(chain, start=1):
        where = f"chain step {step.index}"
        if step.index != position:
            problems.append(f"{where}: steps must be numbered 1..n in order")

        role = roles.get(step.agent)
        if role is None:
            problems.append(f"{where}: no card for agent {step.agent!r}")
            continue
        if step.job not in role.jobs:
            problems.append(
                f"{where}: {step.agent} has no job {step.job!r} "
                f"(its card declares: {', '.join(role.jobs)})")

    for earlier, later in zip(chain, chain[1:]):
        if earlier.hands_to != later.agent:
            problems.append(
                f"chain step {earlier.index} hands to {earlier.hands_to!r} "
                f"but step {later.index} runs {later.agent!r}")
    if chain[-1].hands_to is not None:
        problems.append(
            f"chain step {chain[-1].index} is last and must hand to '-', "
            f"not {chain[-1].hands_to!r}")

    for agent_id, role in roles.items():
        if not any(step.agent == agent_id for step in chain):
            problems.append(f"{role.path.name}: {agent_id} is never used by the chain")
        for job in role.jobs:
            if not any(step.agent == agent_id and step.job == job for step in chain):
                problems.append(f"{role.path.name}: job {job!r} is never used by the chain")

    return problems
