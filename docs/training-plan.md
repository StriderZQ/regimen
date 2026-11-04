# The nightly training plan

Every night at 22:01 local the worker rebuilds the coming days of training in Garmin and
sends one plain-text summary of what changed and why. No model is involved at any point:
`app/workers/gym.py` imports the planner, the builder and the Garmin client, and a test
asserts it never imports `anthropic`.

It writes without asking, which is the one exception to "the agent never changes state on
its own": with no assistant session open there would be nobody to confirm, and the
calendar would stay empty. The guarantees that replace the question are listed below.

## Setting up your programme

The programme comes from files — see `examples/training/` for a working sample:

```sh
docker compose exec app python -m app.cli import-training --dir examples/training
docker compose exec app python -m app.cli gym-plan --dry-run
```

Copy the folder, replace the exercises with your own (keys and categories must be Garmin's
exact identifiers), import it, and check the dry run before the first night. Importing
again adds what is new and duplicates nothing.

## What decides what

Two authorities, deliberately separate.

**The programme decides what a session contains.** `ProgramExercise` holds the exercises
of each day, their order, target rep range, rest and how each one gets harder. Without it
the plan could only be derived from what was logged, and an exercise skipped once left the
programme for good — silently, because nothing reports a session that got shorter.

**The history decides the numbers.** `plan_day` reads the last three sessions of that slot
and computes each weight from them.

## The progression rule

Load rises when the **top of the target range is reached in every working set**, on **two
consecutive sessions of that slot**, and then by 2-10 % — the lower end, which stays safe
in a calorie deficit too. The source is the ACSM position stand on progression models in
resistance training (Med Sci Sports Exerc, 2009).

Reading the best set instead of the worst is what produced "10 кг × 12 дважды подряд —
норма выполнена" for a session of 12, 10, 12. A refusal now names the sets it refused on.

Four kinds of progression, because "add load" cannot say all of them:

| | What moves |
|---|---|
| `LOAD` | barbell, dumbbell, stack — kilograms up |
| `ASSIST` | gravitron, assisted dips — counterweight **down** |
| `REPS` | bodyweight counted in repetitions |
| `TIME` | holds: plank, side plank, the vacuum |

A third session is read but never decides: two numbers are a pair, three are a direction,
so three identical sessions are reported as a plateau.

## What it will not do

**It will not plan a day whose predecessor has not happened.** Next Monday appears the
night after this Monday is trained. Writing it earlier means putting a number into an
equation you already know you will replace.

**It will not delete anything.** One durable template per program day, rewritten in place
with `update_workout`, so the id survives and the calendar entry stays attached. The fake
client in the tests raises on `delete_workout` so a regression towards deleting fails the
suite.

**It will not schedule twice.** Updating a workout is idempotent; scheduling one is not,
so the calendar is read before it is written to.

**It will not invent a session it cannot compute.** Cycling, the stair machine and running
have no builder yet. Their templates are put in the calendar unchanged — an empty Tuesday
is worse than an unchanged one — and their content is never touched.

**It will not act on readiness.** The figure is reported in the message and nothing else:
whether to train on a bad morning is the owner's call, not the planner's.

## Safety rails

`sane()` refuses a weight that cannot be right and says so in the line where the number
would have gone: more than +15 % in one step, less than half, above 300 kg, or above 50 kg
when the whole history tops out under 25. It never clamps silently — a refusal nobody can
see is worse than the number, because nothing would say the plan had stopped moving.

Every write is read back before it is recorded: Garmin answers 200 for a payload it has
partly ignored, and fetching the workout again is the only way to find out.

Each day commits as soon as it is written, so a failure on Thursday cannot undo Monday.

## Threshold tests (optional)

Every zone is computed from a measured threshold, and the measurement goes stale. If you
list your own test workouts in `GARMIN_THRESHOLD_TESTS` (see `env.example`), the next test
is booked six weeks after the last one **actually performed**, computed from history
rather than stored — so a date moved by hand simply becomes the new anchor. Nothing is
written while a future test is already on the calendar. Left empty, nothing is booked.

## What the message says

Tomorrow's session by name, the weights that moved, and one line for the days that did
not. Nothing else: the plan is already on the watch, and a message spelling out every
exercise of every day means thirty lines to find the two numbers that changed.

A failure is never folded into that summary; it is marked and kept.

## Seeing it without writing anything

```sh
python -m app.cli gym-plan --dry-run
```

Prints exactly what the nightly job would send, and touches Garmin only to read. The job
and the dry run ask the same generator which days are next, so they cannot disagree.
