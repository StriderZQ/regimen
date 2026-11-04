#!/bin/sh
# Enter secrets into .env so they reach neither the shell history nor a chat with an
# agent. Run on the server from the repository directory: sh scripts/set-secrets.sh
#
# Input is not echoed (stty -echo). Existing values are never shown, only the fact that
# something is set — so a working key is not wiped by an empty line.

set -e
ENV_FILE="${1:-.env}"
[ -f "$ENV_FILE" ] || { echo "No file $ENV_FILE"; exit 1; }

ask() {
  name="$1"; label="$2"; secret="$3"
  current=$(grep "^${name}=" "$ENV_FILE" | cut -d= -f2-)
  if [ -n "$current" ]; then
    printf "%s — already set. Replace? [y/N] " "$label"
    read -r answer
    case "$answer" in y|Y) ;; *) echo "  left as is"; return 0 ;; esac
  fi

  printf "%s: " "$label"
  if [ "$secret" = "secret" ]; then
    stty -echo 2>/dev/null || true
    read -r value
    stty echo 2>/dev/null || true
    echo
  else
    read -r value
  fi
  [ -n "$value" ] || { echo "  empty — skipping"; return 0; }

  tmp=$(mktemp)
  grep -v "^${name}=" "$ENV_FILE" > "$tmp"
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
  chmod 600 "$ENV_FILE"
  echo "  written (${#value} characters)"
}

echo "Secrets are written to $ENV_FILE and never shown on screen."
echo
ask TELEGRAM_BOT_TOKEN     "Telegram bot token from @BotFather" secret
ask TELEGRAM_OWNER_CHAT_ID "Your Telegram chat_id (a number)"   plain
ask ANTHROPIC_API_KEY      "Anthropic API key"                  secret

echo
echo "Done. Check everything is in place (values are not printed):"
awk -F= '/^(TELEGRAM_BOT_TOKEN|TELEGRAM_OWNER_CHAT_ID|ANTHROPIC_API_KEY)=/ {
  printf "  %-24s %s\n", $1, (length($2) ? "set" : "EMPTY")
}' "$ENV_FILE"
