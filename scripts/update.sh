#!/usr/bin/env bash
# Update a running installation to the latest code.
#
# Run on the server from the repository directory: ./scripts/update.sh
#
# The order matters: the database is copied before migrations touch it, and the health
# check runs after the rebuild, so "updated" means a service that is actually answering,
# not a build that merely finished.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== backup"
docker compose exec -T app python -m app.cli backup | tail -4

echo "== pull"
before=$(git rev-parse --short HEAD)
git pull --ff-only --quiet
after=$(git rev-parse --short HEAD)
echo "$before -> $after"

if [ "$before" = "$after" ]; then
  echo "== nothing new, not rebuilding"
  exit 0
fi

echo "== build"
docker compose up -d --build 2>&1 | tail -5

echo "== wait for health"
for _ in $(seq 1 60); do
  if docker compose ps --format '{{.Service}} {{.Status}}' | grep -q 'app.*healthy'; then
    break
  fi
  sleep 5
done

docker compose ps --format '{{.Service}} {{.Status}}'
if ! docker compose ps --format '{{.Service}} {{.Status}}' | grep -q 'app.*healthy'; then
  echo "!! app did not become healthy"
  docker compose logs app --tail 30
  exit 1
fi

echo "== updated to $after"
