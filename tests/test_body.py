"""Body measurements: the tape says what the scale cannot."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.domain import body
from app.models import Principal


def test_body_fat_is_computed_from_the_belly_not_the_waist(session: Session, owner: Principal):
    """Body fat comes from the belly at navel level, never the narrow waist.

    The waist would give 14.5% where the belly gives 21.9% — and a trend line switching
    between the two breaks at the moment of the switch.
    """
    body.record(
        session, owner.id, measured_on="2026-08-24",
        values={"вес": 80, "шея": 40, "талия": 85, "живот": 95},
    )

    trend = body.trend(session, owner.id)
    derived = trend["derived"]["2026-08-24"]

    assert derived["body_fat_pct"] == pytest.approx(21.9, abs=0.2)
    assert derived["fat_mass_kg"] == pytest.approx(17.5, abs=0.3)
    assert derived["lean_mass_kg"] == pytest.approx(62.5, abs=0.3)
    assert derived["belly_minus_waist"] == pytest.approx(10.0, abs=0.1)


def test_the_trend_keeps_every_line_ever_measured(session: Session, owner: Principal):
    """A line measured in only some sessions must not drop out of the trend."""
    body.record(session, owner.id, measured_on="2026-05-10",
                values={"вес": 90, "талия": 95, "бицепс_левый": 33})
    body.record(session, owner.id, measured_on="2026-07-19",
                values={"вес": 86, "талия": 91, "плечи": 120, "бицепс_левый": 33.5})

    trend = body.trend(session, owner.id)

    assert trend["sessions"] == ["2026-05-10", "2026-07-19"], "Oldest first: a trend reads forward"
    assert trend["lines"]["талия"] == [("2026-05-10", 95), ("2026-07-19", 91)]
    assert trend["lines"]["плечи"] == [("2026-07-19", 120)], "A line measured once still counts"


def test_measuring_again_the_same_day_is_a_correction(session: Session, owner: Principal):
    """A tape read twice in one morning does not mean the body changed twice."""
    body.record(session, owner.id, measured_on="2026-09-13", values={"талия": 90, "шея": 40})
    body.record(session, owner.id, measured_on="2026-09-13", values={"талия": 89.5})

    rows = body.history(session, owner.id)

    assert len(rows) == 1
    assert rows[0].values["талия"] == 89.5, "The correction wins"
    assert rows[0].values["шея"] == 40, "Lines not re-measured survive the correction"


def test_body_fat_is_not_invented_without_the_numbers_it_needs(session: Session, owner: Principal):
    """Weight alone cannot give a percentage, and a guess here reads as a measurement."""
    body.record(session, owner.id, measured_on="2026-09-13", values={"вес": 85})

    derived = body.trend(session, owner.id)["derived"]["2026-09-13"]

    assert derived["body_fat_pct"] is None
    assert derived["lean_mass_kg"] is None
