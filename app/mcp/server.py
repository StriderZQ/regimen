"""The MCP adapter: the same system, entered through the agent protocol.

An adapter, not a store. Every tool is a thin call into the domain; all the logic lives
in `app/domain`, so Telegram, MCP and the web never drift apart in behaviour.

The split between tools is strict and deliberate:

  * **Reading** — unrestricted.
  * **Registering what happened** (ate, did a set) — written at once: that is not a
    change to the world but a record of something that already occurred.
  * **Changing state** (writing off, throwing out, correcting) happens immediately — but
    only after the owner says so. Web confirmation is gone: it made him confirm his own
    words. The guard comes from elsewhere — nothing is deleted, everything is a movement,
    and the database is copied every 15 minutes.

Behaviour rules are served by a separate tool and versioned: remote MCP supports tool
calls only, so instructions cannot be relied on to travel through the protocol itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import logging
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

from app.core.security import AuthError, Identity, authenticate
from app.db import session_scope
from app.models.base import local_today
from app.models.identity import Scope
from app.models.ops import CallLog

log = logging.getLogger(__name__)

mcp = FastMCP(
    name="regimen",
    instructions=(
        "The owner's pantry, food diary, menus, recipes and gym. "
        "Read the pantry BEFORE talking about food. Write-offs and orders happen only "
        "after the owner says so — propose first, then act on his answer."
    ),
)


def _identity(session) -> Identity:
    """The token arrives as a header. Without it a tool does not run at all.

    `include` is required: by default FastMCP strips the authorisation header so it
    cannot leak into outgoing requests. We need it here and it goes no further —
    without this no client connects, and the answer looks like "no token" even though
    a token was sent.
    """
    headers = get_http_headers(include={"authorization", "x-api-key"})
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    token = raw.removeprefix("Bearer ").strip() or headers.get("x-api-key")
    return authenticate(session, token)


def _record(session, identity, tool: str, args: dict, ok: bool, started: float,
            error: str | None = None) -> None:
    """The trace of a call: who, what, and how it ended.

    Argument values are not written down — only their names and a digest. Pantry and
    diary history is already recoverable from movements; what is needed here is an
    answer to "who called this", not a second copy of the data.
    """
    digest = hashlib.sha256(
        json.dumps(sorted(args.keys()), ensure_ascii=False).encode()
    ).hexdigest()[:64]
    session.add(
        CallLog(
            client_id=identity.client_id if identity else None,
            tool=tool,
            ok=ok,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error[:500] if error else None,
            args_digest=digest,
        )
    )


def _tool(fn):
    """The wrapper: session, authentication, and human words instead of a traceback.

    The tool signature is rebuilt without `session` and `identity`: FastMCP reads it to
    build the schema, and an agent has no way to supply those two.
    """

    def wrapper(*_args, **kwargs) -> Any:
        started = time.monotonic()
        with session_scope() as session:
            try:
                identity = _identity(session)
            except AuthError as exc:
                _record(session, None, fn.__name__, kwargs, False, started, str(exc))
                return {"error": f"No access: {exc}"}
            try:
                result = fn(session, identity, **kwargs)
            except AuthError as exc:
                _record(session, identity, fn.__name__, kwargs, False, started, str(exc))
                return {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 — the agent needs words, not a stack
                log.exception("Tool %s failed", fn.__name__)
                _record(session, identity, fn.__name__, kwargs, False, started,
                        f"{type(exc).__name__}: {exc}")
                return {"error": f"{type(exc).__name__}: {exc}"}
            failed = isinstance(result, dict) and "error" in result
            _record(session, identity, fn.__name__, kwargs, not failed, started,
                    result.get("error") if failed else None)
            return result

    original = inspect.signature(fn)
    visible = [
        param for name, param in original.parameters.items()
        if name not in ("session", "identity")
    ]
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__signature__ = original.replace(parameters=visible)
    wrapper.__annotations__ = {
        name: value for name, value in getattr(fn, "__annotations__", {}).items()
        if name not in ("session", "identity")
    }
    return wrapper


# --- reading -----------------------------------------------------------------------

@mcp.tool
@_tool
def pantry_list(session, identity, place: str | None = None) -> dict:
    """What is in the house: grams remaining, storage places, expiry dates."""
    from app.domain import pantry
    from app.models.pantry import StoragePlace

    lines = pantry.list_stock(
        session, identity.principal_id, place=StoragePlace(place) if place else None
    )
    return {
        "items": [
            {
                "batch_id": line.batch.id,
                "name": line.label,
                "grams": line.grams_left,
                "place": line.batch.place.value,
                "days_left": line.days_left,
                "expiry_known": line.batch.expires_at is not None,
            }
            for line in lines
        ]
    }


@mcp.tool
@_tool
def pantry_expiring(session, identity, days: int = 2) -> dict:
    """What is going bad. This is the first input to any menu."""
    from app.domain import pantry

    return {
        "items": [
            {"batch_id": x.batch.id, "name": x.label, "grams": x.grams_left,
             "days_left": x.days_left}
            for x in pantry.expiring(session, identity.principal_id, within_days=days)
        ]
    }


@mcp.tool
@_tool
def diary_today(session, identity) -> dict:
    """How much was eaten today, with the composition and item ids for corrections."""
    from app.domain import diary

    return diary.day_payload(diary.day_totals(session, identity.principal_id))


@mcp.tool
@_tool
def diary_totals(session, identity, date: str) -> dict:
    """Totals for a given day (YYYY-MM-DD), with composition and item ids."""
    from app.domain import diary

    return diary.day_payload(diary.day_totals(session, identity.principal_id, day=date))


@mcp.tool
@_tool
def menu_propose(session, identity, exclude_batch_ids: list[str] | None = None) -> dict:
    """A menu for the day from what is at home. Writes off nothing: a plan is not a fact."""
    from app.domain import menu

    proposal = menu.propose(
        session, identity.principal_id, exclude_batches=set(exclude_batch_ids or [])
    )
    return {
        "date": proposal.date,
        "carb_day": proposal.carb_day,
        "targets": proposal.targets,
        "totals": proposal.totals,
        "burning": proposal.burning,
        "warnings": proposal.warnings,
        "notes": proposal.notes,
        "meals": [
            {
                "n": meal.n,
                "time": meal.time,
                "notes": meal.notes,
                "dishes": [
                    {"batch_id": d.batch_id, "name": d.label, "grams": d.grams,
                     "role": d.role, "reason": d.reason, "protein_g": d.protein_g,
                     "estimated": d.estimated}
                    for d in meal.dishes
                ],
            }
            for meal in proposal.meals
        ],
    }


@mcp.tool
@_tool
def menu_swap(session, identity, refused_batch_ids: list[str]) -> dict:
    """He refused a dish — propose a menu without it."""
    from app.domain import menu

    proposal = menu.propose(
        session, identity.principal_id, exclude_batches=set(refused_batch_ids)
    )
    return {
        "meals": [
            {"n": m.n, "dishes": [{"name": d.label, "grams": d.grams, "reason": d.reason}
                                  for d in m.dishes]}
            for m in proposal.meals
        ],
        "warnings": proposal.warnings,
    }


@mcp.tool
@_tool
def recipe_find(session, identity, query: str | None = None,
                only_cookable: bool = False) -> dict:
    """The owner's recipes against a query and against stock: what is there, what is not."""
    from app.domain import recipes

    matches = recipes.find(
        session, identity.principal_id, query=query, only_cookable=only_cookable
    )
    return {
        "recipes": [
            {"id": m.recipe.id, "title": m.recipe.title, "cookable": m.cookable,
             "coverage": round(m.score, 2), "scale": m.scale, "limiting": m.limiting,
             "missing": [x.name for x in m.missing], "uncertainty": m.recipe.uncertainty,
             "ask_variant": m.ask_variant, "tags": m.recipe.tags}
            for m in matches
        ]
    }


@mcp.tool
@_tool
def recipe_get(session, identity, recipe_id: str, base_grams: float | None = None) -> dict:
    """The full recipe: ingredients, method, yield, and what is unknown in the maths.

    `base_grams` rescales it: a recipe written per 1 kg of mince, asked for 3 kg, comes
    back with every ingredient multiplied — the owner should never do that arithmetic.
    """
    from app.domain import recipes

    recipe = recipes.get(session, identity.principal_id, recipe_id)
    if recipe is None:
        return {"error": "Recipe not found"}
    return {
        "scaled": recipes.scaled(recipe, base_grams=base_grams) if base_grams else None,
        "id": recipe.id, "title": recipe.title, "method": recipe.method,
        "yield_grams": recipe.yield_grams, "yield_pieces": recipe.yield_pieces,
        "piece_grams": recipe.piece_grams, "uncertainty": recipe.uncertainty,
        "notes": recipe.notes, "source": recipe.origin_note,
        "ingredients": [
            {"name": i.name, "grams": i.grams, "quantity": i.quantity, "unit": i.unit,
             "staple": i.is_staple, "note": i.note}
            for i in recipe.ingredients
        ],
    }


@mcp.tool
@_tool
def recipe_add(
    session, identity, title: str, ingredients: list[dict],
    method: str | None = None, yield_pieces: int | None = None,
    yield_grams: float | None = None, piece_grams: float | None = None,
    tags: list[str] | None = None, uncertainty: str | None = None,
    ask_variant: bool = False, per_100g: dict | None = None,
    per_piece: dict | None = None,
) -> dict:
    """Write a recipe into the owner's book.

    ONLY on his explicit instruction to put it in the recipe book. The cook proposes
    plenty — the book keeps what he cooked and approved.

    Record ingredients PER 1 KG of the base product: 1 kg of mince, 1 kg of chicken,
    350 g of cottage cheese — and everything else relative to it. Mark the base product
    with the base field. Each ingredient: name, grams (or quantity+unit), staple for
    small things like salt.

    What you do not know, do not invent: leave the ingredient without a weight and say so.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import recipes

    try:
        recipe = recipes.add(
            session, identity.principal_id, title=title, ingredients=ingredients,
            method=method, yield_pieces=yield_pieces, yield_grams=yield_grams,
            piece_grams=piece_grams, tags=tags, uncertainty=uncertainty,
            ask_variant=ask_variant, per_100g=per_100g, per_piece=per_piece,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    view = recipes.scaled(recipe)
    return {
        "id": recipe.id,
        "title": recipe.title,
        "version": recipe.version,
        "base": view.get("base"),
        "per_kilo": view,
        "saved": True,
    }


@mcp.tool
@_tool
def recipe_questions(session, identity, recipe_id: str | None = None) -> dict:
    """What the system could not identify itself and wants to ask about once.

    Returns ingredients with no product together with candidates and their macros. The
    answer is recorded through recipe_link_ingredient and never asked again.
    """
    from app.domain import recipes

    targets = (
        [recipes.get(session, identity.principal_id, recipe_id)]
        if recipe_id
        else [m.recipe for m in recipes.find(session, identity.principal_id, limit=50)]
    )
    questions = []
    for recipe in targets:
        if recipe is None:
            continue
        computed = recipes.compute_macros(session, recipe)
        for question in computed.questions:
            questions.append({**question, "recipe": recipe.title})
        for missing in computed.unknown:
            if "no product card" in missing:
                questions.append({"recipe": recipe.title, "ingredient": missing,
                                  "candidates": []})
    return {"questions": questions, "count": len(questions)}


@mcp.tool
@_tool
def recipe_link_ingredient(session, identity, ingredient: str, product_id: str,
                           state: str | None = None) -> dict:
    """The owner's answer: this ingredient is that product. Remembered forever.

    `state` covers a product used in a particular form: skinless, boiled, raw.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import recipes
    from app.models.catalog import CanonicalProduct

    product = session.get(CanonicalProduct, product_id)
    if product is None or product.owner_id != identity.principal_id:
        return {"error": "Product not found"}

    alias = recipes.remember_ingredient(
        session, identity.principal_id, ingredient, product, state=state
    )
    return {
        "ingredient": alias.raw_name, "product": product.name,
        "state": alias.state, "remembered": True,
    }


@mcp.tool
@_tool
def recipe_set_base(session, identity, recipe_id: str, ingredient: str) -> dict:
    """Set the recipe's base product when the wrong one was guessed."""
    identity.require(Scope.WRITE_FACTS)
    from app.domain import recipes

    recipe = recipes.set_base(session, identity.principal_id, recipe_id, ingredient)
    if recipe is None:
        return {"error": "Recipe not found"}
    return {"id": recipe.id, "base": recipes.scaled(recipe).get("base")}


@mcp.tool
@_tool
def gym_today(session, identity, date: str | None = None) -> dict:
    """What the program says for today. The answer carries the snapshot date — name it if old."""
    from app.domain import training

    day = dt.date.fromisoformat(date) if date else local_today()
    plan = training.program_for(session, identity.principal_id, day)
    return {
        "date": day.isoformat(),
        "rest_day": plan is None,
        "plan": None if plan is None else {
            "title": plan.title, "morning": plan.morning, "evening": plan.evening,
            "purpose": plan.purpose, "snapshot_date": plan.snapshot_date,
            "source": plan.source_note,
        },
    }


@mcp.tool
@_tool
def exercise_history(session, identity, exercise: str) -> dict:
    """What weight to start from: the fact of last time. Progression is the owner's call.

    Read `freshness_note`: when Garmin holds later sessions than this history, say so
    instead of presenting a week-old weight as the last time.
    """
    from app.domain import training

    found = training.find_exercise(session, identity.principal_id, exercise)
    if found is None:
        return {"error": f"The exercise '{exercise}' is not in the registry"}
    hist = training.history(session, identity.principal_id, found)
    return {
        "exercise": found.name, "in_program": found.in_program,
        "sessions": hist.sessions_count, "last_date": hist.last_date,
        "last_sets": [str(s) for s in (hist.last_sets or [])],
        "best_weight_kg": hist.best_weight_kg, "start_hint": hist.start_hint,
        "freshness_note": hist.freshness_note,
        "alternatives": [
            a.name for a in training.alternatives(session, identity.principal_id, found)
        ][:5],
    }


@mcp.tool
@_tool
def exercise_how(session, identity, exercise: str) -> dict:
    """How to do it and on what: registry notes plus the gym's equipment."""
    from app.domain import training

    found = training.find_exercise(session, identity.principal_id, exercise)
    gear = training.equipment(session, identity.principal_id, needle=exercise)
    return {
        "exercise": found.name if found else exercise,
        "known": found is not None,
        "notes": found.notes if found else None,
        "category": found.category if found else None,
        "equipment": [
            {"name": e.name, "trains": e.trains, "unverified": e.unverified} for e in gear[:5]
        ],
    }


@mcp.tool
@_tool
def gym_equipment(session, identity, query: str | None = None) -> dict:
    """What the gym has. Consult this BEFORE proposing a substitution."""
    from app.domain import training

    items = training.equipment(session, identity.principal_id, needle=query)
    return {
        "equipment": [
            {"name": e.name, "trains": e.trains, "brand": e.brand or e.kind,
             "unverified": e.unverified, "note": e.note}
            for e in items[:30]
        ]
    }


@mcp.tool
@_tool
def workout_next_week(session, identity, from_date: str | None = None) -> dict:
    """Next week's version of a session, computed from what was actually lifted.

    Weight rises only where the same weight was carried through every set — double
    progression, and the reason travels with each line. Nothing reaches the watch from
    here: show it to the owner and wait for his word.
    """
    import datetime as dt

    from app.domain import planning

    day = dt.date.fromisoformat(from_date) if from_date else local_today()
    plan = planning.propose_from(session, identity.principal_id, day)
    if plan is None:
        return {"error": f"За {day.isoformat()} тренировки с подходами нет"}
    return {
        "for_date": plan.date,
        "title": plan.title,
        "based_on": plan.source_date,
        "exercises": [
            {"name": e.name, "sets": e.sets, "reps": e.reps,
             "weight_kg": e.weight_kg, "reason": e.reason}
            for e in plan.exercises
        ],
        "note": "Предложение. В Garmin не записано — нужно отдельное подтверждение",
    }


@mcp.tool
@_tool
def workout_push_to_garmin(session, identity, from_date: str) -> dict:
    """Write a proposed session into Garmin — ONLY after the owner said to.

    Never call this off your own judgement, and never straight after building the
    proposal: he reads it first and answers. The workout is created and scheduled on the
    date the proposal names.
    """
    identity.require(Scope.WRITE_FACTS)
    import datetime as dt

    from app.domain import planning
    from app.integrations.garmin.sync import connect

    day = dt.date.fromisoformat(from_date)
    plan = planning.propose_from(session, identity.principal_id, day)
    if plan is None:
        return {"error": f"За {day} тренировки с подходами нет"}

    workout, skipped = planning.to_garmin_workout(plan)
    client = connect()
    created = client.upload_strength_workout(workout)
    workout_id = created.get("workoutId") if isinstance(created, dict) else None
    if workout_id:
        client.schedule_workout(workout_id, plan.date)
    return {
        "workout_id": workout_id,
        "scheduled_for": plan.date,
        "exercises": len(plan.exercises) - len(skipped),
        "skipped": skipped,
        "note": "Без ключа Garmin упражнение не записано — назови его владельцу"
                if skipped else None,
    }


@mcp.tool
@_tool
def body_measure(session, identity, measured_on: str, values: dict,
                 note: str | None = None) -> dict:
    """Record a tape measurement session: girths in centimetres, weight in kilograms.

    Keys as the owner names them: вес, шея, грудь, талия, живот, бёдра_таз,
    бицепс_левый, бедро_правое and so on. Record only what he actually measured — a
    line left out is silence, a guessed line is a false trend.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import body

    entry = body.record(session, identity.principal_id, measured_on=measured_on,
                        values=values, note=note)
    derived = body.derive(entry.values)
    return {
        "measured_on": entry.measured_on,
        "lines": len(entry.values),
        "body_fat_pct": derived.body_fat_pct,
        "note": "Процент жира — оценка US Navy по окружностям, ±3-4 пункта",
    }


@mcp.tool
@_tool
def body_trend(session, identity) -> dict:
    """Body measurements over time, with what they imply.

    Weight alone says little on a cut: the tape shows where the change happened. Read
    the trend, and say the body-fat figure is an estimate whenever you quote it.
    """
    from app.domain import body

    return body.trend(session, identity.principal_id)


@mcp.tool
@_tool
def garmin_freshness(session, identity) -> dict:
    """How fresh the Garmin data is. Ask this BEFORE relying on it."""
    from app.integrations.garmin.sync import freshness

    rows = freshness(session, identity.principal_id)
    return {
        "freshness": [f.describe() for f in rows],
        "stale_kinds": [f.kind.value for f in rows if f.stale],
    }


@mcp.tool
@_tool
def garmin_recent(session, identity, kind: str = "activity", limit: int = 5) -> dict:
    """The latest Garmin data of a chosen kind, together with its freshness."""
    from app.integrations.garmin.sync import freshness, latest
    from app.models.garmin import GarminKind

    rows = latest(session, identity.principal_id, GarminKind(kind), limit=limit)
    fresh = next(
        (f for f in freshness(session, identity.principal_id) if f.kind.value == kind), None
    )
    return {
        "freshness": fresh.describe() if fresh else None,
        "stale": fresh.stale if fresh else True,
        "items": [
            {"date": m.local_date, "title": m.title, "value": m.value,
             "duration_min": m.duration_min, "calories": m.calories,
             "avg_hr": m.avg_hr, "details": m.details}
            for m in rows
        ],
    }


@mcp.tool
@_tool
def shop_deficit(session, identity, days: int = 4) -> dict:
    """What is missing against the targets. Check promotions AFTER the list is built."""
    from app.domain.shopping import deficit

    return deficit(session, identity.principal_id, days=days).as_dict()


@mcp.tool
@_tool
def rules_current(session, identity) -> dict:
    """The behaviour rules in force. Served separately: the protocol cannot carry them."""
    from app.domain.rules import system_prompt

    text = system_prompt(session, identity.principal_id)
    return {"rules_md": text, "length": len(text)}


# --- registering what happened ------------------------------------------------------

@mcp.tool
@_tool
def diary_log_meal(session, identity, portions: list[dict],
                   external_place: str | None = None, separate: bool = False) -> dict:
    """Record what was eaten. No confirmation needed: the food is already eaten.

    Food recorded within an hour of the previous meal joins it: one sitting entered in
    several calls is still one meal. Pass separate=True only when the owner really ate
    twice inside the hour.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import diary
    from app.models.diary import Confidence, ExternalKind, MealSource

    items = [
        diary.Portion(
            name=p["name"], grams=float(p.get("grams") or 0),
            batch_id=p.get("batch_id"), recipe_id=p.get("recipe_id"),
            portions=p.get("pieces"),
            kcal=p.get("kcal"), protein_g=p.get("protein_g"), fat_g=p.get("fat_g"),
            carbs_g=p.get("carbs_g"),
            confidence=Confidence.ESTIMATED if p.get("batch_id") is None else Confidence.WEIGHED,
        )
        for p in portions
    ]
    meal = diary.log_meal(
        session, identity.principal_id, portions=items, source=MealSource.DICTATED,
        external=ExternalKind.RESTAURANT if external_place else None,
        external_place=external_place,
        separate=separate,
    )
    return {"meal_id": meal.id, "kcal": meal.kcal, "protein_g": meal.protein_g,
            "fat_g": meal.fat_g, "carbs_g": meal.carbs_g}


@mcp.tool
@_tool
def diary_adjust_meal(session, identity, meal_item_id: str, grams: float) -> dict:
    """Correct an item's weight: "ate 370, not 350". The stock movement follows."""
    identity.require(Scope.WRITE_FACTS)
    from app.domain import diary

    item = diary.adjust_item(
        session, identity.principal_id, item_id=meal_item_id, actual_grams=grams
    )
    return {"meal_item_id": item.id, "grams": item.grams, "kcal": item.kcal,
            "protein_g": item.protein_g}


@mcp.tool
@_tool
def gym_log_set(session, identity, exercise: str, reps: int | None = None,
                weight_kg: float | None = None, duration_s: float | None = None,
                note: str | None = None, add_if_unknown: bool = False,
                category: str | None = None) -> dict:
    """Record a set. A fact, so no confirmation is needed.

    An unknown exercise is refused rather than guessed. If the owner confirms it is a new
    one — a machine the gym added, a name he uses — call again with add_if_unknown=True
    and it joins the registry.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import training

    try:
        entry = training.log_set(
            session, identity.principal_id, exercise_name=exercise, reps=reps,
            weight_kg=weight_kg, duration_s=duration_s, note=note,
        )
    except training.TrainingError as exc:
        return {"error": str(exc)}
    return {"set_id": entry.id, "order": entry.order, "recorded": True}


# --- requires the human --------------------------------------------------------------

@mcp.tool
@_tool
def pantry_correct(session, identity, batch_id: str, actual_grams: float,
                   reason: str) -> dict:
    """The owner weighed it — bring the balance to his number. AT ONCE, no confirmation.

    He is the source of this fact: trimmed the skin, weighed it, said the number. Asking
    him to confirm his own words is pointless, and the pantry shows an untruth the whole
    time.

    This does not edit history: a correcting movement is created and the past stays readable.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import pantry

    line = next(
        (x for x in pantry.list_stock(session, identity.principal_id)
         if x.batch.id == batch_id),
        None,
    )
    if line is None:
        return {"error": "Batch not found or empty"}

    was = line.grams_left
    pantry.correct(
        session, identity.principal_id, batch_id=batch_id,
        actual_grams=actual_grams, reason=reason,
    )
    return {
        "batch_id": batch_id, "name": line.label,
        "was": was, "now": actual_grams, "delta": round(actual_grams - was, 1),
        "recorded": True,
    }


@mcp.tool
@_tool
def pantry_waste(session, identity, batch_id: str, grams: float,
                 reason: str | None = None) -> dict:
    """The owner threw it out — record it at once. His fact, not the agent's decision.

    A write-off you proposed yourself ("it is past its date, shall we?") is his decision
    too: say out loud what you mean to write off, wait for the answer, then call this
    pantry_writeoff.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import pantry

    line = next(
        (x for x in pantry.list_stock(session, identity.principal_id)
         if x.batch.id == batch_id),
        None,
    )
    if line is None:
        return {"error": "Batch not found or empty"}

    pantry.waste(session, identity.principal_id, batch_id=batch_id, grams=grams,
                 reason=reason or "thrown out")
    return {
        "batch_id": batch_id, "name": line.label,
        "was": line.grams_left, "now": round(line.grams_left - grams, 1),
        "recorded": True,
    }


@mcp.tool
@_tool
def pantry_writeoff(session, identity, batch_id: str, grams: float,
                    reason: str | None = None) -> dict:
    """Write off from the pantry. Immediate — but ONLY after the owner says so.

    Never on your own judgement: an expiry date is a reminder, not a verdict. Propose it
    out loud, show what and how much, wait for his yes — then call this.

    If he ate some of it meanwhile, what is there is written off, and that is said.
    """
    identity.require(Scope.WRITE_FACTS)
    from app.domain import pantry

    line = next(
        (x for x in pantry.list_stock(session, identity.principal_id)
         if x.batch.id == batch_id),
        None,
    )
    if line is None:
        return {"error": "Batch not found or empty"}

    available = line.grams_left
    actual = min(float(grams), available)
    move = pantry.waste(
        session, identity.principal_id, batch_id=batch_id, grams=actual,
        reason=reason or "written off on the owner's word",
    )
    return {
        "name": line.label, "written_off": actual,
        "left": round(available - actual, 1), "move_id": move.id,
        "note": None if actual == grams else f"Asked {grams:.0f} g, had {available:.0f} g",
        "recorded": True,
    }


@mcp.tool
@_tool
def receipt_line_resolve(session, identity, line_id: str, store_item_id: str) -> dict:
    """The answer to "what is this receipt line". Remembered forever."""
    identity.require(Scope.WRITE_FACTS)
    from app.integrations.silpo.importer import remember_alias, stock_from_line
    from app.models.catalog import StoreItem
    from app.models.receipts import ReceiptLine

    line = session.get(ReceiptLine, line_id)
    item = session.get(StoreItem, store_item_id)
    if line is None or item is None or line.owner_id != identity.principal_id:
        return {"error": "Line or product not found"}

    remember_alias(session, identity.principal_id, raw_name=line.raw_name, store_item=item)
    batch_id = stock_from_line(session, identity.principal_id, line, item)
    return {
        "line_id": line.id, "remembered": True, "batch_id": batch_id,
        "note": None if batch_id else "Weight not derivable from the receipt — no batch",
    }


@mcp.tool
@_tool
def reminders_list(session, identity) -> dict:
    """Supplements and reminders. Medication carries a neutral label, never a name."""
    from app.domain import reminders

    return {
        "supplements": [
            {"name": s.name, "schedule": s.schedule, "medication": s.is_medication,
             "note": s.note}
            for s in reminders.supplements(session, identity.principal_id)
        ],
        "due_now": [
            {"kind": d.reminder.kind, "text": d.text}
            for d in reminders.due(session, identity.principal_id)
        ],
    }


@mcp.tool
@_tool
def reminder_acknowledge(session, identity, kind: str) -> dict:
    """"Taken" — the reminder goes quiet until next time."""
    identity.require(Scope.WRITE_FACTS)
    from app.domain import reminders

    try:
        item = reminders.acknowledge(session, identity.principal_id, kind)
    except reminders.ReminderError as exc:
        return {"error": str(exc)}
    return {"kind": item.kind, "acknowledged": True}


@mcp.tool
@_tool
def reminder_snooze(session, identity, kind: str, minutes: int = 60) -> dict:
    """Snooze a reminder. Does not switch it off — it simply stays quiet."""
    identity.require(Scope.WRITE_FACTS)
    from app.domain import reminders

    try:
        item = reminders.snooze(session, identity.principal_id, kind, minutes=minutes)
    except reminders.ReminderError as exc:
        return {"error": str(exc)}
    return {"kind": item.kind,
            "snoozed_until": item.snoozed_until.isoformat() if item.snoozed_until else None}


def http_app():
    """The MCP ASGI application, for mounting into FastAPI."""
    return mcp.http_app(path="/")
