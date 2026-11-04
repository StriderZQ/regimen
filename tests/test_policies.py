"""How the owner sets his targets: without them the agent cannot name a shortfall."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import cli
from app.domain import rules
from app.models import Principal
from app.models.policies import Policy, PolicyKind


def test_targets_become_part_of_the_prompt(session: Session, owner: Principal):
    """A target set by a human reaches the agent — otherwise there is no point setting it."""
    rules.set_policy(
        session, owner.id, kind=PolicyKind.MACROS,
        value={"daily_calories": [2000, 2200], "target_protein_g": [160, 170]},
    )

    prompt = rules.system_prompt(session, owner.id)

    assert "Targets for today" in prompt
    assert "target_protein_g" in prompt


def test_new_version_closes_the_old_one(session: Session, owner: Principal):
    """Rule history is not rewritten: "why was the target different in spring" stays answerable."""
    first = rules.set_policy(
        session, owner.id, kind=PolicyKind.MACROS,
        value={"target_protein_g": [160, 170]}, rationale="cutting",
    )
    second = rules.set_policy(
        session, owner.id, kind=PolicyKind.MACROS,
        value={"target_protein_g": [140, 150]}, rationale="maintenance",
    )

    assert first.active_to is not None, "The previous target must be closed"
    assert second.active_to is None
    assert rules.system_prompt(session, owner.id).count("140") == 1

    kept = session.scalars(
        select(Policy).where(Policy.owner_id == owner.id, Policy.kind == PolicyKind.MACROS)
    ).all()
    assert len(kept) == 2, "The old version stays in history instead of being overwritten"


def test_range_accepts_both_a_pair_and_a_single_number():
    assert cli._range("160-170") == [160.0, 170.0]
    assert cli._range("160") == 160.0


def test_policy_set_rejects_broken_json(capsys):
    """Broken JSON must not silently write rubbish into the rules."""
    args = type("A", (), {"kind": "meal_pattern", "json": "{not json}", "why": None,
                      "from_file": None})()
    assert cli.cmd_policy_set(args) == 1
    assert "Could not parse the JSON" in capsys.readouterr().out


def test_policy_value_must_be_an_object(capsys):
    args = type("A", (), {"kind": "meal_pattern", "json": json.dumps([1, 2]), "why": None,
                      "from_file": None})()
    assert cli.cmd_policy_set(args) == 1
    assert "a JSON object" in capsys.readouterr().out
