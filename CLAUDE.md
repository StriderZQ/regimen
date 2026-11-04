# Regimen — pantry, nutrition and training service

Guidance for an AI agent working on this code. Setting it up on a server is a separate
document: `docs/deployment.md`.

This repository holds the **code of the service, not its data**. The pantry, the diary,
receipts, training history and the owner's rules all live in a SQLite database on the
owner's server. None of that belongs here: git is a protocol for source code, and live
state kept in it gets overwritten by the first two agents that disagree.

## The first principle

**The system knows only what was entered into it, so entering a fact must be easier than
skipping it.** Everything else follows from that: the bot in the pocket, receipt import,
typical weights filled in automatically. Manual entry loses not because it is slow, but
because it gets skipped.

## Shape

```
Claude / Codex / Telegram / terminal
                 │
        ┌────────┴────────┐
    MCP adapter        REST API
        └────────┬────────┘
             app/domain
                 │
          SQLite (WAL)  ←── background job queue
                 │
     ┌───────────┼───────────┐
 receipt import  Garmin sync  backups
```

| Directory | What lives there |
|---|---|
| `app/domain` | the rules of the subject: pantry, diary, menu, recipes, training |
| `app/core` | access scopes, job queue, idempotency, backups |
| `app/models` | database schema |
| `app/api`, `app/mcp`, `app/bot` | three doors into one system |
| `app/integrations` | grocery receipts and Garmin |
| `app/workers` | the schedule: syncs, reports, the nightly training plan |
| `app/migration` | importing a training programme from files |
| `examples/` | a sample programme and rule files |
| `docs/` | deployment, operations, the training plan |

## Boundaries that do not move

**There is no medical data here.** No lab results, no prescriptions, no diagnoses. Asked
about them, answer that the data is absent — and do not reconstruct it from indirect
signs. Medication names are never stored even when supplied: the domain replaces them
with a neutral label.

**The agent never changes state on its own initiative.** Writing off, throwing out,
correcting, ordering — only after the owner says so. Not "it expired, I wrote it off",
but "this is going bad, write it off?" — and then wait. The guarantee lives in the access
model: those tools demand `Scope.WRITE_FACTS`, and a read-only token cannot spend the
pantry.

One standing exception: **the nightly training plan writes itself into Garmin without
asking.** It is not an agent acting on a hunch — it is `app/workers/gym.py`, deterministic
code with no model in it, running a rule the owner configured. With no session open there
would be nobody to confirm, and the calendar would stay empty. The guarantees that
replace the question are narrow and must stay: it rewrites only the templates it owns and
never deletes anything, it reads every write back before recording it, it reports what it
wrote and why, and `GYM_PLAN_SILENT` decides only whether the message is sent, never
whether the check is done.

**A fact is written immediately.** "Ate it", "did the set" — that is registering something
that already happened, not changing the world. It needs no confirmation.

**A balance is never overwritten.** Balance is the sum of movements; there is no `left_g`
column. A correction is a new movement, not an edit of an old one.

**Every number carries its source.** `receipt` | `confirmed` | `estimated` | `reference`.
A reference value is never passed off as a label.

## Working on the code

- The service talks to its owner in Russian; commits, documentation and identifiers are
  English. Some docstrings inside `app/` are still Russian — translating what you touch
  is welcome, a mass rewrite is not asked for. New files are written in English.
- **No person's name and no personal data appear in the code** — not in docstrings, not
  in comments, not in tests, not in strings that end up in the database. There is a role:
  the owner. Test fixtures use invented numbers and invented Garmin ids.
- Anything specific to one person — height, timezone, test workouts — is a setting in
  `app/config.py` and `env.example`, never a constant.
- A comment explains **why**, not what. What is visible in the code.
- Transactions stay short: **never** wait on Anthropic, a grocery API, Garmin or Telegram
  inside one. That is a correctness condition for SQLite, not a style preference.
- Every datetime carries a timezone. The domain rejects a naive `datetime` with an error.
- After every push, wait for CI and read the result. The runner is UTC and a fresh
  checkout, and both catch failures a local run cannot.

## Commands

```sh
python -m app.cli status          # state, including jobs that did not finish
python -m app.cli targets --protein 130-150   # macro targets
python -m app.cli backup          # snapshot plus verification
python scripts/restore_drill.py   # drill: restore a copy and compare the data
python -m app.cli garmin-login    # one-time Garmin login (2FA)
python -m app.cli garmin-sync     # pull sleep, HRV and workouts
python -m app.cli import-training --dir examples/training
python -m app.cli gym-plan --dry-run          # next week of training, without writing it
pytest -q && ruff check app tests scripts
```

Deployment: `docs/deployment.md`. Operations: `docs/operations.md`.
