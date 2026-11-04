#!/bin/sh
# The schema is applied BEFORE the app starts, and only by it: worker and bot wait
# until app is healthy, so two migrating processes never exist.
#
# Without this the service comes up on an empty database and dies on the first request.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "Applying migrations…"
  python -m alembic upgrade head
fi

exec "$@"
