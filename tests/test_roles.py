"""The chain and the cards: shop/ read as data.

These are the tests for the outer harness itself. Everything else in this
project assumes the chain was read correctly; this file is what makes that
assumption safe.
"""

from __future__ import annotations

import pytest

from sliceops.roles import (
    RoleCardError, Step, load_brief, load_chain, load_roles, parse_role_card,
    validate_chain,
)


# ---- the chain ---------------------------------------------------------

def test_the_shipped_chain_is_the_four_step_shift():
    assert [(s.index, s.agent, s.job) for s in load_chain()] == [
        (1, "marco", "order"),
        (2, "sophia", "ring_up"),
        (3, "chef", "cook"),
        (4, "marco", "pickup"),
    ]


def test_the_last_step_hands_to_nobody():
    chain = load_chain()
    assert chain[-1].hands_to is None
    assert all(s.hands_to is not None for s in chain[:-1])


def test_the_shipped_chain_and_cards_agree():
    assert validate_chain(load_chain(), load_roles()) == []


def test_a_chain_table_is_found_among_other_tables(tmp_path):
    """The brief is prose with a table in it, not a config file.

    Markdown around the table must not be able to break the parser, or editing
    the brief becomes something you can only do carefully.
    """
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "# Heading\n\nSome prose.\n\n"
        "| a | b | c |\n|---|---|---|\n| x | y | z |\n\n"
        "## The chain\n\n"
        "| step | agent | job | hands to |\n"
        "|------|-------|-----|----------|\n"
        "| 1    | marco | order | sophia |\n"
        "| 2    | sophia | ring_up | - |\n\n"
        "More prose.\n")
    assert [(s.agent, s.hands_to) for s in load_chain(path)] == [
        ("marco", "sophia"), ("sophia", None)]


def test_a_brief_with_no_chain_table_is_an_error(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# No table here\n\nJust prose.\n")
    with pytest.raises(RoleCardError, match="no chain table"):
        load_chain(path)


# ---- what the validator catches ----------------------------------------

def test_a_broken_handoff_is_caught_before_the_run_starts():
    """The expensive failure this prevents: a chain that pays for two steps
    and then discovers step three goes to nobody."""
    roles = load_roles()
    broken = (Step(1, "marco", "order", "chef"),
              Step(2, "sophia", "ring_up", None))
    problems = validate_chain(broken, roles)
    assert any("hands to 'chef' but step 2 runs 'sophia'" in p for p in problems)


def test_a_step_naming_an_agent_with_no_card_is_caught():
    problems = validate_chain((Step(1, "dave", "order", None),), load_roles())
    assert any("no card for agent 'dave'" in p for p in problems)


def test_a_step_naming_a_job_the_card_does_not_have_is_caught():
    problems = validate_chain((Step(1, "chef", "ring_up", None),), load_roles())
    assert any("chef has no job 'ring_up'" in p for p in problems)


def test_a_card_the_chain_never_uses_is_caught():
    """An agent nobody calls is almost always a chain edit that was half done."""
    problems = validate_chain((Step(1, "marco", "order", None),), load_roles())
    assert any("sophia is never used" in p for p in problems)
    assert any("chef is never used" in p for p in problems)


def test_a_job_the_chain_never_uses_is_caught():
    """marco's card declares two jobs; a chain that only orders leaves the
    pickup unreachable, and the customer never pays."""
    chain = (Step(1, "marco", "order", "sophia"),
             Step(2, "sophia", "ring_up", "chef"),
             Step(3, "chef", "cook", None))
    problems = validate_chain(chain, load_roles())
    assert any("job 'pickup' is never used" in p for p in problems)


def test_steps_must_be_numbered_in_order():
    chain = (Step(1, "marco", "order", "sophia"), Step(3, "sophia", "ring_up", None))
    assert any("numbered 1..n" in p for p in validate_chain(chain, load_roles()))


# ---- the cards ---------------------------------------------------------

def test_every_card_declares_a_model_a_job_and_its_tools():
    for role in load_roles().values():
        assert role.model in ("haiku", "sonnet", "opus"), role.path.name
        assert role.jobs, role.path.name
        assert role.tools, role.path.name
        assert role.body.strip(), role.path.name


def test_every_tool_a_card_asks_for_is_namespaced_to_the_shop():
    """A card asking for a bare tool name gets silently nothing: the SDK does
    not complain, the tool just never appears, and the agent improvises."""
    for role in load_roles().values():
        for tool in role.tools:
            assert tool.startswith("mcp__shop__"), f"{role.path.name}: {tool}"


def test_a_card_missing_frontmatter_names_the_file(tmp_path):
    path = tmp_path / "nobody.md"
    path.write_text("# Just a body\n")
    with pytest.raises(RoleCardError, match="nobody.md"):
        parse_role_card(path)


def test_a_card_missing_a_required_key_says_which(tmp_path):
    path = tmp_path / "half.md"
    path.write_text("---\nname: half\nmodel: haiku\n---\n\nbody\n")
    with pytest.raises(RoleCardError, match="missing description, tools, jobs"):
        parse_role_card(path)


def test_the_shared_brief_carries_the_chain_every_agent_is_told_about():
    """Each worker is handed the brief, chain table included, so they can see
    which step is theirs and who reads their last line."""
    brief = load_brief()
    assert "| step | agent" in brief
    for agent in ("marco", "sophia", "chef"):
        assert agent in brief
