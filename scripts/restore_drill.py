#!/usr/bin/env python
"""The restore drill: unpacks a copy somewhere clean and compares the data.

The point is not to confirm that a backup file exists. The point is to prove that a
**working system with the same data** comes up from it: schema at the current migration,
integrity intact, and the same rows as the live database.

Run by hand before deploying and once a month. It changes nothing: the live database is
opened read-only and the restore goes into a temporary directory.

    python scripts/restore_drill.py            # take the newest copy
    python scripts/restore_drill.py path.db.gz # check a specific one
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.core.backup import INTRADAY, create_backup, restore  # noqa: E402

# Tables where a mismatch means lost data rather than a difference in timing
CHECKED = [
    "batches", "stock_moves", "canonical_products", "meals", "meal_items",
    "receipts", "receipt_lines", "recipes", "exercises", "set_logs",
    "workout_sessions", "policies", "program_days", "equipment",
]


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        return {
            table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in CHECKED
            if table in present
        }
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    settings = get_settings()

    if len(argv) > 1:
        backup_path = Path(argv[1])
        print(f"Copy: {backup_path}")
    else:
        print("Taking a fresh copy of the live database…")
        backup_path = create_backup(INTRADAY).path
        print(f"Copy: {backup_path}")

    live = _counts(settings.db_path)

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kag-drill-") as tmp:
        target = Path(tmp) / "kitchen.db"
        restore(backup_path, target)

        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            migration = row[0] if row else None
        finally:
            conn.close()

        restored = _counts(target)
        elapsed = time.monotonic() - started

    print(f"\nIntegrity:    {integrity}")
    print(f"Migration:    {migration}")
    print(f"Restore time: {elapsed:.1f} s  (this is the RTO for the data)")
    print(f"\n{'table':22} {'live':>8} {'copy':>8}")

    problems = []
    for table in CHECKED:
        if table not in live:
            continue
        mark = "" if live[table] == restored.get(table) else "  ← MISMATCH"
        if mark:
            problems.append(table)
        print(f"{table:22} {live[table]:>8} {restored.get(table, 0):>8}{mark}")

    if integrity != "ok":
        print("\nFAILED: the copy is damaged.")
        return 1
    if migration is None:
        print("\nFAILED: the copy has no migration version — the schema cannot come up.")
        return 1
    if problems:
        print(f"\nFAILED: tables disagree: {', '.join(problems)}")
        return 1

        print("\nDrill passed: the copy brings up the same system with the same data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
