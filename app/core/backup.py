"""Backup and restore.

The key condition set when SQLite was chosen: a copy is taken through the Online Backup
API, NOT by copying the file while it is live. Copying a `.db` with an active WAL yields
a corrupt or partial snapshot — the classic mistake that makes "we have backups" mean
"our backups are useless".

RPO is 15 minutes: the owner's decision. Losing more is unacceptable, less is unneeded.
"""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

INTRADAY = "intraday"
DAILY = "daily"
MONTHLY = "monthly"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size_bytes: int
    pages: int
    created_at: dt.datetime


def _timestamp() -> str:
    """Down to milliseconds: two copies in one second must not overwrite each other."""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")


def create_backup(kind: str = INTRADAY, *, compress: bool = True) -> BackupResult:
    """A consistent snapshot of the live database."""
    settings = get_settings()
    target_dir = settings.backup_dir / kind
    target_dir.mkdir(parents=True, exist_ok=True)

    raw_path = target_dir / f"kitchen-{_timestamp()}.db"
    pages = 0

    source = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(raw_path)
        try:
            def _progress(status, remaining, total):  # noqa: ARG001
                nonlocal pages
                pages = total

            source.backup(dest, pages=0, progress=_progress)
        finally:
            dest.close()
    finally:
        source.close()

    final_path = raw_path
    if compress:
        final_path = raw_path.with_suffix(".db.gz")
        with open(raw_path, "rb") as src, gzip.open(final_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        raw_path.unlink()

    # Owner-only: the copy contains everything, including tokens for external services
    final_path.chmod(0o600)

    result = BackupResult(
        path=final_path,
        size_bytes=final_path.stat().st_size,
        pages=pages,
        created_at=dt.datetime.now(dt.UTC),
    )
    log.info("Backup created: %s (%s bytes)", result.path.name, result.size_bytes)
    return result


def prune(kind: str, keep: int) -> int:
    """Delete old copies, keeping the most recent `keep`."""
    target_dir = get_settings().backup_dir / kind
    if not target_dir.exists():
        return 0
    files = sorted(target_dir.glob("kitchen-*.db*"), key=lambda p: p.name, reverse=True)
    removed = 0
    for stale in files[keep:]:
        stale.unlink()
        removed += 1
    return removed


def restore(backup_path: Path, target_path: Path) -> None:
    """Restore: put a copy back in place of the database.

    An existing database is NOT overwritten silently — the caller must move it aside.
    That is the guard against restoring on top of live data.
    """
    if target_path.exists():
        raise FileExistsError(
            f"{target_path} exists. Move it aside deliberately before restoring."
        )
    if backup_path.suffix == ".gz":
        with gzip.open(backup_path, "rb") as src, open(target_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    else:
        shutil.copy2(backup_path, target_path)
    target_path.chmod(0o600)


def verify(backup_path: Path) -> dict:
    """Check that a copy is readable and intact.

    This is the difference between "a copy exists" and "a copy works". Run cheaply after
    every copy, and monthly with a full restore.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.db"
        restore(backup_path, probe)
        conn = sqlite3.connect(f"file:{probe}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
            try:
                version = conn.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                migration = version[0] if version else None
            except sqlite3.OperationalError:
                migration = None
        finally:
            conn.close()

    ok = integrity == "ok"
    if not ok:
        log.error("Copy %s failed its integrity check: %s", backup_path.name, integrity)
    return {
        "ok": ok,
        "integrity": integrity,
        "tables": tables,
        "migration": migration,
        "path": str(backup_path),
    }
