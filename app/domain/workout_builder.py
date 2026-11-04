"""Turning a proposed session into the workout the watch actually runs.

Assembled by code, never by a model — the same reasoning as `reports.py`: the shape must
be identical every week so a glance in the gym finds the number, and a model writing
these would drift, invent a weight on a quiet day, and cost tokens for the privilege.

`planning.py` decides what the numbers are. This module decides what they look like on
the watch. Keeping them apart is why a change to the progression rule cannot silently
alter the layout of a step, and vice versa.

Three things here are not obvious and cost a session each when got wrong:

**Steps are flat, one per set.** The convenience helper in the Garmin library wraps N
sets in a repeat group, which collapses them under a single description — so a comment
per set becomes structurally impossible. The owner reads those comments at the rack, so
the group is not used.

**The weight is written in kilograms, not grams.** The library multiplies by 1000 for
this field; workouts already written by hand carry `weightValue: 8.0` with the unit's
`factor: 1000.0` filled in by Garmin, and the owner trained from them. The factor
describes the unit; it is not a multiplier to apply here. The read side is different —
activity payloads really do report set weights in grams (`garmin/sync.py`).

**The category must come from the exercise, not from its key.** Garmin's taxonomy is its
own: `DUMBBELL_REVERSE_LUNGE` belongs to `LUNGE`. Splitting the key yields `DUMBBELL`,
which Garmin does not know, and an unknown category costs the name and the animation.
"""

from __future__ import annotations

from app.domain.planning import ExercisePlan, SessionPlan

# Garmin truncates a step description; stay clear of the edge rather than find it.
MAX_DESCRIPTION = 490

WARMUP_SECONDS = 360
COOLDOWN_SECONDS = 180
DEFAULT_REST_SECONDS = 90

# Rough, and only used to fill a field Garmin demands — it shows an estimate to the owner
# before he starts, and refuses the workout outright when it is missing.
SECONDS_PER_REP = 3

WARMUP_TEXT = "РАЗМИНКА. Велотренажёр 6 мин, легко."
COOLDOWN_TEXT = "ЗАМИНКА 3 мин. Растяжка квадрицепса, бицепса бедра и груди. Спокойное дыхание."


# Garmin's own marker for a set it could not identify. It arrives through the activity
# import, so it is data rather than a mistake of ours — reported and left out, not raised
# on, because one unidentified set must not cost the whole session.
PLACEHOLDER_CATEGORIES = frozenset({"UNKNOWN"})


class UnknownCategory(ValueError):
    """A category Garmin does not know. Never sent: the step would arrive nameless.

    Raised only for a category we invented — the sign of a bug upstream, such as deriving
    it from the exercise key. Garmin's own placeholders are skipped instead.
    """


def _valid_categories() -> frozenset[str]:
    from garminconnect.exercises import CATEGORIES

    return frozenset(CATEGORIES)


def _first_description(item: ExercisePlan, total: int) -> str:
    """The comment on set 1: what it is, what to aim for, and why this weight today.

    The reason travels onto the watch deliberately. "45 кг × 12 дважды подряд" answers
    the question he would otherwise ask the assistant from the gym floor, and it is the
    only place the progression is visible at the moment it matters.
    """
    parts = [f"{item.name.upper()} — 1/{total}."]
    if item.duration_s:
        parts.append(f"Удержание {item.duration_s:g} сек.")
    elif item.weight_kg:
        parts.append(f"{item.weight_kg:g} кг × {item.reps}.")
    else:
        parts.append(f"Свой вес, цель {item.reps}.")
    parts.append(item.reason.capitalize() + ".")
    if item.source:
        parts.append(f"Основание: {item.source}.")
    return " ".join(parts)[:MAX_DESCRIPTION]


def _later_description(item: ExercisePlan, k: int, total: int) -> str:
    if item.duration_s:
        target = f"{item.duration_s:g} сек"
    elif item.weight_kg:
        target = f"{item.weight_kg:g} кг × {item.reps}"
    else:
        target = f"цель {item.reps}"
    return f"{item.name.upper()} — {k}/{total}. {target}."[:MAX_DESCRIPTION]


def _step(order: int, kind: str, description: str, *, seconds: int | None = None,
          reps: int | None = None, category: str | None = None,
          exercise_name: str | None = None, weight_kg: float | None = None):
    from garminconnect.workout import ExecutableStep

    if seconds is not None:
        end_condition = {"conditionTypeId": 2, "conditionTypeKey": "time"}
        end_value: float = float(seconds)
    else:
        end_condition = {"conditionTypeId": 10, "conditionTypeKey": "reps"}
        end_value = float(reps or 0)

    extra: dict = {"description": description}
    if category:
        extra["category"] = category
    # An exercise with no Garmin key is still written: a category-only step is legal and
    # shows the category name. Dropping it would silently shorten the session instead.
    extra["exerciseName"] = exercise_name or ""
    if weight_kg:
        extra["weightValue"] = float(weight_kg)
        extra["weightUnit"] = {"unitKey": "kilogram"}

    return ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": {"warmup": 1, "cooldown": 2, "interval": 3, "rest": 5}[kind],
                  "stepTypeKey": kind},
        endCondition=end_condition,
        endConditionValue=end_value,
        targetType={"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
        **extra,
    )


def build(plan: SessionPlan, *, name: str | None = None):
    """A full strength workout: warm-up, every set as its own step, cool-down.

    Returns the workout and the list of exercises that could not be written, which is
    now only those with neither a key nor a category — Garmin cannot show a step that
    names nothing at all.
    """
    from garminconnect.workout import StrengthWorkout, WorkoutSegment

    known = _valid_categories()
    steps: list = []
    skipped: list[str] = []
    order = 1
    duration = WARMUP_SECONDS + COOLDOWN_SECONDS

    steps.append(_step(order, "warmup", WARMUP_TEXT, seconds=WARMUP_SECONDS,
                       category="CARDIO"))
    order += 1

    def is_writable(item: ExercisePlan) -> bool:
        if item.category in PLACEHOLDER_CATEGORIES:
            return False
        return bool(item.garmin_key or item.category)

    writable = [i for i in plan.exercises if is_writable(i)]
    skipped.extend(i.name for i in plan.exercises if not is_writable(i))

    for index, item in enumerate(writable):
        if item.category and item.category not in known:
            raise UnknownCategory(
                f"{item.name}: категория {item.category!r} неизвестна Garmin. "
                "Исправить в реестре упражнений, а не угадывать здесь."
            )
        last = index == len(writable) - 1
        total = max(1, item.sets)
        # Per-exercise rest arrives with the program template; until then one value for
        # all of them, and it is stated here rather than hidden in the call below.
        rest = DEFAULT_REST_SECONDS
        for k in range(1, total + 1):
            text = _first_description(item, total) if k == 1 else _later_description(item, k, total)
            if item.duration_s:
                steps.append(_step(order, "interval", text, seconds=int(item.duration_s),
                                   category=item.category, exercise_name=item.garmin_key))
                duration += int(item.duration_s)
            else:
                steps.append(_step(order, "interval", text, reps=item.reps,
                                   category=item.category, exercise_name=item.garmin_key,
                                   weight_kg=item.weight_kg))
                duration += item.reps * SECONDS_PER_REP
            order += 1
            if k < total:
                steps.append(_step(order, "rest", "Отдых. Поправь повторы, вес уже стоит."
                                   if item.weight_kg else "Отдых. Поправь повторы.",
                                   seconds=rest))
                order += 1
                duration += rest
        # No rest after the last exercise: the cool-down follows immediately and is the
        # end of the session. A rest step there reads as "something else is coming".
        if not last:
            steps.append(_step(order, "rest",
                               f"Отдых. Далее: {writable[index + 1].name}.",
                               seconds=rest))
            order += 1
            duration += rest

    steps.append(_step(order, "cooldown", COOLDOWN_TEXT, seconds=COOLDOWN_SECONDS))

    sport = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
    workout = StrengthWorkout(
        workoutName=name or plan.title,
        description=f"Собрано автоматически на {plan.date}. Веса уже в шагах — набирать не надо.",
        estimatedDurationInSecs=duration,
        workoutSegments=[WorkoutSegment(segmentOrder=1, sportType=sport, workoutSteps=steps)],
    )
    return workout, skipped


class NoBuilder(NotImplementedError):
    """This kind of session cannot be written to the watch yet. Named, never guessed at."""


# Keyed by `ProgramDay.kind`. Cycling, running and the stair machine belong here next;
# the Garmin library already models them (`CyclingWorkout`, `RunningWorkout`,
# `FitnessEquipmentWorkout`), and the nightly job needs no change to use them — it asks
# for a builder by kind and reports the kinds it could not build.
BUILDERS = {"strength": build}


def build_for(kind: str, plan: SessionPlan, *, name: str | None = None):
    """The builder for a kind of session, or a refusal that says which kind."""
    builder = BUILDERS.get(kind)
    if builder is None:
        raise NoBuilder(kind)
    return builder(plan, name=name)
