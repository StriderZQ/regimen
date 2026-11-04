# Connecting a client

Four ways in, and they are not equivalent. Pick by where you want to be when you use it.

| Client | Where it works | How it authenticates |
|---|---|---|
| Claude app (web, desktop, **phone**) | everywhere you are signed in | OAuth — a custom connector on your account |
| ChatGPT | everywhere you are signed in | OAuth — a custom connector, behind developer mode |
| Claude Code (CLI, IDE) | the machine it is installed on | a static token in a header, or OAuth |
| Telegram bot | the phone | the owner's chat id; no token in the conversation |

Claude Code has **no MCP support on the web or in the mobile app** — only the CLI and the
IDE plugins. A connector added in the Claude app is the route to a phone; a server added
with `claude mcp add` stays on that one computer and syncs nowhere.

## Claude, personal plan (Pro or Max)

1. Open **[claude.ai/customize/connectors](https://claude.ai/customize/connectors)**.
   Not Settings — the page moved, and older guides still point at the old place.
2. Click **+** → **Add custom connector**.
3. URL: `https://your.domain/mcp`. Leave the OAuth client ID and secret **empty** — the
   server registers the client itself, which is what dynamic client registration is for.
4. Connect. A waiting page appears; approve in Telegram (below).

The connector lives on the account, so it shows up in the phone app on its own. Inside a
conversation it may still need enabling in the tools menu.

## Claude, inside an organisation (Team or Enterprise)

**A member cannot add a custom connector.** This is the usual reason the "+" is missing
while everything else on the page looks normal: the account belongs to an organisation,
and there the capability is the Owner's.

1. An Owner adds it once at **Organization settings → Connectors → Add → Custom → Web**
   ([claude.ai/admin-settings/connectors](https://claude.ai/admin-settings/connectors)),
   with the same URL.
2. Every member then connects their own account at
   [Customize → Connectors](https://claude.ai/customize/connectors) — each gets their own
   approval and their own token.

If you have both a personal and a work account, check which one the browser is signed
into before concluding the feature is missing.

## ChatGPT

**Settings → Apps → Advanced → Developer mode**, then **Add custom connector**: same URL,
authentication **OAuth**. Developer mode is not on every plan — the cheaper tiers
(including Go) do not carry third-party connectors at all, and the button simply is not
there. That is a subscription question, not a configuration one.

## Claude Code (CLI)

With the service published, OAuth works here too and the browser opens by itself:

```sh
claude mcp add --transport http regimen https://your.domain/mcp
```

Without a public address — or when you would rather not depend on one — a static token
through an SSH tunnel is still the shortest path:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18090:127.0.0.1:8090 user@server
claude mcp add --transport http regimen http://127.0.0.1:18090/mcp/ \
    --header "Authorization: Bearer <token from client-add>"
```

## What approval looks like

Whichever client asks, the sequence is the same:

1. The client discovers the server from a `401` and registers itself. Registration grants
   nothing.
2. The browser lands on a page that says "confirm in Telegram". Nothing can be typed into
   it, so nothing on it can be phished.
3. The bot sends the request — which program, which permissions — with two buttons.
4. Allow, and the page hands the client its token. Deny, and the client is sent home.

Timings: fifteen minutes to answer, five minutes for the code once you have, an hour for
the access token, and sixty days of silent refreshing after that. Miss the window and
nothing breaks — start again.

A connector receives `read` and `write_facts`. **`admin` is not on offer at all**: it is
not in the list of scopes this server will issue, so no connector can hold it, however it
asks. Administrative work stays with tokens issued by hand on the server.

## When it does not work

| What you see | What it usually is |
|---|---|
| No "Add custom connector" button | the account is inside an organisation — the Owner adds it, see above |
| The connector says the server is unreachable | the address is not public, or only the tunnel is up. Check `/healthz` from outside |
| It connects, then every call fails | `PUBLIC_BASE_URL` does not match the address the client used. It must be identical, without a trailing slash |
| No Telegram message arrives | the app container has no bot token or owner chat id. The request stays pending and expires |
| "This server does not issue tokens for …" | the client asked for a different audience than this server publishes. Compare with `/.well-known/oauth-protected-resource/mcp` |

Everything the server publishes can be read without logging in, which makes the first
three cheap to check:

```sh
curl https://your.domain/healthz
curl https://your.domain/.well-known/oauth-protected-resource/mcp
curl -i -X POST https://your.domain/mcp/ -d '{}'   # expect 401 + WWW-Authenticate
```
