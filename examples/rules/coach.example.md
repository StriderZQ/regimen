# Coaching rules — example

Load with: `python -m app.cli policy-set --kind coaching --from-file examples/rules/coach.example.md`
Replace every value below with your own. The assistant reads this text whenever the
conversation turns to training.

## Heart-rate zones

Zones are computed from the **lactate threshold**, not from maximum heart rate. The split
below is only an example — use the zone model you actually follow.

| Zone | % of threshold |
|---|---|
| Z1 | < 85 % |
| Z2 | 85–89 % |
| Z3 | 90–94 % |
| Z4 | 95–99 % |
| Z5 | ≥ 100 % |

My threshold: <fill in after a test> bpm (cycling), <fill in> bpm (running).
If a threshold changes, it changes because I tested again — never estimate it.

## The week

| Day | Session | Purpose |
|---|---|---|
| Mon | Strength A | legs, back, core |
| Tue | Cycling Z2 | aerobic base |
| Thu | Strength B | chest, shoulders, back |
| Fri | Running Z2 | aerobic base |

Goal: <what all of this is for>.

## Progression

Load rises after **two** sessions at the top of the target range in every working set,
by 2–10 % (ACSM position stand on progression models, 2009). The decision is mine:
state the fact, name the source, give the confidence, propose.

## Readiness decides today, not the calendar

Readiness below 30 or sleep under six hours: walking or rest. Do not move the hard
session onto the next day — skip it.
