"""``hermes kanban spend-budget <id>``: show or set one card's own spend budget (kanban_spend_budget). [daveey]

Without a change flag it prints the card's effective limits, its override (who set it, why) and its current
spend. ``--runs N`` / ``--cache-read-tokens N`` (0 = no limit), ``--exempt`` (both 0) or ``--clear`` (back to
the board's limits) record a ``spend_budget_set`` event and need ``--reason``. Worker and delegate_task shells
are fenced from this verb like every other board write, so a looping card cannot exempt itself.
"""
from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as ksb


def _err(msg: str, rc: int = 1) -> int:
    print(msg, file=sys.stderr)
    return rc


def _actor() -> str:
    from hermes_cli.profiles import current_profile_name
    return current_profile_name("user") or "user"


def command(args: argparse.Namespace) -> int:
    tid = args.task_id
    runs, cache = args.runs, args.cache_read_tokens
    exempt, clear = bool(args.exempt), bool(args.clear)
    if ((exempt or clear) and (runs is not None or cache is not None)) or (exempt and clear):
        return _err("kanban spend-budget: --exempt and --clear stand alone (no --runs / --cache-read-tokens)", 2)
    changing = exempt or clear or runs is not None or cache is not None
    if changing and not (args.reason or "").strip():
        return _err("kanban spend-budget: a change needs --reason (it is recorded on the card)", 2)
    if exempt:
        runs = cache = 0
    with kbc.connect_closing() as conn:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is None:
            return _err(f"kanban spend-budget: unknown task {tid}")
        if changing:
            ksb.set_override(conn, tid, runs=runs, cache_read_tokens=cache, by=_actor(), reason=args.reason)
        lim = ksb.card_limits(conn, tid, ksb.limits())
        override = ksb.card_override(conn, tid)
        spent = ksb.spend(conn, tid)
    out = {"task_id": tid, "limits": lim._asdict(), "override": override,
           "spent": {"runs": spent.runs, "since": spent.since, "tokens": spent.tokens}}
    if getattr(args, "json", False):
        print(json.dumps(out, sort_keys=True))
        return 0
    if not lim.runs and not lim.cache_read_tokens:
        print(f"{tid}: no spend budget (exempt)")
    else:
        shown = ", ".join(f"{k} {'no limit' if v == 0 else v}" for k, v in lim._asdict().items())
        print(f"{tid}: spend budget {shown}")
    if override is not None and ksb.override_set(override):
        print(f"  override set by {override.get('by')}: {override.get('reason')}")
    print(f"  spent: {spent.runs} runs, {spent.tokens['cache_read_tokens']} cache-read tokens in this window")
    return 0
