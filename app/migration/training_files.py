"""Importing a training program from files: exercises, set history, the week, equipment.

Every file lives in one directory, and every file except the week is optional:

  * `exercise-history.json` — past sets, for example from a Garmin export;
  * `exercise-registry.md` — exercise names, Garmin keys and categories; a name ending in
    an asterisk is marked unverified;
  * `current-week.md` — the shape of the week: one table row per weekday;
  * `program-composition.json` — what each day is made of: exercises, sets, target
    range, rest, progression;
  * `gym-equipment.md` — the machines available, grouped by brand.

`examples/training/` holds a working sample of each. Running the import twice creates no
duplicates.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.naming import slugify as _slugify
from app.models.identity import Principal
from app.models.training import (
    Equipment,
    Exercise,
    ProgramDay,
    ProgramExercise,
    Progression,
    SessionSource,
    SetLog,
    WorkoutSession,
)

WEEKDAYS = {
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}


@dataclass
class TrainingReport:
    exercises: int = 0
    sessions: int = 0
    sets: int = 0
    program_days: int = 0
    program_exercises: int = 0
    equipment: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "\n".join(
            [
                f"exercises:     {self.exercises}",
                f"sessions:      {self.sessions} ({self.sets} sets)",
                f"program days:  {self.program_days} ({self.program_exercises} exercises)",
                f"equipment:     {self.equipment}",
                f"skipped:       {len(self.skipped)}",
            ]
        )


def _looks_like_key(value: str | None) -> bool:
    """"LEG_PRESS" is the Garmin key; the human name sits beside it."""
    return bool(value) and value.replace("_", "").replace(" ", "").isupper()


def _ensure_exercise(
    session: Session,
    owner: Principal,
    *,
    name: str | None,
    garmin_key: str | None,
    category: str | None,
    report: TrainingReport,
) -> Exercise:
    """An exercise by Garmin key, else by name. With no key, by category."""
    label = name or garmin_key or category or "unknown exercise"
    slug = _slugify(garmin_key or label)

    existing = session.scalar(
        select(Exercise).where(Exercise.owner_id == owner.id, Exercise.slug == slug)
    )
    if existing:
        # History arrives before the registry and leaves the Garmin key in the name
        # ("LEG_PRESS"). The registry knows the human name — that is what replaces it.
        if name and _looks_like_key(existing.name) and not _looks_like_key(name):
            existing.name = name.rstrip(" *")
            existing.name_unverified = name.rstrip().endswith("*")
        return existing

    exercise = Exercise(
        owner_id=owner.id,
        slug=slug,
        name=label.rstrip(" *"),
        garmin_key=garmin_key,
        category=category,
        name_unverified=bool(name and name.rstrip().endswith("*")),
    )
    session.add(exercise)
    session.flush()
    report.exercises += 1
    return exercise


def migrate_registry(
    session: Session, owner: Principal, path: Path, report: TrainingReport
) -> None:
    """The registry table: name, Garmin key, category, whether it is in the program."""
    for line in path.read_text().splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Ключ Garmin" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 8:
            continue
        name, key, category, *_rest = cells
        key = key.strip("`").strip()
        exercise = _ensure_exercise(
            session,
            owner,
            name=name,
            garmin_key=None if key in ("—", "") else key,
            category=category.strip("`") or None,
            report=report,
        )
        exercise.in_program = cells[-1].startswith("да")
        if exercise.in_program and " — " in cells[-1]:
            exercise.notes = cells[-1].split(" — ", 1)[1]
    session.flush()


def migrate_history(
    session: Session, owner: Principal, path: Path, report: TrainingReport
) -> None:
    """Sets from the Garmin export. A repeat import does not duplicate sessions."""
    data = json.loads(path.read_text())
    for raw in data.get("sessions") or []:
        external_id = str(raw.get("activity_id")) if raw.get("activity_id") else None
        if external_id and session.scalar(
            select(WorkoutSession).where(
                WorkoutSession.owner_id == owner.id,
                WorkoutSession.external_id == external_id,
            )
        ):
            continue

        workout = WorkoutSession(
            owner_id=owner.id,
            external_id=external_id,
            source=SessionSource.GARMIN,
            local_date=raw.get("date") or dt.date.today().isoformat(),
            title=raw.get("name"),
            duration_min=raw.get("duration_min"),
        )
        session.add(workout)
        session.flush()
        report.sessions += 1

        order = 0
        for block in raw.get("exercises") or []:
            exercise = _ensure_exercise(
                session,
                owner,
                name=block.get("name"),
                garmin_key=block.get("name"),
                category=block.get("category"),
                report=report,
            )
            for entry in block.get("sets") or []:
                order += 1
                session.add(
                    SetLog(
                        owner_id=owner.id,
                        session_id=workout.id,
                        exercise_id=exercise.id,
                        order=order,
                        reps=entry.get("reps"),
                        weight_kg=entry.get("weight_kg"),
                        duration_s=entry.get("duration_s"),
                    )
                )
                report.sets += 1
    session.flush()


def _clean(cell: str) -> str:
    """Strip Markdown: the database should hold text, not leftover asterisks."""
    return re.sub(r"[*_`]+", "", cell).strip()


def migrate_program(
    session: Session, owner: Principal, path: Path, report: TrainingReport
) -> None:
    """The shape of the week from the active plan. Exercise detail lives in Garmin."""
    text = path.read_text()
    phase = "Фаза 1"
    snapshot = None
    if match := re.search(r"СТАТУС НА (\d{2})\.(\d{2})\.(\d{4})", text):
        day, month, year = match.groups()
        snapshot = f"{year}-{month}-{day}"

    for line in text.splitlines():
        if not line.startswith("| **") or "|" not in line[3:]:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        raw_day = _clean(cells[0]).lower()[:2]
        if raw_day not in WEEKDAYS:
            continue
        weekday = WEEKDAYS[raw_day]
        morning = _clean(cells[1]) or None
        evening = _clean(cells[2]) or None
        purpose = _clean(cells[3]) or None

        existing = session.scalar(
            select(ProgramDay).where(
                ProgramDay.owner_id == owner.id,
                ProgramDay.phase == phase,
                ProgramDay.weekday == weekday,
            )
        )
        if existing:
            continue

        title = " / ".join(x for x in (morning, evening) if x and x != "—") or "Отдых"
        from app.domain.training import kind_from_title
        session.add(
            ProgramDay(
                owner_id=owner.id,
                phase=phase,
                weekday=weekday,
                title=title,
                morning=None if morning == "—" else morning,
                evening=None if evening == "—" else evening,
                purpose=purpose,
                blocks=[],
                kind=kind_from_title(title),
                snapshot_date=snapshot,
                source_note="current-week.md",
            )
        )
        report.program_days += 1
    session.flush()


def migrate_equipment(
    session: Session, owner: Principal, path: Path, report: TrainingReport
) -> None:
    """Gym equipment. What is unverified stays marked rather than being tidied away."""
    brand = None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            brand = line[3:].strip()
            continue
        if not line.startswith("|") or line.startswith("|---") or "Тренажёр" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3 or not cells[0]:
            continue
        name = _clean(cells[0])
        if session.scalar(
            select(Equipment).where(Equipment.owner_id == owner.id, Equipment.name == name)
        ):
            continue
        note = cells[3] if len(cells) > 3 else None
        session.add(
            Equipment(
                owner_id=owner.id,
                name=name,
                kind=brand,
                brand=brand.split(" — ")[-1] if brand and " — " in brand else None,
                code=None if cells[1] in ("—", "") else cells[1],
                trains=cells[2] or None,
                note=note or None,
                unverified=bool(note and "не сверен" in note.lower()),
            )
        )
        report.equipment += 1
    session.flush()


def run(session: Session, owner: Principal, root: Path) -> TrainingReport:
    report = TrainingReport()

    history = root / "exercise-history.json"
    if history.exists():
        migrate_history(session, owner, history, report)
    else:
        report.skipped.append("exercise-history.json not found")

    registry = root / "exercise-registry.md"
    if registry.exists():
        migrate_registry(session, owner, registry, report)

    program = root / "current-week.md"
    if program.exists():
        migrate_program(session, owner, program, report)
    else:
        report.skipped.append("current-week.md not found — no program")

    # Composition runs after the days: it hangs exercises off them.
    composition = root / "program-composition.json"
    if composition.exists():
        migrate_program_composition(session, owner, composition, report)

    gym = root / "gym-equipment.md"
    if gym.exists():
        migrate_equipment(session, owner, gym, report)

    return report


def migrate_program_composition(
    session: Session, owner: Principal, path: Path, report: TrainingReport
) -> None:
    """What each program day is made of: exercises, order, target range, rest.

    Without target ranges the progression rule has nothing to compare against — "the top
    of the range in every set" needs a range — so this file is what makes the nightly
    plan possible.

    An exercise named here but absent from the registry is reported, never invented: a
    guessed Garmin key becomes a different movement on the watch without saying so.
    """

    data = json.loads(path.read_text(encoding="utf-8"))
    for day in data.get("days", []):
        program_day = session.scalar(
            select(ProgramDay).where(
                ProgramDay.owner_id == owner.id,
                ProgramDay.weekday == day["weekday"],
                ProgramDay.is_active.is_(True),
            )
        )
        if program_day is None:
            report.skipped.append(f"program day {day['weekday']}: не найден")
            continue

        for item in day.get("exercises", []):
            exercise = _match_exercise(session, owner, item)
            if exercise is None:
                report.skipped.append(f"{item['title']}: упражнения нет в реестре")
                continue

            # The cue and the machine are durable properties of the exercise, not of the
            # day it appears in, so they live on the exercise and are filled in once.
            exercise.cue = exercise.cue or item.get("cue")
            exercise.equipment_note = exercise.equipment_note or item.get("equipment_note")
            exercise.in_program = True

            existing = session.scalar(
                select(ProgramExercise).where(
                    ProgramExercise.owner_id == owner.id,
                    ProgramExercise.program_day_id == program_day.id,
                    ProgramExercise.exercise_id == exercise.id,
                )
            )
            if existing:
                continue

            session.add(ProgramExercise(
                owner_id=owner.id,
                program_day_id=program_day.id,
                exercise_id=exercise.id,
                order=item["order"],
                sets=item["sets"],
                target_min=item["target_min"],
                target_max=item["target_max"],
                rest_seconds=item["rest_seconds"],
                progression=Progression(item["progression"]),
                seed_value=item.get("seed_value"),
                note=item.get("goal_text"),
            ))
            report.program_exercises += 1
    session.flush()


def _match_exercise(
    session: Session, owner: Principal, item: dict
) -> Exercise | None:
    """Find the exercise a composition entry refers to, exactly.

    Fuzzy search must not be used here. `find_exercise` falls back to a substring match,
    and PLANK is a substring of SIDE_PLANK — which quietly put the side plank into Monday,
    a day that does not contain it. A wrong exercise in the programme is worse than a
    missing one, because nothing reports it.

    Work Garmin has no key for — the vacuum, some holds — is recorded under a category
    with no name at all. Several such placeholder rows can exist; the one that carries the
    set history is the real one, so it is chosen by evidence rather than by name.
    """
    if key := item.get("garmin_key"):
        found = session.scalar(
            select(Exercise).where(
                Exercise.owner_id == owner.id, Exercise.garmin_key == key
            )
        )
        if found:
            return found

    # Exact name, case aside. Exercises logged before their key was known carry no key at
    # all, and the plank is one of them. Equality, never a substring: "планка" must not
    # find "боковая планка".
    for name in (item.get("registry_name"), item.get("title")):
        if not name:
            continue
        found = session.scalar(
            select(Exercise).where(
                Exercise.owner_id == owner.id,
                func.ru_lower(Exercise.name) == name.lower(),
            )
        )
        if found:
            return found

    # Only for entries that have no key at all. An entry that names a key Garmin knows
    # but this registry has not seen yet must be reported, not quietly resolved to some
    # other exercise in the same category — that is how "Обратные выпады с гантелями"
    # would become plain "Lunge" without a word.
    if item.get("garmin_key"):
        return None

    if category := item.get("category"):
        rows = session.scalars(
            select(Exercise).where(
                Exercise.owner_id == owner.id,
                Exercise.category == category,
                Exercise.garmin_key.is_(None),
            )
        ).all()
        if rows:
            def logged(exercise: Exercise) -> int:
                return session.scalar(
                    select(func.count()).select_from(SetLog).where(
                        SetLog.exercise_id == exercise.id
                    )
                ) or 0

            return max(rows, key=logged)
    return None
