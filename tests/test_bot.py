"""Bot tests: the tools, prompt assembly, and the guard against other chats.

The model is never really called: what is tested is the ring around it. What matters is
not what the model writes but that the tools do exactly what they promise — and above
all that nothing gets written off without the owner saying so.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from app.bot.agent import TOOLS, ToolContext, answer, run_tool
from app.domain import pantry
from app.domain.rules import system_prompt
from app.models import (
    BatchSource,
    CanonicalProduct,
    Principal,
    StoragePlace,
)
from app.models.base import local_today, utcnow
from app.models.policies import Policy, PolicyKind


@pytest.fixture
def tvorog(session: Session, owner: Principal) -> CanonicalProduct:
    product = CanonicalProduct(
        owner_id=owner.id, slug="tvorog-5", name="Творог 5%", category="творог",
        kcal_100=121, protein_100=17, fat_100=5, carbs_100=3, is_protein_base=True,
    )
    session.add(product)
    session.flush()
    return product


@pytest.fixture
def batch(session: Session, owner: Principal, tvorog):
    return pantry.add_batch(
        session, owner.id, canonical=tvorog, label="Творог 5% 350 г", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=utcnow() + __import__("datetime").timedelta(days=1),
    )


@pytest.fixture
def ctx(session: Session, identity) -> ToolContext:
    return ToolContext(session=session, identity=identity)


def test_tool_schemas_valid() -> None:
    """The schemas must be acceptable to the API: name, description, object input."""
    for tool in TOOLS:
        assert tool["name"] and tool["description"]
        assert tool["input_schema"]["type"] == "object"
        json.dumps(tool)  # сериализуемость — иначе вызов упадёт в рантайме


def test_pantry_list_shows_batch(ctx: ToolContext, batch) -> None:
    result = run_tool(ctx, "pantry_list", {})
    names = {item["name"] for item in result["items"]}
    assert "Творог 5% 350 г" in names
    item = next(i for i in result["items"] if i["batch_id"] == batch.id)
    assert item["grams"] == 350
    assert item["place"] == "fridge"


def test_pantry_list_filters_by_place(ctx: ToolContext, batch) -> None:
    assert run_tool(ctx, "pantry_list", {"place": "freezer"})["items"] == []
    assert run_tool(ctx, "pantry_list", {"place": "fridge"})["items"]


def test_expiring_finds_burning_item(ctx: ToolContext, batch) -> None:
    result = run_tool(ctx, "pantry_expiring", {"days": 2})
    assert [i["batch_id"] for i in result["items"]] == [batch.id]


def test_log_meal_reduces_stock(ctx: ToolContext, batch) -> None:
    """The owner's main scenario: ate 150 of 350, leaving 200, not zero."""
    run_tool(
        ctx,
        "diary_log_meal",
        {"portions": [{"name": "Творог", "grams": 150, "batch_id": batch.id}]},
    )
    left = next(
        i for i in run_tool(ctx, "pantry_list", {})["items"] if i["batch_id"] == batch.id
    )
    assert left["grams"] == 200


def test_external_meal_does_not_touch_stock(ctx: ToolContext, batch) -> None:
    run_tool(
        ctx,
        "diary_log_meal",
        {
            "portions": [{"name": "Стейк", "grams": 300, "kcal": 600, "protein_g": 60}],
            "external_place": "кафе",
        },
    )
    left = next(
        i for i in run_tool(ctx, "pantry_list", {})["items"] if i["batch_id"] == batch.id
    )
    assert left["grams"] == 350
    assert run_tool(ctx, "diary_today", {})["protein_g"] == 60


def test_writeoff_is_immediate_after_his_word(ctx: ToolContext, session: Session,
                                              batch) -> None:
    """Said "write it off" — written off at once. His own words need no confirming."""
    result = run_tool(ctx, "pantry_writeoff", {"batch_id": batch.id, "grams": 100,
                                               "reason": "просрочено"})

    assert result["recorded"] is True
    assert result["written_off"] == 100
    assert pantry.batch_balance(session, batch.id) == 250


def test_writeoff_unknown_batch_returns_error(ctx: ToolContext) -> None:
    result = run_tool(ctx, "pantry_writeoff", {"batch_id": "нет-такой", "grams": 10})
    assert "error" in result


def test_unknown_tool_is_reported_not_raised(ctx: ToolContext) -> None:
    assert "error" in run_tool(ctx, "выдуманный_инструмент", {})


def test_prompt_carries_norms_from_db(session: Session, owner: Principal) -> None:
    session.add(
        Policy(
            owner_id=owner.id, kind=PolicyKind.MACROS,
            value={"target_protein_g": [160, 170]}, active_from=utcnow(),
        )
    )
    session.flush()
    prompt = system_prompt(session, owner.id)
    assert "Targets for today" in prompt
    assert "160" in prompt
    assert "Variety" in prompt


def test_prompt_ignores_expired_policy(session: Session, owner: Principal) -> None:
    """A target with a closing date must stay out of the prompt — or the agent lives in the past."""
    import datetime as dt

    session.add(
        Policy(
            owner_id=owner.id, kind=PolicyKind.MACROS, value={"target_protein_g": [90, 100]},
            active_from=utcnow() - dt.timedelta(days=10),
            active_to=utcnow() - dt.timedelta(days=1),
        )
    )
    session.flush()
    assert "Targets for today" not in system_prompt(session, owner.id)


def test_prompt_states_agent_cannot_write_off(session: Session, owner: Principal) -> None:
    prompt = system_prompt(session, owner.id)
    # The line runs along who decided: his words are written at once, the agent's
    # proposals wait to be told
    assert "write it down silently and at once" in prompt
    assert "You thought of it — ask first" in prompt
    assert "You hold no medical data" in prompt


class _FakeBlock:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _FakeResponse:
    def __init__(self, content, stop_reason="end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, script) -> None:
        self._script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # A snapshot: the message list lives in the loop and keeps mutating
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._script.pop(0)


def _tool_results(call: dict) -> list[dict]:
    """Tool results handed to the model in this call."""
    out = []
    for message in call["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            out += [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    return out


class _FakeClient:
    def __init__(self, script) -> None:
        self.messages = _FakeMessages(script)


def _patch_anthropic(monkeypatch, script) -> _FakeClient:
    import sys
    import types

    client = _FakeClient(script)
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda **_kwargs: client
    monkeypatch.setitem(sys.modules, "anthropic", module)
    monkeypatch.setattr("app.bot.agent.get_settings", lambda: _settings("test-key"))
    return client


def _settings(key: str | None):
    from app.config import get_settings

    return get_settings().model_copy(update={"anthropic_api_key": key})


def test_answer_runs_tool_then_replies(ctx: ToolContext, monkeypatch, batch) -> None:
    """A full turn: the model asks for stock, gets data, answers in text."""
    script = [
        _FakeResponse(
            [_FakeBlock(type="tool_use", id="t1", name="pantry_list", input={})],
            stop_reason="tool_use",
        ),
        _FakeResponse([_FakeBlock(type="text", text="Есть творог, 350 г.")]),
    ]
    client = _patch_anthropic(monkeypatch, script)

    text, history = answer(ctx, [], "что есть дома?")

    assert text == "Есть творог, 350 г."
    assert len(client.messages.calls) == 2
    assert "Творог" in _tool_results(client.messages.calls[1])[0]["content"]


def test_answer_survives_broken_tool(ctx: ToolContext, monkeypatch) -> None:
    """A tool error comes back to the model as text instead of ending the conversation."""
    script = [
        _FakeResponse(
            [_FakeBlock(type="tool_use", id="t1", name="diary_log_meal", input={})],
            stop_reason="tool_use",
        ),
        _FakeResponse([_FakeBlock(type="text", text="Не понял, что записать.")]),
    ]
    client = _patch_anthropic(monkeypatch, script)

    text, _ = answer(ctx, [], "запиши")

    assert text == "Не понял, что записать."
    assert "error" in _tool_results(client.messages.calls[1])[0]["content"]


def test_answer_stops_after_tool_loop(ctx: ToolContext, monkeypatch) -> None:
    """Looping on tools is cut short instead of burning the key forever."""
    script = [
        _FakeResponse(
            [_FakeBlock(type="tool_use", id=f"t{i}", name="diary_today", input={})],
            stop_reason="tool_use",
        )
        for i in range(6)
    ]
    client = _patch_anthropic(monkeypatch, script)

    text, _ = answer(ctx, [], "думай")

    assert "stopped to avoid looping" in text
    assert len(client.messages.calls) == 6


def test_answer_without_key_says_so(ctx: ToolContext, monkeypatch) -> None:
    monkeypatch.setattr("app.bot.agent.get_settings", lambda: _settings(None))
    text, history = answer(ctx, [], "привет")
    assert "not configured" in text
    assert history == []


def test_menu_tool_returns_plan_without_touching_stock(ctx: ToolContext, batch) -> None:
    """A menu through the bot: the proposal arrives, the pantry stays untouched."""
    result = run_tool(ctx, "menu_propose", {})
    assert result["meals"][0]["n"] == 1
    assert "targets" in result
    assert pantry.batch_balance(ctx.session, batch.id) == 350


def test_menu_tool_honours_refusal(ctx: ToolContext, batch) -> None:
    result = run_tool(ctx, "menu_propose", {"exclude_batch_ids": [batch.id]})
    offered = {d["batch_id"] for meal in result["meals"] for d in meal["dishes"]}
    assert batch.id not in offered


def test_recipe_tools_answer_from_his_own_collection(ctx: ToolContext, session: Session,
                                                     owner: Principal) -> None:
    from app.models.recipes import Recipe

    recipe = Recipe(
        owner_id=owner.id, slug="syrniki", title="Сырники", tags=[], notes=[],
        piece_grams=60, kcal_100=220, protein_100=15,
        uncertainty="Сахар владелец не отмерял — спросить при готовке",
    )
    session.add(recipe)
    session.flush()

    found = run_tool(ctx, "recipe_find", {"query": "сырник"})
    assert found["recipes"][0]["title"] == "Сырники"

    full = run_tool(ctx, "recipe_get", {"recipe_id": recipe.id})
    assert full["piece_grams"] == 60
    assert "Сахар" in full["uncertainty"]

    assert "error" in run_tool(ctx, "recipe_get", {"recipe_id": "нет-такого"})


def test_prompt_carries_cooking_instructions(session: Session, owner: Principal) -> None:
    session.add(
        Policy(
            owner_id=owner.id, kind=PolicyKind.COOKING, active_from=utcnow(),
            value={"instructions_md": "# Culinary Mentor\nNever fabricate a citation."},
        )
    )
    session.flush()
    prompt = system_prompt(session, owner.id)
    assert "Culinary Mentor" in prompt
    assert "what is at home and what is going bad come first" in prompt


def test_gym_tools_read_and_write(ctx: ToolContext, session: Session, owner: Principal) -> None:
    """The gym through the bot: the plan is read, a set is written at once."""

    from app.models.training import Exercise, ProgramDay

    session.add(
        ProgramDay(
            owner_id=owner.id, phase="Фаза 1", weekday=local_today().weekday(),
            title="Зал: Фулбоди A", evening="Зал: Фулбоди A", purpose="Сила",
            blocks=[], snapshot_date="2026-09-07",
        )
    )
    session.add(
        Exercise(owner_id=owner.id, slug="squat", name="Приседания со штангой",
                 garmin_key="BARBELL_BACK_SQUAT", category="SQUAT", in_program=True)
    )
    session.flush()

    today = run_tool(ctx, "gym_today", {})
    assert today["plan"]["title"] == "Зал: Фулбоди A"
    assert today["plan"]["snapshot_date"] == "2026-09-07"
    assert today["rest_day"] is False

    logged = run_tool(ctx, "gym_log_set", {"exercise": "приседания со штангой",
                                           "reps": 10, "weight_kg": 40})
    assert logged["recorded"] is True

    hist = run_tool(ctx, "gym_exercise_history", {"exercise": "Приседания со штангой"})
    assert hist["best_weight_kg"] == 40
    assert "40 kg × 10" in hist["start_hint"]

    assert "error" in run_tool(ctx, "gym_log_set", {"exercise": "жим на луне", "reps": 5})


def test_gym_today_reports_rest_day(ctx: ToolContext) -> None:
    result = run_tool(ctx, "gym_today", {})
    assert result["rest_day"] is True
    assert result["plan"] is None


def test_garmin_tool_always_reports_freshness(ctx: ToolContext, session: Session,
                                              owner: Principal) -> None:
    """Freshness always comes along — even when there is no data at all."""
    result = run_tool(ctx, "garmin_recent", {})
    assert result["metrics"] == []
    assert any("no data at all" in line for line in result["freshness"])


def test_network_logging_cannot_print_the_bot_token(monkeypatch) -> None:
    """The Telegram token sits IN the request URL, and httpx logs URLs at INFO.

    One `docker logs` and the bot key travels wherever that output is pasted.
    """
    import logging

    from app.bot import telegram as telegram_module

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.INFO)

    telegram_module._silence_token_logging()

    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_bot_records_his_weighing_without_a_link(ctx: ToolContext, session: Session,
                                                 batch) -> None:
    """From his phone he says a weight — stock changes at once, with no links to follow."""
    result = run_tool(ctx, "pantry_correct", {
        "batch_id": batch.id, "actual_grams": 217, "reason": "срезал кожу, взвесил"})

    assert result["recorded"] is True
    assert pantry.batch_balance(session, batch.id) == 217


def test_bot_records_what_he_threw_out(ctx: ToolContext, session: Session, batch) -> None:
    run_tool(ctx, "pantry_waste", {"batch_id": batch.id, "grams": 50})
    assert pantry.batch_balance(session, batch.id) == 300


# --- what actually broke in the chat -----------------------------------------------


def test_history_is_never_cut_between_a_tool_call_and_its_result():
    """A blind slice orphaned a tool_result, and the API refused the whole request.

    The owner saw the bot die mid-conversation with BadRequestError and no explanation.
    """
    from app.bot import telegram

    history = [
        {"role": "user", "content": "что на складе"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "pantry_list"}]},
        {"role": "user",
         "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
        {"role": "assistant", "content": "there is chicken"},
    ]

    # A limit that lands exactly between the call and its result
    monkey = telegram.MAX_HISTORY
    try:
        telegram.MAX_HISTORY = 2
        trimmed = telegram._trim(history)
    finally:
        telegram.MAX_HISTORY = monkey

    first = trimmed[0]["content"]
    orphan = isinstance(first, list) and first and first[0].get("type") == "tool_result"
    assert not orphan, "A tool_result must never be the first message: the API rejects it"


def test_the_day_comes_back_with_its_composition_and_weekday(ctx, session, owner, batch):
    """Asked to list a day, the bot used to get sums only — and counted the calendar itself."""
    from app.domain import diary as diary_domain

    diary_domain.log_meal(
        session, owner.id,
        portions=[diary_domain.Portion(name="Творог 5%", grams=200, batch_id=batch.id)],
    )

    result = run_tool(ctx, "diary_today", {})

    assert result["weekday"], "Without a weekday the agent works the calendar out by hand"
    names = [item["name"] for meal in result["detail"] for item in meal["items"]]
    assert "Творог 5%" in names, "Listing what was eaten must not depend on the chat history"


def test_a_recipe_rescales_to_the_meat_he_actually_bought(ctx, session, owner):
    """\"The recipe is per 1 kg, I have 3 kg\" — the multiplication is the system's job."""
    from app.domain import recipes as recipes_domain

    recipe = recipes_domain.add(
        session, owner.id, title="Котлеты",
        ingredients=[
            {"name": "фарш говяжий", "grams": 1000, "base": True},
            {"name": "лук", "grams": 150},
            {"name": "соль", "grams": 18, "staple": True},
        ],
        method="обжарить",
    )

    result = run_tool(ctx, "recipe_get", {"recipe_id": recipe.id, "base_grams": 3000})

    scaled = result["scaled"]
    assert scaled, "Asked for 3 kg, the answer must carry the rescaled version"
    by_name = {i["name"]: i["grams"] for i in scaled["ingredients"]}
    assert by_name["фарш говяжий"] == pytest.approx(3000)
    assert by_name["лук"] == pytest.approx(450), "Everything scales with the base, not just it"
    assert by_name["соль"] == pytest.approx(54), "Salt scales too: three kilos need three times"


def test_the_prompt_forbids_the_agent_from_repairing_anything(session, owner):
    """It is a door: it reports a failure and stops, instead of diagnosing or improvising."""
    prompt = system_prompt(session, owner.id)

    assert "You are a door, not an engineer" in prompt
    assert "The owner repairs the system" in prompt
    assert "reconstructing its answer from the" in prompt, (
        "Inventing a number to cover a broken tool is the failure mode that must be named"
    )


def test_every_declared_tool_is_actually_dispatched(ctx) -> None:
    """A schema without a handler answers \"Unknown tool\" — to the model, not to a test.

    The failure is silent from the code's point of view: the tool exists, the agent calls
    it, and the owner gets a refusal with no explanation.
    """
    for tool in TOOLS:
        name = tool["name"]
        try:
            result = run_tool(ctx, name, {})
        except Exception:  # noqa: BLE001 — missing arguments are fine, an unknown name is not
            continue
        unknown = isinstance(result, dict) and result.get("error", "").startswith(
            "Unknown tool"
        )
        assert not unknown, (
            f"{name} is declared to the model but nothing handles it"
        )


def test_the_prompt_forbids_assuming_a_fact(session, owner):
    """The sharpest form of the no-invention rule, and the one that gets broken.

    Guessing how much was on the plate is forbidden even when the guess is reasonable:
    a missing fact is a question, not a blank to fill in.
    """
    prompt = system_prompt(session, owner.id)

    assert "Do not assume" in prompt
    assert "is a question to him, not a blank" in prompt
    assert "unverified source" in prompt


def test_the_prompt_requires_evidence_and_a_confidence_level(session, owner):
    """A recommendation without a basis and a confidence level is not a recommendation."""
    prompt = system_prompt(session, owner.id)

    assert "Every recommendation stands on evidence" in prompt
    assert "высокая" in prompt and "средняя" in prompt and "низкая" in prompt
    assert "No confidence level means no recommendation" in prompt


def test_his_stated_dislikes_reach_the_prompt(session, owner):
    """"Never offer me milk" is a fact he stated, not a taste to be inferred.

    A preference kept outside the prompt gets forgotten by the next conversation, and he
    has to say it again — which is how a system stops feeling like one agent.
    """
    from app.domain import rules
    from app.models.policies import PolicyKind

    rules.set_policy(
        session, owner.id, kind=PolicyKind.VARIETY,
        value={"не_предлагать": ["молоко пить отдельно"]},
    )

    prompt = system_prompt(session, owner.id)

    assert "не_предлагать" in prompt
    assert "молоко" in prompt
    assert "даже когда горит" in prompt, "A dislike outranks an expiry date"
