"""Administrative commands. Run: python -m app.cli <command>.

These are not agent tools. This is what a human does by hand: create the owner, issue a
client token, take a copy, restore, check the state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.core import backup as backup_mod
from app.core.security import create_client
from app.db import session_scope
from app.models import Client, ClientKind, Job, JobStatus, Principal, Scope
from app.models.policies import PolicyKind


def cmd_init(args: argparse.Namespace) -> int:
    """Create the owner. Done once."""
    with session_scope() as session:
        existing = session.scalar(select(Principal))
        if existing is not None:
            print(f"An owner already exists: {existing.name} ({existing.id})")
            return 0
        principal = Principal(name=args.name, display_timezone=args.timezone)
        session.add(principal)
        session.flush()
        print(f"Owner created: {principal.name}")
        print(f"id: {principal.id}")
    return 0


def cmd_client_add(args: argparse.Namespace) -> int:
    """Issue a client token. Shown ONCE and stored nowhere else."""
    scopes = {Scope(s) for s in args.scopes.split(",")}
    with session_scope() as session:
        principal = session.scalar(select(Principal))
        if principal is None:
            print("Create the owner first: python -m app.cli init --name ...")
            return 1
        client, token = create_client(
            session,
            principal=principal,
            name=args.name,
            kind=ClientKind(args.kind),
            scopes=scopes,
            telegram_chat_id=args.telegram_chat_id,
        )
        session.flush()
        print(f"Client: {client.name} ({client.kind.value})")
        print(f"Scopes: {', '.join(sorted(s.value for s in scopes))}")
        print()
        if ClientKind(args.kind) is ClientKind.TELEGRAM:
            # Telegram is identified by chat_id: it needs no token, and a printed
            # secret lingers in the terminal log and in the chat with an agent
            print("No token needed: Telegram is identified by the owner's chat_id.")
            return 0

        if args.token_file:
            path = Path(args.token_file).expanduser()
            path.write_text(token + "\n")
            path.chmod(0o600)
            print(f"Token written to {path} (mode 600). Not printed to the screen.")
            return 0

        print("Token — save it now, it will not be shown again:")
        print(f"  {token}")
    return 0


def cmd_client_list(_args: argparse.Namespace) -> int:
    with session_scope() as session:
        clients = session.scalars(select(Client)).all()
        if not clients:
            print("No clients")
            return 0
        for c in clients:
            state = "active" if c.is_active and c.revoked_at is None else "revoked"
            seen = c.last_seen_at.isoformat() if c.last_seen_at else "never"
            print(f"{c.name:<20} {c.kind.value:<10} {state:<9} last call: {seen}")
    return 0


def cmd_client_revoke(args: argparse.Namespace) -> int:
    from app.models.base import utcnow

    with session_scope() as session:
        client = session.scalar(select(Client).where(Client.name == args.name))
        if client is None:
            print(f"Client {args.name} not found")
            return 1
        client.revoked_at = utcnow()
        client.is_active = False
        print(f"Client {args.name} revoked. Its token no longer works.")
    return 0


def _range(raw: str) -> list[float] | float:
    """"130-150" is a range, "130" a single number. The domain takes the lower bound."""
    if "-" in raw:
        low, high = (part.strip() for part in raw.split("-", 1))
        return [float(low), float(high)]
    return float(raw)


def cmd_targets(args: argparse.Namespace) -> int:
    """Set the macro targets. Without them a menu builds, but a shortfall has no name."""
    from app.domain import rules
    from app.models.policies import PolicyKind

    value: dict = {}
    for flag, key in (
        ("calories", "daily_calories"),
        ("protein", "target_protein_g"),
        ("fat", "target_fat_g"),
        ("carbs", "target_net_carbs_g"),
        ("fiber", "target_fiber_g"),
    ):
        raw = getattr(args, flag)
        if raw:
            value[key] = _range(raw)

    if not value:
        print("Nothing to set: give at least one target, for example --protein 130-150")
        return 1

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            print("Create the owner first: python -m app.cli init")
            return 1
        rules.set_policy(
            session, owner.id, kind=PolicyKind.MACROS, value=value,
            rationale=args.why or "set by the targets command",
        )
    print("Targets recorded:")
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_policy_set(args: argparse.Namespace) -> int:
    """The other rules — meal pattern, carb days — are given as JSON."""
    from app.domain import rules
    from app.models.policies import PolicyKind

    if args.from_file:
        # A markdown brief — the cook's, the coach's — is a document, not a JSON literal
        value = {"instructions_md": Path(args.from_file).read_text(encoding="utf-8")}
    else:
        try:
            value = json.loads(args.json or "")
        except json.JSONDecodeError as exc:
            print(f"Could not parse the JSON: {exc}")
            return 1
    if not isinstance(value, dict):
        print("A rule value is a JSON object, not a list or a number")
        return 1

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            print("Create the owner first: python -m app.cli init")
            return 1
        rules.set_policy(
            session, owner.id, kind=PolicyKind(args.kind), value=value, rationale=args.why
        )
        print(f"Rule {args.kind} recorded.")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    result = backup_mod.create_backup(kind=args.kind)
    check = backup_mod.verify(result.path)
    print(f"Copy:      {result.path}")
    print(f"Size:      {result.size_bytes:,} bytes")
    print(f"Integrity: {'intact' if check['ok'] else 'DAMAGED: ' + check['integrity']}")
    print(f"Migration: {check['migration']}")
    removed = backup_mod.prune(args.kind, args.keep)
    if removed:
        print(f"Old copies removed: {removed}")
    return 0 if check["ok"] else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a copy by restoring it. "A copy exists" versus "a copy works"."""
    check = backup_mod.verify(Path(args.path))
    print(json.dumps(check, ensure_ascii=False, indent=2))
    return 0 if check["ok"] else 1


def cmd_restore(args: argparse.Namespace) -> int:
    settings = get_settings()
    target = Path(args.target) if args.target else settings.db_path
    if target.exists():
        print(f"{target} exists. Move it aside deliberately — restore does not overwrite.")
        return 1
    backup_mod.restore(Path(args.path), target)
    print(f"Restored into {target}")
    return 0


def cmd_import_training(args: argparse.Namespace) -> int:
    """Import a training program from files. Running it again creates no duplicates."""
    from app.migration.training_files import run as run_training

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            print("Create the owner first: python -m app.cli init")
            return 1
        report = run_training(session, owner, Path(args.dir))
        print(report.summary())
        for name in report.skipped:
            print(f"  {name}")
    return 0


def cmd_silpo_login(_args: argparse.Namespace) -> int:
    """Grocery authentication. Needs a browser: the server cannot log in for a human."""
    from app.config import get_settings
    from app.integrations.silpo.login import describe, login

    try:
        token = login(get_settings().silpo_mcp_url)
    except Exception as exc:  # noqa: BLE001 — a human needs words, not a traceback
        print(f"Did not work: {exc}")
        return 1
    print(describe(token))
    print(f"Token file: {get_settings().data_dir / 'silpo-token.json'}")
    return 0


def cmd_silpo_sync(args: argparse.Namespace) -> int:
    """Fetch receipts and create batches for the products already known."""
    from app.integrations.silpo.client import SilpoAuthError, SilpoClient
    from app.integrations.silpo.importer import sync

    try:
        client = SilpoClient()
    except SilpoAuthError as exc:
        print(f"No access: {exc}")
        return 1

    with session_scope() as session, client:
        owner = session.scalar(select(Principal))
        report = sync(session, owner.id, client, months_back=args.months)
        print(report.summary())
        for name in report.pending[:15]:
            print(f"  waiting for an answer: {name}")
        for err in report.errors[:5]:
            print(f"  error: {err}")
    return 0


def cmd_garmin_login(_args: argparse.Namespace) -> int:
    """The one-time Garmin login. Interactive: email, password, then the 2FA code.

    The password is used once and never stored — only the tokens Garmin returns are
    written, into the token directory, and every sync after this runs on them. When the
    tokens eventually die the service says so, and this command is run again.
    """
    import getpass

    from garminconnect import Garmin

    from app.integrations.garmin.sync import token_dir

    path = token_dir()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    email = input("Garmin Connect email: ").strip()
    password = getpass.getpass("Garmin Connect password (not shown, not stored): ")
    client = Garmin(email, password, prompt_mfa=lambda: input("2FA code: ").strip())
    try:
        client.login(str(path))
    except Exception as exc:  # noqa: BLE001 — a human needs words, not a traceback
        print(f"Login failed: {exc}")
        return 1
    print(f"Tokens saved to {path}. Check with: python -m app.cli garmin-sync")
    return 0


def cmd_garmin_sync(args: argparse.Namespace) -> int:
    """Fetch activities, sleep and HRV on the tokens `garmin-login` saved."""
    from app.integrations.garmin.sync import GarminAuthError, connect, freshness, sync

    try:
        client = connect()
    except GarminAuthError as exc:
        print(f"No access: {exc}")
        return 1

    with session_scope() as session:
        owner = session.scalar(select(Principal))
        report = sync(session, owner.id, client, days_back=args.days)
        print(report.summary())
        for err in report.errors[:5]:
            print(f"  error: {err}")
        for item in freshness(session, owner.id):
            print(f"  {item.describe()}")
    return 0


def cmd_gym_plan(args: argparse.Namespace) -> int:
    """Show — or write — the coming week of training.

    The dry run is the one to reach for first: it prints exactly what the nightly job
    would send to the watch without touching Garmin at all, which is how a change to the
    progression rule gets checked before it reaches the gym.
    """

    from app.domain import planning, workout_builder
    from app.integrations.garmin.sync import GarminAuthError, connect
    from app.models.base import local_today
    from app.workers import gym

    today = local_today()
    with session_scope() as session:
        owner = session.scalar(select(Principal))
        if owner is None:
            print("No owner yet.")
            return 1

        if args.dry_run:
            for day, program_day in gym.days_to_plan(session, owner.id, today):
                if program_day.kind not in workout_builder.BUILDERS:
                    print(f"{gym._label(day)} · {program_day.title}: "
                          f"{program_day.kind} — ставится как есть")
                    continue
                plan = planning.plan_day(session, owner.id, program_day, day)
                if plan is None:
                    print(f"{gym._label(day)} · {program_day.title}: состава нет")
                    continue
                workout, skipped = workout_builder.build_for(
                    program_day.kind, plan, name=program_day.title)
                steps = workout.to_dict()["workoutSegments"][0]["workoutSteps"]
                print(f"{gym._label(day)} · {program_day.title}  "
                      f"({len(steps)} шагов)")
                for row in gym._as_rows(plan):
                    print(gym._line(row))
                if skipped:
                    print(f"  не записано: {', '.join(skipped)}")
                print()
            return 0

        try:
            client = connect()
        except GarminAuthError as exc:
            print(f"No access: {exc}")
            return 1
        blocks, failures = gym.plan_week(session, owner.id, client, today)
        for block in blocks:
            print(block)
        for line in failures:
            print(f"  {line}")
    return 0


def cmd_silpo_nutrition(args: argparse.Namespace) -> int:
    """Fill in catalogue macros from the shop's product cards."""
    from app.integrations.silpo.client import SilpoAuthError, SilpoClient
    from app.integrations.silpo.importer import enrich_nutrition

    try:
        client = SilpoClient()
    except SilpoAuthError as exc:
        print(f"No access: {exc}")
        return 1

    with session_scope() as session, client:
        owner = session.scalar(select(Principal))
        report = enrich_nutrition(
            session, owner.id, client, only_missing=not args.all
        )
        print(
            f"updated: {report['updated']}, skipped: {report['skipped']}, "
            f"no data from the shop: {report['no_data']}"
        )
        for conflict in report.get("conflicts", []):
            print(f"  DISAGREEMENT — {conflict['product']}:")
            for line in conflict["differences"]:
                print(f"      {line}")
        for err in report["errors"][:5]:
            print(f"  error: {err}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print(f"Database: {settings.db_path} ({'present' if settings.db_path.exists() else 'MISSING'})")
    print(f"Copies:   {settings.backup_dir}")
    with session_scope() as session:
        principal = session.scalar(select(Principal))
        print(f"Owner:    {principal.name if principal else 'not created'}")
        clients = session.scalars(select(Client)).all()
        print(f"Clients:  {len(clients)}")
        dead = session.scalars(select(Job).where(Job.status == JobStatus.DEAD)).all()
        if dead:
            print(f"\nJOBS DID NOT COMPLETE: {len(dead)} — the owner must be told")
            for job in dead[:5]:
                print(f"  {job.kind}: {job.last_error}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="regimen", description="Administrative commands")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the owner")
    p.add_argument("--name", default="owner")
    p.add_argument("--timezone", default="UTC")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("client-add", help="issue a client token")
    p.add_argument("--name", required=True)
    p.add_argument("--kind", required=True, choices=[k.value for k in ClientKind])
    p.add_argument("--scopes", default="read,write_facts")
    p.add_argument("--telegram-chat-id", type=int, default=None)
    p.add_argument(
        "--token-file",
        help="write the token to a file instead of the screen (mode 600)",
    )
    p.set_defaults(func=cmd_client_add)

    p = sub.add_parser("targets", help="set macro targets: \"130-150\" or a single number")
    p.add_argument("--calories", help="kcal per day, for example 2200-2400")
    p.add_argument("--protein", help="protein, g")
    p.add_argument("--fat", help="fat, g")
    p.add_argument("--carbs", help="net carbohydrates, g")
    p.add_argument("--fiber", help="fibre, g")
    p.add_argument("--why", help="why this much — kept in the rule history")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("policy-set", help="set a rule from a JSON value")
    p.add_argument("--kind", required=True, choices=[k.value for k in PolicyKind])
    p.add_argument("--json", help="the rule value as a JSON object")
    p.add_argument("--from-file", help="a markdown file to store as instructions_md")
    p.add_argument("--why", help="why — kept in the rule history")
    p.set_defaults(func=cmd_policy_set)

    p = sub.add_parser("client-list", help="list clients")
    p.set_defaults(func=cmd_client_list)

    p = sub.add_parser("client-revoke", help="revoke a token")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_client_revoke)

    p = sub.add_parser("backup", help="take a copy and verify it")
    p.add_argument("--kind", default="intraday", choices=["intraday", "daily", "monthly"])
    p.add_argument("--keep", type=int, default=96)
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("verify", help="verify a copy by restoring it")
    p.add_argument("path")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore", help="restore the database from a copy")
    p.add_argument("path")
    p.add_argument("--target", default=None)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("import-training", help="import a training program from files")
    p.add_argument("--dir", default="examples/training")
    p.set_defaults(func=cmd_import_training)

    p = sub.add_parser("silpo-login", help="grocery authentication (needs a browser)")
    p.set_defaults(func=cmd_silpo_login)

    p = sub.add_parser("silpo-sync", help="fetch grocery receipts")
    p.add_argument("--months", type=int, default=1, help="how many months back")
    p.set_defaults(func=cmd_silpo_sync)

    p = sub.add_parser("gym-plan", help="show or write the coming week of training")
    p.add_argument("--dry-run", action="store_true",
                   help="print the week without touching Garmin")
    p.set_defaults(func=cmd_gym_plan)

    p = sub.add_parser("garmin-login", help="one-time Garmin login (interactive, 2FA)")
    p.set_defaults(func=cmd_garmin_login)

    p = sub.add_parser("garmin-sync", help="fetch Garmin data")
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=cmd_garmin_sync)

    p = sub.add_parser("silpo-nutrition", help="macros from the shop's product cards")
    p.add_argument("--all", action="store_true",
                   help="update even where values are already reference ones")
    p.set_defaults(func=cmd_silpo_nutrition)

    p = sub.add_parser("status", help="service state")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
