# Self-hosting

Putting Regimen on a machine of your own. It runs as three containers (plus an optional
Caddy for HTTPS) and keeps everything in one SQLite file on a Docker volume. Setup takes
about half an hour, most of it spent on the Garmin and Telegram accounts.

When you are done, this runs on its own, with no assistant session open:

| When | What |
|---|---|
| every 30 min (every 5 min in the morning) | Garmin sync: sleep, HRV, readiness, activities, strength sets |
| from 06:00 local, as soon as the night is in | morning report in Telegram |
| 22:00 | evening report; Sunday 22:05 — weekly report |
| 22:01 | next week of strength training written into the Garmin calendar on your watch |
| every 15 min | database copy; daily copy kept for 30 days |

Reports and the training plan are plain code — no model, no API key needed. The
Anthropic key is only for the Telegram bot you can talk to.

## What you need

| | |
|---|---|
| A Linux host with Docker and the Compose plugin | any small VPS; 1 GB of RAM is plenty |
| A Garmin Connect account | with a watch that syncs to it |
| A Telegram bot | create one with Telegram's BotFather bot; learn your own chat id from the userinfobot bot |
| An Anthropic API key | optional — only for the chat bot |

## 1. Get the code and configure it

```sh
git clone <this repository> regimen && cd regimen
cp env.example .env
nano .env                     # at least DISPLAY_TIMEZONE; read the comments in the file
sh scripts/set-secrets.sh     # Telegram token, your chat id, Anthropic key — typed, not echoed
```

`DISPLAY_TIMEZONE` matters: every report and the nightly plan run on that clock.

## 2. Start it

```sh
docker compose up -d --build
docker compose ps              # app should become "healthy" within a minute
docker compose exec app python -m app.cli init
```

`init` creates the owner — you. It is done once.

## 3. Connect Telegram

```sh
docker compose exec app python -m app.cli client-add \
    --name telegram --kind telegram --telegram-chat-id <your chat id>
```

Write anything to your bot. It answers only your chat id and ignores everyone else.

## 4. Connect Garmin

```sh
docker compose exec app python -m app.cli garmin-login
```

It asks for your email, password and the 2FA code. The password is used once and never
stored: only the tokens Garmin returns are kept, on the data volume. Then:

```sh
docker compose exec app python -m app.cli garmin-sync
```

prints what came in and how fresh each kind of data is.

**Only one program may use these tokens.** If you also run another Garmin connector with
the same account login (a desktop script, another MCP server), the two will keep
invalidating each other.

When Garmin eventually logs the service out, you get an alert and the morning report says
so. Run `garmin-login` again.

## 5. Your targets and rules

```sh
# Macro targets — a range or a single number
docker compose exec app python -m app.cli targets \
    --calories 2200-2400 --protein 130-150 --fat 70-90 --carbs 200-250

# Coaching and cooking rules the assistant follows — edit the examples first
docker compose exec app python -m app.cli policy-set --kind coaching \
    --from-file examples/rules/coach.example.md
docker compose exec app python -m app.cli policy-set --kind cooking \
    --from-file examples/rules/cooking.example.md

# Optional: height, for body fat from tape measurements — set BODY_HEIGHT_CM in .env
```

Rules are versioned: setting one again closes the old version instead of overwriting it.

## 6. Your training programme

The nightly plan needs to know what each strength day contains. Start from the sample:

```sh
cp -r examples/training my-training     # my-training/ is git-ignored
nano my-training/current-week.md        # the shape of your week
nano my-training/exercise-registry.md   # your exercises with their Garmin keys
nano my-training/program-composition.json   # sets, rep ranges, rest, starting weights

docker compose cp my-training app:/tmp/my-training
docker compose exec app python -m app.cli import-training --dir /tmp/my-training
docker compose exec app python -m app.cli gym-plan --dry-run
```

Import your own programme, not the sample first: a program day that already exists is
kept, not replaced.

Garmin keys and categories must be Garmin's exact identifiers — to search them:

```sh
docker compose exec app python -c "from garminconnect.exercises import find; print(find('squat'))"
```

The dry run prints exactly what would go to the watch and touches nothing. From the next
22:01 the plan is written by itself; how it decides is in [training-plan](training-plan.md).
Keep `GYM_PLAN_SILENT=false` for the first weeks so every night's change is reported.

## 7. Prove the backups work

```sh
docker compose exec app python scripts/restore_drill.py
```

Then set `BACKUP_OFFSITE_COMMAND` in `.env`: a copy on the same disk does not survive the
disk. Details: [operations](operations.md).

## 8. Optional: let Claude talk to it

Issue a token and add the MCP server to Claude Code or Claude Desktop:

```sh
docker compose exec app python -m app.cli client-add --name claude --kind claude
```

To use it from the Claude app on a phone you need a public HTTPS address — see
"Reaching it from outside" below and [connecting](connecting.md).

## Updating

```sh
./scripts/update.sh
```

Backs up the database, pulls, rebuilds, and waits for the app to report healthy — or
prints the last log lines and fails loudly.

## Reaching it from outside

Two honest options, and one thing not to do.

**With a hostname**: set `PUBLIC_HOSTNAME` and start the `tls` profile
(`docker compose --profile tls up -d`). Caddy obtains a certificate automatically. A name
of the form `<ip>.nip.io` resolves to that IP and works without owning a domain.

**Without one**: leave it on a private port and reach it through an SSH tunnel (see
[operations](operations.md#giving-an-assistant-access)). Telegram and Garmin work either
way — both connect outwards.

**Not this**: exposing the port publicly over plain HTTP. Client tokens travel in a header
on every request, and the pantry is one write-scoped token away.

## Connecting a phone

A phone cannot hold an SSH tunnel, and the connector screens in Claude and ChatGPT have no
field for a token: they offer OAuth or nothing. So the service is its own authorisation
server, and the owner approves each connection with one tap in Telegram. There is no
password anywhere — the right to approve belongs to whoever holds the chat the bot talks
to.

What has to be true before it works:

| | |
|---|---|
| A public HTTPS address | the connector connects from Anthropic's or OpenAI's servers, not from your machine |
| `PUBLIC_BASE_URL` | the issuer, exactly as clients see it: `https://your.domain`, no trailing slash |
| Telegram configured | `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_CHAT_ID`, or there is nobody to ask |

With `PUBLIC_BASE_URL` empty the OAuth endpoints refuse to issue anything. That is
deliberate: a service that does not know its own address cannot mint tokens for it.

Only these paths need to be reachable from outside — everything else, the REST API
included, can stay behind the firewall:

```
/mcp           the protocol itself
/oauth/*       authorise, token, register, revoke
/.well-known/* discovery documents the clients fetch first
/healthz       so the proxy can tell whether it is alive
```

How it is kept honest:

- **The token is bound to this server.** RFC 8707: a code obtained here cannot become a
  token for somebody else's resource.
- **Codes are single-use, and a replay revokes the session it created.**
- **Refresh tokens rotate**, and reusing a rotated one revokes everything that client holds.
- **Redirect URIs match exactly.**
- **`admin` is not on offer.** A connector can read and record facts; the administrative
  scope stays with the static tokens issued by `client-add`.
- Nothing is stored in the clear: codes and tokens live as digests.

## What this deliberately does not do

- **No medical data.** No results, prescriptions or diagnoses. Medication names are not
  stored even when supplied — the domain replaces them with a neutral label.
- **No automatic ordering.** A basket is filled to the point of one tap; payment is yours.
- **One owner.** A second person later becomes a new row, not a schema rewrite:
  `owner_id` is already everywhere.

## Before you call it done

- `docker compose exec app python -m app.cli status` shows the database, the copies, the
  owner and no failed jobs.
- `scripts/restore_drill.py` passes.
- The next morning a report arrives in Telegram, and the Garmin calendar on your watch
  shows the week's strength sessions.
