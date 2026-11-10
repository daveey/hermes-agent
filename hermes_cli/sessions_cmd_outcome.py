"""``hermes sessions mute|unmute|outcome|outcome-audit``: the stored chat outcome from the CLI. [daveey]

``outcome-audit`` lists exactly the rows the Desktop sidebar lists (``list_sessions_rich`` over the
profile's ``sessions.interactive_sources``, by recency, pins back-filled) with their outcome: it is the
session-status auditor's input.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

AUDIT_DEFAULT_LIMIT = 100


def _resolve(db, raw_id: str):
    sid = db.resolve_session_id(raw_id)
    if not sid:
        print(f"Session '{raw_id}' not found.")
    return sid


def cmd_mute(db, args, muting: bool):
    from hermes_state_outcome import set_muted

    failures = 0
    for raw_id in args.session_ids:
        sid = _resolve(db, raw_id)
        if not sid:
            failures += 1
            continue
        outcome = set_muted(sid, muting, db=db)
        print(f"{'Muted' if muting else 'Unmuted'} session '{sid}' (outcome: {outcome.get('kind')}).")
    return 1 if failures else None


def _describe(outcome: dict[str, Any] | None) -> str:
    if not outcome:
        return "no outcome stored (run with --refresh)"
    kind = outcome.get("kind")
    if kind == "done":
        return f"done: {outcome.get('summary', '')}"
    if kind == "decision":
        return "decision: " + " | ".join(i.get("question", "") for i in outcome.get("items") or ())
    if kind == "waiting":
        return "waiting on: " + ", ".join(f"{w.get('kind')} {w.get('label')}" for w in outcome.get("on") or ())
    if kind == "unresolved":
        return f"unresolved: {outcome.get('reason')}"
    return str(kind)


def ensure_outcome_hooks() -> None:
    """Load plugins before a re-derive so their outcome providers and judge are registered. Without
    them a CLI/web refresh drops plugin-sourced decisions (user-steps tiles) and downgrades to the
    heuristic, overwriting what the chat's own backend stored. Idempotent; a load failure leaves the
    core-only derivation."""
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
    except Exception:  # health: allow BLE001 -- plugin load is best effort; core sources still derive
        logging.getLogger(__name__).warning("outcome refresh: plugin load failed", exc_info=True)


def cmd_outcome(db, args):
    from hermes_state_outcome import get_outcome, refresh_outcome

    if args.refresh:
        ensure_outcome_hooks()

    sid = _resolve(db, args.session_id)
    if not sid:
        return 1
    outcome = refresh_outcome(sid, "manual", db=db) if args.refresh else get_outcome(sid, db=db)
    if args.json:
        print(json.dumps({"session_id": sid, "outcome": outcome}, indent=2, ensure_ascii=False))
    else:
        print(f"{sid}  {_describe(outcome)}")
    return None


def sidebar_rows(db, limit: int = AUDIT_DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """The rows the Desktop sidebar lists for this profile."""
    from pathlib import Path

    from hermes_cli.session_listing import _sessions_source_list

    interactive = _sessions_source_list(Path(db.db_path).parent, "interactive_sources")
    return db.list_sessions_rich(sources=interactive or None, order_by_last_active=True, limit=limit,
                                 include_pinned=True, compact_rows=True)


def audit_rows(db, *, refresh: bool = False, limit: int = AUDIT_DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """``{id, title, kind, reason, age_h, on, items}`` per sidebar row (``age_h`` = hours since last activity)."""
    from hermes_state_outcome import _cron_items, _lineage_ids, refresh_outcome

    if refresh:
        ensure_outcome_hooks()
    now, out = time.time(), []
    for row in sidebar_rows(db, limit):
        outcome = refresh_outcome(row["id"], "manual", db=db) if refresh else row.get("outcome")
        outcome = outcome or {}
        crons = _cron_items(_lineage_ids(db, row["id"])) if outcome.get("kind") == "done" else []
        if crons:
            outcome = {**outcome, "reason": "done_owns_cron", "on": crons}
        last = row.get("last_active") or row.get("started_at") or now
        out.append({"id": row["id"], "title": row.get("title") or row.get("preview") or "",
                    "kind": outcome.get("kind"), "reason": outcome.get("reason"),
                    "age_h": round(max(0.0, now - float(last)) / 3600.0, 2),
                    "on": outcome.get("on") or [], "items": outcome.get("items") or []})
    return out


def cmd_outcome_audit(db, args):
    rows = audit_rows(db, refresh=args.refresh, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No sidebar sessions.")
        return
    print(f"{'Kind':<11} {'Reason':<12} {'Age h':>6}  {'Title':<40} ID\n" + "─" * 100)
    for r in rows:
        print(f"{(r['kind'] or '-'):<11} {(r['reason'] or ''):<12} {r['age_h']:>6}  {r['title'][:40]:<40} {r['id']}")
