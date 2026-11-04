# Operations

Running the service day to day: what it promises, how the backups work, how to restore,
and what breaks in normal life without being an emergency.

## Promises, and what backs them

| Promise | Figure | Evidence |
|---|---|---|
| Lose no more than 15 minutes of data (RPO) | 15 min | scheduled snapshot in `app/workers/runner.py`, test `test_schedule_covers_the_promises` |
| Bring data back from a copy (data RTO) | seconds at this size | `scripts/restore_drill.py` compares row counts between live database and copy |
| A copy is readable and intact | — | `app/core/backup.py::verify`, `PRAGMA integrity_check` |
| A copy exists off the box | daily | whatever `BACKUP_OFFSITE_COMMAND` you configure |

**Full RTO** — the service, not just the data — is the time to bring containers up on a
new machine plus the time to restore data. Measure it during your first deployment and
write the number here. An estimate is not a measurement.

## Backups

Three tiers, each with its own lifetime:

| Tier | Every | Kept | For |
|---|---|---|---|
| `intraday` | 15 minutes | 96 (one day) | undoing your own mistake |
| `daily` | day | 30 days | a mistake noticed a week later |
| `monthly` | month | 12 months | long history |

Snapshots go through the **SQLite Online Backup API**, not a file copy. A `cp` of a live
database can produce something unusable, and you find out at the worst possible moment.

Copies are written mode `0600`: they contain the whole database, including tokens for
external services.

### Off-box copy

The daily copy leaves the machine immediately after it is made. The command is yours to
choose — `{path}` is substituted with the backup file:

```sh
BACKUP_OFFSITE_COMMAND=rclone copy {path} remote:regimen-backups
BACKUP_OFFSITE_COMMAND=scp {path} user@host:/backups/regimen/
```

If you use `rclone`, it ships **inside the image** rather than on the host, so the service
performs the upload itself and reports when it fails. Its configuration belongs on the
data volume (`/data/rclone.conf`, mode 600) next to the integration tokens — the
container does not run as root and cannot read root's home.

A failed upload is an **error, not a warning**: the job fails and shows up in
`app.cli status`.

### Restore drill

```sh
python scripts/restore_drill.py
```

Restores a copy into a temporary directory and compares row counts across fourteen tables
against the live database. A mismatch is a failure, not a warning.

Run it **before deploying**, **monthly**, and **after every schema migration**.

Drill the off-box copy in particular — it is the one that survives losing the disk:

```sh
# fetch the newest copy from wherever you send them, then
docker compose cp <file> app:/tmp/from-cloud.db.gz
docker compose exec app python scripts/restore_drill.py /tmp/from-cloud.db.gz
```

### Restoring for real

```sh
docker compose stop app worker
mv var/kitchen.db var/kitchen.db.broken      # keep it; you will want it for the post-mortem
python -m app.cli restore var/backups/daily/kitchen-YYYYMMDDTHHMMSSZ.db.gz
python -m app.cli status
docker compose start app worker
```

`restore` **refuses to overwrite** an existing database silently — you move the old file
aside by hand. That is the guard against restoring on top of live data.

## Giving an assistant access

The service authenticates MCP clients by token:

```sh
python -m app.cli client-add --name claude --kind claude --scopes read,write_facts
```

The token is printed once and never shown again. Scopes are `read`, `write_facts` and
`admin` — a read-only token cannot write off food, which is what makes the boundary real
rather than a sentence in a prompt.

If the service answers over plain HTTP, do not send that token across the open internet.
Either terminate TLS in front of it (the `tls` profile starts Caddy, which will obtain a
certificate for a real hostname or an `<ip>.nip.io` name), or reach it through an SSH
tunnel:

```sh
ssh -N -L 127.0.0.1:18090:127.0.0.1:8090 user@server
```

Then point the MCP client at `http://127.0.0.1:18090/mcp/`. Pass
`-o ExitOnForwardFailure=yes`: a tunnel that comes up without the forward is worse than no
tunnel, because it looks alive.

A phone has no tunnel and no header field to paste a token into. That route is OAuth: the
same service acts as its own authorisation server and asks the owner in Telegram before
letting anyone in. Every client and its click path: [connecting](connecting.md).

## What breaks in the normal course of things

**Garmin disconnects after their updates.** This is a normal state, not an outage. The
sign: `garmin_freshness` shows stale data and the log says a re-login is needed. Only a
human can fix it: run `docker compose exec app python -m app.cli garmin-login` again and
enter the password and the 2FA code. The service cannot pass a login and 2FA on its own,
and does not pretend it can.

**A grocery token expires.** The `silpo.sync` job does not retry forever: it records that
authorisation is needed and stops. A human passes it in a browser.

**Telegram is unreachable.** The reminder is not marked as sent and goes out on the next
pass. Treating a send-into-nowhere as delivery is how a reminder gets lost.

**The training plan is not written.** How it works: `docs/training-plan.md`.
 At 23:10 local the `gym.plan` job rebuilds the next
seven days of strength training in Garmin. Silence from it usually means one of three
things, and the first two say so out loud: the Garmin token is dead (one line in Telegram,
plus an alert on the webhook), or Garmin accepted a workout only in part (the day is
recorded `failed` with the reason in `planned_workouts.note`). The third is quiet by
design: a night whose plan is identical to the one already written makes no network call
at all, and reports "без изменений".

To see what it would write without touching Garmin: `python -m app.cli gym-plan --dry-run`.

A day with no exercises on record produces nothing rather than a guess, and a day whose
kind has no builder yet — cycling, running, the stair machine — is skipped by name. Check
`program_exercises` for the first and `program_days.kind` for the second.

**The owner renamed or deleted a workout template by hand.** The link between a program
day and its Garmin template is an id, not a name, so renaming is safe. Deleting is
recovered from: the next run rebuilds the template and stores the new id, and says so in
the message. What it will not do is delete anything itself.

**Alerts deliberately bypass Telegram** (`ALERT_WEBHOOK_URL`). If the bot breaks, a
message about the breakage cannot travel through the bot — and that is exactly when you
need it.

## Sharing a machine

If the service shares a host with anything else, keep them apart: separate containers,
explicit ports (`APP_PORT`, `CADDY_HTTP_PORT`), and the resource limits already set in
`docker-compose.yml`, so a failure here cannot drag a neighbour down. There is no shared
state, and backups are separate. Moving to a dedicated machine is: copy the database file,
bring the containers up.

## Daily health check

```sh
python -m app.cli status
```

Shows whether the database exists, where the copies are, whether an owner is configured,
how many clients hold tokens, and — most importantly — **jobs left in a failed state**. A
non-empty list there means something has been quietly not working for a while.
