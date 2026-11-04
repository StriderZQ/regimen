"""The Telegram bot's brain: an Anthropic model with domain access through tools.

The bot does not think by itself — it is a pipe. A message goes to the model, the model
has tools that reach the database, and the answer comes back to the chat. The result is a
conversation with the same agent as in a terminal, entered through another door.

Behaviour rules travel in the system prompt rather than as "server instructions": remote
MCP supports tool calls only, and rules cannot be relied on to travel through the
protocol.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.security import Identity
from app.domain import diary as diary_domain
from app.domain import pantry as pantry_domain
from app.domain.rules import system_prompt
from app.models.base import local_today

log = logging.getLogger(__name__)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "pantry_list",
        "description": (
            "What is at home right now: grams remaining, storage places, expiry dates. "
            "Call this BEFORE any conversation about food."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "enum": ["fridge", "freezer", "pantry"],
                    "description": "Limit to one storage place",
                }
            },
        },
    },
    {
        "name": "pantry_expiring",
        "description": "What is going bad. This is the first input to any menu.",
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "default": 2}},
        },
    },
    {
        "name": "menu_propose",
        "description": (
            "A day's menu from what is at home: what is going bad first, protein that does "
            "not repeat recent days, a full plate (protein + vegetables + fat). "
            "Writes off NOTHING — it is a proposal. Shortfalls arrive in warnings: "
            "say them to the owner out loud rather than hiding them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exclude_batch_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Batches he refused — propose something else",
                }
            },
        },
    },
    {
        "name": "diary_today",
        "description": (
            "The day's totals AND everything eaten, meal by meal, with each item's weight "
            "and macros. Use it to answer \"list what I ate\" — do not reconstruct a day "
            "from the conversation. The answer also carries date and weekday: take them "
            "from here, never work the calendar out yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD; omit for today"},
            },
        },
    },
    {
        "name": "diary_adjust_meal",
        "description": (
            "Correct the weight of an item already logged: \"it was 370, not 350\". Take "
            "meal_item_id from diary_today. The stock movement follows by itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "meal_item_id": {"type": "string"},
                "grams": {"type": "number", "description": "The actual weight eaten"},
            },
            "required": ["meal_item_id", "grams"],
        },
    },
    {
        "name": "menu_swap",
        "description": (
            "He refused a dish — rebuild the menu without it. Pass the batch ids he "
            "turned down."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "refused_batch_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["refused_batch_ids"],
        },
    },
    {
        "name": "shop_deficit",
        "description": (
            "What is missing against the targets, computed from stock — not a habitual "
            "shopping list. Build the list first, look at promotions afterwards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days ahead to cover"},
            },
        },
    },
    {
        "name": "recipe_questions",
        "description": (
            "Ingredients the system could not identify, with candidate products and their "
            "macros. Show them to the owner and ask; answer through recipe_link_ingredient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"recipe_id": {"type": "string", "description": "Omit for all"}},
        },
    },
    {
        "name": "recipe_set_base",
        "description": (
            "Set the recipe's base product when the heaviest ingredient is not it — the "
            "one whose kilogram the whole recipe scales to."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string"},
                "ingredient": {"type": "string"},
            },
            "required": ["recipe_id", "ingredient"],
        },
    },
    {
        "name": "exercise_how",
        "description": (
            "How to perform an exercise and on what: registry notes plus the machines the "
            "gym actually has. Check before proposing a substitution."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"exercise": {"type": "string"}},
            "required": ["exercise"],
        },
    },
    {
        "name": "workout_next_week",
        "description": (
            "Next week's version of a session, computed from what he actually lifted. "
            "Weight rises only where the same weight was held through every set. Show "
            "it to him and wait — this writes nothing to the watch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "YYYY-MM-DD, default today"},
            },
        },
    },
    {
        "name": "workout_push_to_garmin",
        "description": (
            "Write a proposed session into Garmin and schedule it. ONLY after he said "
            "to, never straight after building the proposal and never on your own "
            "judgement."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {
                    "type": "string",
                    "description": "The day the proposal was built from",
                },
            },
            "required": ["from_date"],
        },
    },
    {
        "name": "body_measure",
        "description": (
            "Record a tape measurement session. Keys as he names them: вес, шея, грудь, "
            "талия, живот, бёдра_таз, бицепс_левый, бедро_правое. Record only the lines "
            "he actually measured — a missing line is silence, a guessed one is a lie in "
            "the trend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "measured_on": {"type": "string", "description": "YYYY-MM-DD"},
                "values": {"type": "object", "description": "line → number, cm or kg"},
                "note": {"type": "string"},
            },
            "required": ["measured_on", "values"],
        },
    },
    {
        "name": "body_trend",
        "description": (
            "Body measurements over time with what they imply: body fat, fat and lean "
            "mass, waist-to-height. Weight alone says little on a cut — the tape shows "
            "where the change happened. Always call the body-fat figure an estimate."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "garmin_freshness",
        "description": (
            "How old the Garmin data is, per kind. Ask BEFORE citing sleep, HRV or a "
            "workout: stale data must be called stale, not passed off as today's."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "receipt_line_resolve",
        "description": (
            "The owner's answer to \"what is this receipt line\": link it to a shop "
            "product. Remembered forever, and a batch is created when the weight is known."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "string"},
                "store_item_id": {"type": "string"},
            },
            "required": ["line_id", "store_item_id"],
        },
    },
    {
        "name": "diary_log_meal",
        "description": (
            "Record what was eaten. NO confirmation needed: the food is already eaten. "
            "For pantry items give batch_id — then the balance drops by itself. "
            "For food eaten out give no batch_id, but pass the macros as an estimate. "
            "Food logged within an hour of the previous meal joins it: one sitting entered "
            "in several calls stays one meal. Set separate only if he truly ate twice."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "portions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "grams": {"type": "number"},
                            "batch_id": {"type": "string"},
                            "recipe_id": {
                                "type": "string",
                                "description": (
                                    "Ate something cooked from the recipe book — "
                                    "macros come from there, not from a reference table"
                                ),
                            },
                            "pieces": {
                                "type": "number",
                                "description": "How many pieces, for a per-piece dish",
                            },
                            "kcal": {"type": "number"},
                            "protein_g": {"type": "number"},
                            "fat_g": {"type": "number"},
                            "carbs_g": {"type": "number"},
                        },
                        "required": ["name", "grams"],
                    },
                },
                "external_place": {
                    "type": "string",
                    "description": "If eaten out: cafe, delivery, at friends, travelling",
                },
                "separate": {
                    "type": "boolean",
                    "description": "Force a new meal although the last one was under an hour ago",
                },
            },
            "required": ["portions"],
        },
    },
    {
        "name": "recipe_find",
        "description": (
            "The owner's recipes against a query and against stock. Returns what is "
            "covered, what is missing, and how far the batch scales. These are his own "
            "recipes — do not offer other people's unless he asks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A product, a title or a tag"},
                "only_cookable": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "recipe_add",
        "description": (
            "Write a recipe into the book. ONLY on the owner's explicit instruction "
            "(\"put it in the recipe book\"). Never on your own — the book must stay "
            "what he cooks, not a pile of suggestions. "
            "Ingredients PER 1 KG of the base product; mark the base one with base."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "ingredients": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "grams": {"type": "number"},
                            "quantity": {"type": "number"},
                            "unit": {"type": "string"},
                            "staple": {"type": "boolean"},
                            "base": {"type": "boolean"},
                            "note": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
                "method": {"type": "string", "description": "The cooking steps"},
                "yield_pieces": {"type": "integer"},
                "yield_grams": {"type": "number"},
                "uncertainty": {"type": "string", "description": "What is unknown in the maths"},
                "ask_variant": {
                    "type": "boolean",
                    "description": "The dish is cooked in more than one way — ask which",
                },
                "per_100g": {
                    "type": "object",
                    "description": (
                        "Macros per 100 g of the finished dish, when known: "
                        "kcal, protein_g, fat_g, carbs_g"
                    ),
                },
                "per_piece": {
                    "type": "object",
                    "description": "Macros per piece, when a piece was never weighed",
                },
            },
            "required": ["title", "ingredients"],
        },
    },
    {
        "name": "recipe_link_ingredient",
        "description": (
            "The owner's answer to \"what product is this\": link a name from a recipe "
            "to a catalogue product. Remembered forever, never asked twice. "
            "Ask only when the system failed to identify it — it says so itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ingredient": {"type": "string", "description": "As named in the recipe"},
                "product_id": {"type": "string"},
                "state": {
                    "type": "string",
                    "description": "A particular form: skinless, boiled, raw",
                },
            },
            "required": ["ingredient", "product_id"],
        },
    },
    {
        "name": "recipe_get",
        "description": (
            "The full recipe: batch ingredients, method, yield, known unknowns. Pass "
            "base_grams to rescale it — \"the recipe is per 1 kg, I have 3 kg of mince\" "
            "means base_grams 3000, and every ingredient comes back multiplied."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string"},
                "base_grams": {
                    "type": "number",
                    "description": "Rescale to this weight of the base product, in grams",
                },
            },
            "required": ["recipe_id"],
        },
    },
    {
        "name": "gym_today",
        "description": (
            "What the program says for today and what the recent sessions were. "
            "The program lives in Garmin — this is a copy, and the answer carries the "
            "snapshot date: name it if the copy is old."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
        },
    },
    {
        "name": "gym_exercise_history",
        "description": (
            "What weight to start from: what he lifted last time, his best working set, "
            "what can substitute. Do NOT set progression yourself — show the fact and ask."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"exercise": {"type": "string"}},
            "required": ["exercise"],
        },
    },
    {
        "name": "gym_log_set",
        "description": (
            "Record a set. A fact, no confirmation needed. Give no weight "
            "if the exercise is bodyweight. An unknown exercise is refused, not guessed: "
            "ask the owner whether it is a new one, and only then repeat with "
            "add_if_unknown — that is how a new machine or his own name for a movement "
            "gets into the registry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise": {"type": "string"},
                "reps": {"type": "integer"},
                "weight_kg": {"type": "number"},
                "duration_s": {"type": "number"},
                "note": {"type": "string"},
                "add_if_unknown": {
                    "type": "boolean",
                    "description": "Add this exercise to the registry — only after he confirms",
                },
                "category": {"type": "string", "description": "For a new exercise"},
            },
            "required": ["exercise"],
        },
    },
    {
        "name": "gym_equipment",
        "description": (
            "What the gym has. Check this BEFORE proposing a substitution: "
            "work must never be prescribed on a machine that is not there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "garmin_recent",
        "description": (
            "Sleep, HRV and workouts from Garmin, plus the freshness of the data. "
            "ALWAYS look at freshness: if the data is marked stale, say so out loud "
            "and do not pass the night before last off as last night."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["activity", "sleep", "hrv", "readiness", "body"],
                }
            },
        },
    },
    {
        "name": "reminders_list",
        "description": (
            "Reminders and supplements. Medication arrives as a neutral label with no "
            "name — call it that, and do not guess what the drug is."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reminder_respond",
        "description": (
            "An answer to a reminder: taken, snooze N minutes, switch it off entirely. "
            "Switching off is his right — do not argue and do not talk him out of it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "action": {"type": "string", "enum": ["ack", "snooze", "off", "on"]},
                "minutes": {"type": "integer", "default": 60},
            },
            "required": ["kind", "action"],
        },
    },
    {
        "name": "pantry_correct",
        "description": (
            "The owner weighed it and said the number — bring the balance to it AT ONCE. "
            "No confirmation needed: he is the source of this fact. "
            "Example: \"trimmed the skin, weighed it, it is 217, not 288\"."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
                "actual_grams": {"type": "number"},
                "reason": {"type": "string", "description": "In his words, why it differed"},
            },
            "required": ["batch_id", "actual_grams", "reason"],
        },
    },
    {
        "name": "pantry_waste",
        "description": (
            "The owner threw it out — record it at once. That is his fact. "
            "But \"it is past its date, shall we?\" means ask out loud first, wait for "
            "a yes, then call pantry_writeoff."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
                "grams": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["batch_id", "grams"],
        },
    },
    {
        "name": "pantry_writeoff",
        "description": (
            "Write off from the pantry. Immediate, but ONLY after his yes. "
            "Never on your own: an expiry date is a reminder, not a verdict. "
            "Say what and how much you mean to write off, wait for the answer, then call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
                "grams": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["batch_id", "grams"],
        },
    },
]


@dataclass
class ToolContext:
    session: Session
    identity: Identity


def _menu_as_dict(proposal) -> dict:
    """One shape for a menu, whichever tool asked for it."""
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
                "totals": {
                    "kcal": meal.kcal,
                    "protein_g": meal.protein_g,
                    "fat_g": meal.fat_g,
                    "carbs_g": meal.carbs_g,
                },
                "notes": meal.notes,
                "dishes": [
                    {
                        "batch_id": d.batch_id,
                        "name": d.label,
                        "grams": d.grams,
                        "role": d.role,
                        "reason": d.reason,
                        "protein_g": d.protein_g,
                        "kcal": d.kcal,
                        "estimated": d.estimated,
                    }
                    for d in meal.dishes
                ],
            }
            for meal in proposal.meals
        ],
    }


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Run a tool. Errors come back to the model as text instead of crashing."""
    owner_id = ctx.identity.principal_id

    if name == "pantry_list":
        place = args.get("place")
        from app.models.pantry import StoragePlace

        lines = pantry_domain.list_stock(
            ctx.session, owner_id, place=StoragePlace(place) if place else None
        )
        return {
            "items": [
                {
                    "batch_id": line.batch.id,
                    "name": line.label,
                    "grams": line.grams_left,
                    "portions": line.portions_left,
                    "place": line.batch.place.value,
                    "days_left": line.days_left,
                    "expiry_known": line.batch.expires_at is not None,
                }
                for line in lines
            ]
        }

    if name == "pantry_expiring":
        lines = pantry_domain.expiring(ctx.session, owner_id, within_days=args.get("days", 2))
        return {
            "items": [
                {
                    "batch_id": line.batch.id,
                    "name": line.label,
                    "grams": line.grams_left,
                    "days_left": line.days_left,
                }
                for line in lines
            ]
        }

    if name == "menu_propose":
        from app.domain import menu as menu_domain

        proposal = menu_domain.propose(
            ctx.session,
            owner_id,
            exclude_batches=set(args.get("exclude_batch_ids") or []),
        )
        return _menu_as_dict(proposal)

    if name == "diary_today":
        day = args.get("date")
        totals = diary_domain.day_totals(ctx.session, owner_id, day=day) if day else \
            diary_domain.day_totals(ctx.session, owner_id)
        return diary_domain.day_payload(totals)

    if name == "diary_adjust_meal":
        item = diary_domain.adjust_item(
            ctx.session, owner_id,
            item_id=args["meal_item_id"], actual_grams=float(args["grams"]),
        )
        return {"meal_item_id": item.id, "grams": item.grams, "kcal": item.kcal,
                "protein_g": item.protein_g}

    if name == "menu_swap":
        from app.domain import menu as menu_domain

        proposal = menu_domain.propose(
            ctx.session, owner_id, exclude_batches=set(args.get("refused_batch_ids") or [])
        )
        return _menu_as_dict(proposal)

    if name == "shop_deficit":
        from app.domain.shopping import deficit

        return deficit(ctx.session, owner_id, days=int(args.get("days", 4))).as_dict()

    if name == "recipe_questions":
        from app.domain import recipes as recipes_domain

        recipe_id = args.get("recipe_id")
        targets = (
            [recipes_domain.get(ctx.session, owner_id, recipe_id)]
            if recipe_id
            else [m.recipe for m in recipes_domain.find(ctx.session, owner_id, limit=50)]
        )
        questions = []
        for recipe in targets:
            if recipe is None:
                continue
            computed = recipes_domain.compute_macros(ctx.session, recipe)
            questions.extend(computed.questions)
        return {"questions": questions}

    if name == "recipe_set_base":
        from app.domain import recipes as recipes_domain

        recipe = recipes_domain.set_base(
            ctx.session, owner_id, args["recipe_id"], args["ingredient"]
        )
        if recipe is None:
            return {"error": "Recipe not found"}
        return {"id": recipe.id, "base": recipes_domain.scaled(recipe).get("base")}

    if name == "exercise_how":
        from app.domain import training as training_domain

        found = training_domain.find_exercise(ctx.session, owner_id, args["exercise"])
        gear = training_domain.equipment(ctx.session, owner_id, needle=args["exercise"])
        return {
            "exercise": found.name if found else args["exercise"],
            "known": found is not None,
            "notes": found.notes if found else None,
            "category": found.category if found else None,
            "equipment": [
                {"name": e.name, "trains": e.trains, "unverified": e.unverified}
                for e in gear[:5]
            ],
        }

    if name in ("workout_next_week", "workout_push_to_garmin"):
        import datetime as date_module

        from app.domain import planning

        raw_day = args.get("from_date")
        day = date_module.date.fromisoformat(raw_day) if raw_day else local_today()
        plan = planning.propose_from(ctx.session, owner_id, day)
        if plan is None:
            return {"error": f"За {day.isoformat()} тренировки с подходами нет"}

        if name == "workout_next_week":
            return {
                "for_date": plan.date,
                "title": plan.title,
                "based_on": plan.source_date,
                "exercises": [
                    {"name": e.name, "sets": e.sets, "reps": e.reps,
                     "weight_kg": e.weight_kg, "reason": e.reason}
                    for e in plan.exercises
                ],
                "note": "Предложение. В Garmin не записано",
            }

        from app.integrations.garmin.sync import connect

        workout, skipped = planning.to_garmin_workout(plan)
        client = connect()
        created = client.upload_strength_workout(workout)
        workout_id = created.get("workoutId") if isinstance(created, dict) else None
        if workout_id:
            client.schedule_workout(workout_id, plan.date)
        return {"workout_id": workout_id, "scheduled_for": plan.date,
                "skipped": skipped}

    if name == "body_measure":
        from app.domain import body as body_domain

        entry = body_domain.record(
            ctx.session, owner_id,
            measured_on=args["measured_on"], values=args["values"], note=args.get("note"),
        )
        derived = body_domain.derive(entry.values)
        return {
            "measured_on": entry.measured_on,
            "lines": len(entry.values),
            "body_fat_pct": derived.body_fat_pct,
            "note": "Процент жира — оценка US Navy по окружностям, ±3-4 пункта",
        }

    if name == "body_trend":
        from app.domain import body as body_domain

        return body_domain.trend(ctx.session, owner_id)

    if name == "garmin_freshness":
        from app.integrations.garmin.sync import freshness

        rows = freshness(ctx.session, owner_id)
        return {
            "freshness": [f.describe() for f in rows],
            "stale_kinds": [f.kind.value for f in rows if f.stale],
        }

    if name == "receipt_line_resolve":
        from app.integrations.silpo.importer import remember_alias, stock_from_line
        from app.models.catalog import StoreItem
        from app.models.receipts import ReceiptLine

        line = ctx.session.get(ReceiptLine, args["line_id"])
        item = ctx.session.get(StoreItem, args["store_item_id"])
        if line is None or item is None or line.owner_id != owner_id:
            return {"error": "Line or product not found"}
        remember_alias(ctx.session, owner_id, raw_name=line.raw_name, store_item=item)
        batch_id = stock_from_line(ctx.session, owner_id, line, item)
        return {
            "line": line.raw_name, "product": item.name, "batch_id": batch_id,
            "note": None if batch_id else "Weight not derivable from the receipt — no batch",
        }

    if name == "diary_log_meal":
        from app.models.diary import Confidence, ExternalKind, MealSource

        external = ExternalKind.RESTAURANT if args.get("external_place") else None
        portions = [
            diary_domain.Portion(
                name=p["name"],
                grams=float(p.get("grams") or 0),
                batch_id=p.get("batch_id"),
                recipe_id=p.get("recipe_id"),
                portions=p.get("pieces"),
                kcal=p.get("kcal"),
                protein_g=p.get("protein_g"),
                fat_g=p.get("fat_g"),
                carbs_g=p.get("carbs_g"),
                confidence=Confidence.ESTIMATED if p.get("batch_id") is None
                else Confidence.WEIGHED,
            )
            for p in args["portions"]
        ]
        meal = diary_domain.log_meal(
            ctx.session,
            owner_id,
            portions=portions,
            source=MealSource.DICTATED,
            external=external,
            external_place=args.get("external_place"),
            separate=bool(args.get("separate")),
        )
        return {
            "meal_id": meal.id,
            "kcal": meal.kcal,
            "protein_g": meal.protein_g,
            "fat_g": meal.fat_g,
            "carbs_g": meal.carbs_g,
        }

    # A guard naming only two tools left recipe_add and recipe_link_ingredient
    # unreachable: declared to the model, answered with "Unknown tool".
    if name.startswith("recipe_"):
        from app.domain import recipes as recipes_domain

        if name == "recipe_link_ingredient":
            from app.models.catalog import CanonicalProduct

            product = ctx.session.get(CanonicalProduct, args["product_id"])
            if product is None or product.owner_id != owner_id:
                return {"error": "Product not found"}
            alias = recipes_domain.remember_ingredient(
                ctx.session, owner_id, args["ingredient"], product,
                state=args.get("state"),
            )
            return {"ingredient": alias.raw_name, "product": product.name,
                    "remembered": True}

        if name == "recipe_add":
            try:
                recipe = recipes_domain.add(
                    ctx.session,
                    owner_id,
                    title=args["title"],
                    ingredients=args["ingredients"],
                    method=args.get("method"),
                    yield_pieces=args.get("yield_pieces"),
                    yield_grams=args.get("yield_grams"),
                    uncertainty=args.get("uncertainty"),
                    ask_variant=bool(args.get("ask_variant")),
                    per_100g=args.get("per_100g"),
                    per_piece=args.get("per_piece"),
                )
            except ValueError as exc:
                return {"error": str(exc)}
            return {
                "id": recipe.id,
                "title": recipe.title,
                "version": recipe.version,
                "per_kilo": recipes_domain.scaled(recipe),
                "saved": True,
            }

        if name == "recipe_get":
            recipe = recipes_domain.get(ctx.session, owner_id, args["recipe_id"])
            if recipe is None:
                return {"error": "Recipe not found"}
            return {
                "id": recipe.id,
                "title": recipe.title,
                "method": recipe.method,
                "yield_grams": recipe.yield_grams,
                "yield_pieces": recipe.yield_pieces,
                "piece_grams": recipe.piece_grams,
                "per_100g": {
                    "kcal": recipe.kcal_100,
                    "protein_g": recipe.protein_100,
                    "fat_g": recipe.fat_100,
                    "carbs_g": recipe.carbs_100,
                },
                "uncertainty": recipe.uncertainty,
                "notes": recipe.notes,
                "source": recipe.origin_note,
                "per_kilo": recipes_domain.scaled(recipe),
                # "the recipe is per 1 kg, I have 3 kg of mince" — rescaled here, not by hand
                "scaled": (
                    recipes_domain.scaled(recipe, base_grams=float(args["base_grams"]))
                    if args.get("base_grams")
                    else None
                ),
                "per_piece": recipe.per_piece or None,
                "as_dictated": [
                    {
                        "name": ing.name,
                        "grams": ing.grams,
                        "quantity": ing.quantity,
                        "unit": ing.unit,
                        "staple": ing.is_staple,
                        "base": ing.is_base,
                        "note": ing.note,
                    }
                    for ing in recipe.ingredients
                ],
            }

        matches = recipes_domain.find(
            ctx.session,
            owner_id,
            query=args.get("query"),
            only_cookable=bool(args.get("only_cookable")),
        )
        return {
            "recipes": [
                {
                    "id": m.recipe.id,
                    "title": m.recipe.title,
                    "cookable": m.cookable,
                    "coverage": round(m.score, 2),
                    "scale": m.scale,
                    "limiting": m.limiting,
                    "missing": [
                        {"name": x.name, "grams_needed": x.grams_needed} for x in m.missing
                    ],
                    "piece_grams": m.recipe.piece_grams,
                    "uncertainty": m.recipe.uncertainty,
                }
                for m in matches
            ]
        }

    if name.startswith("gym_"):
        import datetime as date_module

        from app.domain import training as training_domain

        if name == "gym_today":
            day = (
                date_module.date.fromisoformat(args["date"])
                if args.get("date")
                else local_today()
            )
            plan = training_domain.program_for(ctx.session, owner_id, day)
            recent = training_domain.recent_sessions(ctx.session, owner_id, limit=3)
            return {
                "date": day.isoformat(),
                "plan": None
                if plan is None
                else {
                    "title": plan.title,
                    "morning": plan.morning,
                    "evening": plan.evening,
                    "purpose": plan.purpose,
                    "snapshot_date": plan.snapshot_date,
                    "source": plan.source_note,
                },
                "rest_day": plan is None,
                "recent": [
                    {"date": s.local_date, "title": s.title, "sets": len(s.sets)}
                    for s in recent
                ],
            }

        if name == "gym_equipment":
            items = training_domain.equipment(ctx.session, owner_id, needle=args.get("query"))
            return {
                "equipment": [
                    {
                        "name": e.name,
                        "trains": e.trains,
                        "brand": e.brand or e.kind,
                        "unverified": e.unverified,
                        "note": e.note,
                    }
                    for e in items[:25]
                ]
            }

        exercise = training_domain.find_exercise(ctx.session, owner_id, args["exercise"])
        if name == "gym_exercise_history":
            if exercise is None:
                return {"error": f"The exercise '{args['exercise']}' is not in the registry"}
            hist = training_domain.history(ctx.session, owner_id, exercise)
            return {
                "exercise": exercise.name,
                "in_program": exercise.in_program,
                "sessions": hist.sessions_count,
                "last_date": hist.last_date,
                "last_sets": [str(s) for s in (hist.last_sets or [])],
                "best_weight_kg": hist.best_weight_kg,
                "start_hint": hist.start_hint,
                "freshness_note": hist.freshness_note,
                "alternatives": [
                    a.name for a in training_domain.alternatives(ctx.session, owner_id, exercise)
                ][:5],
                "name_unverified": exercise.name_unverified,
            }

        if name == "gym_log_set":
            try:
                entry = training_domain.log_set(
                    ctx.session,
                    owner_id,
                    exercise_name=args["exercise"],
                    reps=args.get("reps"),
                    weight_kg=args.get("weight_kg"),
                    duration_s=args.get("duration_s"),
                    note=args.get("note"),
                    create_if_unknown=bool(args.get("add_if_unknown")),
                    category=args.get("category"),
                )
            except training_domain.TrainingError as exc:
                return {"error": str(exc)}
            return {
                "set_id": entry.id,
                "order": entry.order,
                "session_id": entry.session_id,
                "recorded": True,
            }

    if name == "garmin_recent":
        from app.integrations.garmin.sync import freshness, latest
        from app.models.garmin import GarminKind

        kinds = [GarminKind(args["kind"])] if args.get("kind") else list(GarminKind)
        return {
            "freshness": [f.describe() for f in freshness(ctx.session, owner_id)],
            "metrics": [
                {
                    "kind": m.kind.value,
                    "date": m.local_date,
                    "title": m.title,
                    "value": m.value,
                    "duration_min": m.duration_min,
                    "calories": m.calories,
                    "avg_hr": m.avg_hr,
                    "details": m.details,
                }
                for kind in kinds
                for m in latest(ctx.session, owner_id, kind, limit=3)
            ],
        }

    if name in ("reminders_list", "reminder_respond"):
        from app.domain import reminders as reminders_domain

        if name == "reminders_list":
            items = reminders_domain.supplements(ctx.session, owner_id)
            pending_now = reminders_domain.due(ctx.session, owner_id)
            return {
                "supplements": [
                    {"name": s.name, "schedule": s.schedule,
                     "medication": s.is_medication, "note": s.note}
                    for s in items
                ],
                "due_now": [{"kind": d.reminder.kind, "text": d.text} for d in pending_now],
            }

        try:
            if args["action"] == "ack":
                item = reminders_domain.acknowledge(ctx.session, owner_id, args["kind"])
            elif args["action"] == "snooze":
                item = reminders_domain.snooze(
                    ctx.session, owner_id, args["kind"], minutes=args.get("minutes", 60)
                )
            else:
                item = reminders_domain.set_enabled(
                    ctx.session, owner_id, args["kind"], args["action"] == "on"
                )
        except reminders_domain.ReminderError as exc:
            return {"error": str(exc)}
        return {"kind": item.kind, "enabled": item.enabled,
                "snoozed_until": item.snoozed_until.isoformat() if item.snoozed_until else None}

    if name in ("pantry_correct", "pantry_waste"):
        line = next(
            (
                x
                for x in pantry_domain.list_stock(ctx.session, owner_id)
                if x.batch.id == args["batch_id"]
            ),
            None,
        )
        if line is None:
            return {"error": "Batch not found or empty"}

        if name == "pantry_correct":
            pantry_domain.correct(
                ctx.session, owner_id, batch_id=args["batch_id"],
                actual_grams=float(args["actual_grams"]), reason=args["reason"],
            )
            now = float(args["actual_grams"])
        else:
            pantry_domain.waste(
                ctx.session, owner_id, batch_id=args["batch_id"],
                grams=float(args["grams"]), reason=args.get("reason") or "thrown out",
            )
            now = round(line.grams_left - float(args["grams"]), 1)

        return {"name": line.label, "was": line.grams_left, "now": now, "recorded": True}

    if name == "pantry_writeoff":
        line = next(
            (
                x
                for x in pantry_domain.list_stock(ctx.session, owner_id)
                if x.batch.id == args["batch_id"]
            ),
            None,
        )
        if line is None:
            return {"error": "Batch not found or empty"}
        actual = min(float(args["grams"]), line.grams_left)
        pantry_domain.waste(
            ctx.session, owner_id, batch_id=args["batch_id"], grams=actual,
            reason=args.get("reason") or "written off on the owner's word",
        )
        return {
            "name": line.label, "written_off": actual,
            "left": round(line.grams_left - actual, 1), "recorded": True,
        }

    return {"error": f"Unknown tool {name}"}


def answer(ctx: ToolContext, history: list[dict], user_text: str) -> tuple[str, list[dict]]:
    """One turn of the conversation. Returns the answer and the updated history.

    Network calls happen OUTSIDE the database transaction — otherwise a slow model reply
    would hold SQLite's write lock.
    """
    import anthropic

    settings = get_settings()
    if not settings.anthropic_api_key:
        return ("The Anthropic key is not configured — the bot cannot think.", history)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages = [*history, {"role": "user", "content": user_text}]

    for _ in range(6):  # turn limit: a guard against looping on tools
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1500,
            system=system_prompt(ctx.session, ctx.identity.principal_id),
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            return text or "…", messages

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info("Tool %s(%s)", block.name, json.dumps(block.input, ensure_ascii=False))
            try:
                output = run_tool(ctx, block.name, block.input)
            except Exception as exc:  # noqa: BLE001 — a tool error must not end the talk
                output = {"error": f"{type(exc).__name__}: {exc}"}
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(output, ensure_ascii=False),
                }
            )
        # Close the transaction BEFORE going back to the model: otherwise a write made
        # by a tool would hold the SQLite lock for the whole reply.
        ctx.session.commit()
        messages.append({"role": "user", "content": results})

    return ("Too many steps in a row — stopped to avoid looping.", messages)
