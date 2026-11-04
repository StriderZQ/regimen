"""Tests for the MCP adapter.

What matters here is not that the tools exist but the boundary: **no tool writes off
from the pantry on its own**, however the agent asks. That is a server limit, and it must
hold with no prompt behind it.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy.orm import Session

from app.config import get_settings
from app.domain import pantry
from app.mcp import server as mcp_server
from app.models import (
    BatchSource,
    CanonicalProduct,
    ClientKind,
    Principal,
    Scope,
    StoragePlace,
)
from app.models.base import utcnow


@pytest.fixture
def token(session: Session, owner: Principal) -> str:
    from app.core.security import create_client

    _client, raw = create_client(
        session, principal=owner, name="mcp-claude", kind=ClientKind.CLAUDE,
        scopes={Scope.READ, Scope.WRITE_FACTS},
    )
    session.flush()
    session.commit()
    return raw


@pytest.fixture
def batch(session: Session, owner: Principal):
    product = CanonicalProduct(
        owner_id=owner.id, slug="tvorog", name="Творог 5%", category="творог",
        kcal_100=121, protein_100=16, fat_100=5, carbs_100=3, is_protein_base=True,
    )
    session.add(product)
    session.flush()
    created = pantry.add_batch(
        session, owner.id, canonical=product, label="Творог 5% 350 г", grams=350,
        place=StoragePlace.FRIDGE, source=BatchSource.SILPO,
        expires_at=utcnow() + dt.timedelta(days=1),
    )
    session.commit()
    return created


def _call(tool_name: str, token: str | None = None, **kwargs):
    """Call a tool the way an agent will: through a header carrying the token."""
    headers = {"authorization": f"Bearer {token}"} if token else {}
    tool = getattr(mcp_server, tool_name)
    original = mcp_server.get_http_headers
    # The stub takes the same arguments as the real function: otherwise it stops
    # catching mistakes in how we call it
    mcp_server.get_http_headers = lambda **_kwargs: headers
    try:
        return tool(**kwargs)
    finally:
        mcp_server.get_http_headers = original


def test_every_tool_is_registered():
    import asyncio

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"pantry_list", "menu_propose", "recipe_find", "gym_today",
            "garmin_freshness", "shop_deficit", "pantry_writeoff"} <= names


def test_no_token_is_refused(batch):
    result = _call("pantry_list")
    assert "error" in result and "No access" in result["error"]


def test_bad_token_is_refused(batch):
    result = _call("pantry_list", token="kag_подделка")
    assert "error" in result


def test_reading_works_with_a_token(token, batch):
    result = _call("pantry_list", token=token)
    assert any(item["name"] == "Творог 5% 350 г" for item in result["items"])


def test_stock_tools_exist_and_are_named_plainly(token, batch):
    """Pantry tools are named after what they do: writing off means writing off."""
    import asyncio

    names = {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"pantry_writeoff", "pantry_correct", "pantry_waste"} <= names
    assert "pantry_consume_draft" not in names, "предложений больше нет"
    assert "action_request" not in names


def test_logging_a_meal_is_immediate(session, token, batch):
    """Eaten is a fact: written at once, and stock goes down."""
    result = _call(
        "diary_log_meal", token=token,
        portions=[{"name": "Творог", "grams": 150, "batch_id": batch.id}],
    )
    assert result["protein_g"] > 0
    assert pantry.batch_balance(session, batch.id) == 200


def test_menu_does_not_touch_stock(session, token, batch):
    _call("menu_propose", token=token)
    assert pantry.batch_balance(session, batch.id) == 350


def test_rules_are_served_as_a_tool(token):
    """Rules do not travel through the protocol — a separate tool serves them."""
    result = _call("rules_current", token=token)
    assert result["length"] > 500
    assert "You thought of it — ask first" in result["rules_md"]


def test_garmin_freshness_is_available_without_data(token):
    result = _call("garmin_freshness", token=token)
    assert result["stale_kinds"]


def test_tool_errors_come_back_as_text(token):
    result = _call("recipe_get", token=token, recipe_id="нет-такого")
    assert result == {"error": "Recipe not found"}


def test_results_are_json_serialisable(token, batch):
    """The answer goes to the agent over the protocol: a non-serialisable value breaks it."""
    for tool in ("pantry_list", "menu_propose", "shop_deficit", "gym_today",
                 "garmin_freshness", "diary_today"):
        json.dumps(_call(tool, token=token), ensure_ascii=False)


def test_bearer_header_reaches_the_tools_over_real_http(token, batch):
    """End-to-end over HTTP: FastMCP strips authorisation by default.

    A header stub does not catch that — here the request goes through the real ASGI
    layer, as it would from a live client.
    """
    import json as json_module

    from fastapi.testclient import TestClient

    import app.main as main

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-06-18",
    }

    def rpc(client, method, params=None, notify=False, session_id=None):
        body = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        sent = dict(headers)
        if session_id:
            sent["Mcp-Session-Id"] = session_id
        return client.post("/mcp/", json=body, headers=sent)

    with TestClient(main.app) as client:
        started = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "тест", "version": "1"},
        })
        session_id = started.headers.get("Mcp-Session-Id")
        rpc(client, "notifications/initialized", notify=True, session_id=session_id)

        response = rpc(client, "tools/call",
                       {"name": "pantry_list", "arguments": {}}, session_id=session_id)

    payload = response.text
    for line in payload.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            break
    result = json_module.loads(payload)["result"]
    assert "No token" not in json_module.dumps(result, ensure_ascii=False)
    assert result["structuredContent"]["items"], "the tool must return the pantry"
    assert result["structuredContent"]["items"], "инструмент должен вернуть склад"


def test_every_call_leaves_a_trace(session, token, batch):
    """The call trace answers "who called this", it is not a second copy of the data."""
    from app.models import CallLog

    _call("pantry_list", token=token)
    session.commit()

    entry = session.query(CallLog).order_by(CallLog.created_at.desc()).first()
    assert entry is not None
    assert entry.tool == "pantry_list"
    assert entry.ok is True
    assert entry.duration_ms is not None


def test_refused_call_is_logged_too(session, batch):
    """An attempt with no token matters more than a successful one: someone is knocking."""
    from app.models import CallLog

    _call("pantry_list")
    session.commit()

    entry = session.query(CallLog).order_by(CallLog.created_at.desc()).first()
    assert entry.ok is False
    assert entry.client_id is None
    assert "token" in (entry.error or "").lower()


def test_trace_keeps_names_not_values(session, token, batch):
    """The trace holds field names but not values: it must not become a copy of the data."""
    from app.models import CallLog

    _call("pantry_expiring", token=token, days=2)
    session.commit()

    import hashlib
    import json as json_module

    expected = hashlib.sha256(
        json_module.dumps(["days"], ensure_ascii=False).encode()
    ).hexdigest()[:64]

    entry = session.query(CallLog).order_by(CallLog.created_at.desc()).first()
    assert entry.tool == "pantry_expiring"
    # The digest is built from field NAMES: the same name with a different value gives
    # the same digest — which is how you know the values are not in the log
    assert entry.args_digest == expected


def test_correcting_a_portion_weight_works_end_to_end(session, token, batch):
    """The correction tool was dead on every call: signatures had drifted from the domain.

    The domain expects `item_id` and `actual_grams`; the wrapper passed `meal_item_id`
    positionally and `grams`. No test saw it, because the tool layer had no coverage at
    all.
    """
    logged = _call(
        "diary_log_meal", token=token,
        portions=[{"name": "Творог", "grams": 150, "batch_id": batch.id}],
    )
    assert "error" not in logged

    today = _call("diary_today", token=token)
    item_id = today["detail"][0]["items"][0]["meal_item_id"]

    fixed = _call("diary_adjust_meal", token=token, meal_item_id=item_id, grams=200)

    assert "error" not in fixed, fixed
    assert fixed["grams"] == 200
    assert pantry.batch_balance(session, batch.id) == 150


def test_day_payload_carries_item_ids(token, batch):
    """Without an item id there is nothing to correct — the tool is useless."""
    _call("diary_log_meal", token=token,
          portions=[{"name": "Творог", "grams": 100, "batch_id": batch.id}])

    for tool in ("diary_today", "diary_totals"):
        # The local date, not the system one: CI runs in UTC and the diary counts days
        # in the owner's timezone, so after 21:00 UTC they are different days.
        local_day = utcnow().astimezone(get_settings().tz).date().isoformat()
        payload = _call(tool, token=token,
                        **({} if tool == "diary_today" else {"date": local_day}))
        assert payload["detail"], f"{tool} не отдал состав"
        item = payload["detail"][0]["items"][0]
        assert item["meal_item_id"]
        assert item["name"] and item["grams"] == 100
        assert "macros_known" in item


def test_every_tool_survives_being_called(session, token, batch, owner):
    """A smoke run across the whole tool layer.

    Catches wrapper signatures drifting from the domain — the class of error that slips
    past domain tests and surfaces only in front of a live agent.
    """
    import asyncio
    import inspect

    sample = {
        "place": "fridge", "days": 2, "date": "2026-09-12",
        "exclude_batch_ids": [], "refused_batch_ids": [],
        "query": "творог", "only_cookable": False,
        "recipe_id": "нет-такого", "ingredient": "творог",
        "exercise": "приседания", "kind": "activity", "limit": 3,
        "batch_id": batch.id, "grams": 10,
        "actual_grams": 200.0, "reason": "взвесил",
        "meal_item_id": "нет-такого", "line_id": "нет", "store_item_id": "нет",
        "portions": [{"name": "Тест", "grams": 10}], "product_id": "нет-такого",
        "title": "Проверочное блюдо",
        "ingredients": [{"name": "творог", "grams": 100}],
        "reps": 5, "weight_kg": 10.0, "minutes": 30, "days_back": 1,
        "from_date": "2026-09-11", "measured_on": "2026-09-13",
        "values": {"вес": 85, "живот": 95, "шея": 40},
    }

    tools = asyncio.run(mcp_server.mcp.list_tools())
    broken = []
    for tool in tools:
        fn = getattr(mcp_server, tool.name, None)
        if fn is None:
            continue
        wanted = inspect.signature(fn).parameters
        args = {
            name: sample[name]
            for name in wanted
            if name in sample
            and (wanted[name].default is inspect.Parameter.empty or name in sample)
        }
        missing = [
            name for name, p in wanted.items()
            if p.default is inspect.Parameter.empty and name not in args
        ]
        if missing:
            broken.append(f"{tool.name}: тесту нечем заполнить {missing}")
            continue
        try:
            result = _call(tool.name, token=token, **args)
        except TypeError as exc:
            broken.append(f"{tool.name}: {exc}")
            continue
        if isinstance(result, dict) and "TypeError" in str(result.get("error", "")):
            broken.append(f"{tool.name}: {result['error']}")

    assert not broken, "инструменты с расхождением подписи: " + "; ".join(broken)


def test_he_threw_it_out_is_also_a_fact(session, token, batch):
    result = _call("pantry_waste", token=token, batch_id=batch.id, grams=100,
                   reason="испортилось")
    assert result["recorded"] is True
    assert pantry.batch_balance(session, batch.id) == 250


def test_correction_keeps_the_history_readable(session, token, batch):
    """A correction is a new movement, not an edit: the past stays visible."""
    from app.models import MoveKind, StockMove

    _call("pantry_correct", token=token, batch_id=batch.id, actual_grams=217,
          reason="взвесил")

    moves = session.query(StockMove).filter(StockMove.batch_id == batch.id).all()
    kinds = [m.kind for m in moves]
    assert MoveKind.PURCHASE in kinds, "покупка не стёрта"
    assert MoveKind.CORRECTION in kinds
    assert sum(m.grams for m in moves) == 217


def test_unrecognised_ingredient_becomes_a_question_with_macros(session, token, owner):
    """The system asks once, and shows what there is to choose between."""
    from app.models import CanonicalProduct

    for slug, name, kcal, protein in [
        ("kurica", "Куриное филе", 110, 23), ("yaica", "Яйца куриные", 143, 12.6)
    ]:
        session.add(CanonicalProduct(
            owner_id=owner.id, slug=slug, name=name, category="белок",
            kcal_100=kcal, protein_100=protein))
    # Tools use their own session: an open write would lock the database
    session.commit()

    saved = _call("recipe_add", token=token, title="Паргет",
                  ingredients=[{"name": "куриная грудка", "grams": 350, "base": True}])
    assert saved["saved"] is True

    asked = _call("recipe_questions", token=token)
    assert asked["count"] >= 1
    question = next(q for q in asked["questions"] if "грудка" in q["ingredient"])
    names = {c["name"] for c in question["candidates"]}
    assert {"Куриное филе", "Яйца куриные"} <= names

    chosen = next(c for c in question["candidates"] if c["name"] == "Куриное филе")
    answer = _call("recipe_link_ingredient", token=token,
                   ingredient="куриная грудка", product_id=chosen["product_id"],
                   state="без кожи")
    assert answer["remembered"] is True

    # Never asked again
    again = _call("recipe_questions", token=token)
    assert not any("грудка" in q["ingredient"] for q in again["questions"])


def test_every_mcp_tool_is_reachable_from_the_bot() -> None:
    """Both doors lead to one system — so neither may hold a capability the other lacks.

    The owner asked the bot to list a day and to rescale a recipe; the MCP door could do
    both and the bot could not, because tools were added to one adapter and not the other.
    """
    import re
    from pathlib import Path

    from app.bot.agent import TOOLS

    mcp_tools = set(re.findall(r"@mcp\.tool\n@_tool\ndef (\w+)\(",
                               Path("app/mcp/server.py").read_text()))
    bot_tools = {tool["name"] for tool in TOOLS}

    # Served to the bot by other means rather than as a tool:
    #   rules_current — the rules are already in its system prompt
    #   diary_totals  — diary_today takes a date
    #   exercise_history — present under the bot's own name
    #   reminder_acknowledge / reminder_snooze — one reminder_respond tool covers both
    served_otherwise = {
        "rules_current", "diary_totals", "exercise_history",
        "reminder_acknowledge", "reminder_snooze",
    }

    missing = mcp_tools - bot_tools - served_otherwise
    assert not missing, f"The bot cannot reach: {', '.join(sorted(missing))}"


def test_one_sitting_entered_in_two_calls_stays_one_meal(token, batch) -> None:
    """The croissant came from a cafe and the rest from the pantry — still one lunch."""
    _call("diary_log_meal", token,
          portions=[{"name": "Круасан", "grams": 171, "kcal": 482}],
          external_place="кафе")
    _call("diary_log_meal", token,
          portions=[{"name": "Творог 5%", "grams": 200, "batch_id": batch.id}])

    day = _call("diary_today", token)

    assert day["meals"] == 1, "Two calls a moment apart are one sitting"
    assert len(day["detail"][0]["items"]) == 2
