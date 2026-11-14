"""Cheap registered shared-wall checks, persisted in the existing event log.

Call run_due_checks before the dispatcher's release lane, and apply_check_clears
at the end of blocker_states. No subprocess runs under a SQLite write lock.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
import time

logger = logging.getLogger(__name__)
CHECK_EVENT = "shared_blocker_check"
CHECK_EVERY_SECONDS = 120
CHECK_TIMEOUT_SECONDS = 10
BROKEN_AFTER = 3
OUTPUT_TAIL_CHARS = 500


def registered_checks(cfg=None) -> dict[str, str]:
    """Effective commands; an empty/non-string override disables a builtin check."""
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id

    if cfg is None:
        from hermes_cli.config import load_config
        from hermes_cli.config_backend import ConfigBackendUnavailable
        try:
            cfg = load_config()
        except ConfigBackendUnavailable:
            logger.warning("shared blocker checks: config backend unavailable; skipping checks")
            return {}
    commands = {"github-rest-quota": f"{shlex.quote(str(get_hermes_home() / 'bin' / 'github_quota.py'))} --reserve-ok"}
    overrides = (cfg.get("kanban") or {}).get("shared_blocker_checks") or {}
    if isinstance(overrides, dict):
        for raw, command in overrides.items():
            bid = normalize_blocker_id(raw)
            if bid:
                commands[bid] = command.strip() if isinstance(command, str) else ""
    return {bid: cmd for bid, cmd in commands.items() if cmd}


def _assertion_tokens(conn) -> dict[str, int]:
    """Run ids fence same-second reassertions and results racing a worker's park."""
    from hermes_cli.kanban_shared_blockers import run_effect

    tokens = {}
    for row in conn.execute("SELECT id, outcome, metadata FROM task_runs WHERE ended_at IS NOT NULL "
                            "AND metadata LIKE '%\"blocker_id\"%' ORDER BY id"):
        effect, bid = run_effect(row["outcome"], row["metadata"])
        if effect == "assert":
            tokens[bid] = int(row["id"])
    return tokens


def _latest_results(conn) -> dict[str, dict]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id

    results = {}
    for row in conn.execute("SELECT payload FROM task_events WHERE kind=? ORDER BY id", (CHECK_EVENT,)):
        payload = kb._json_dict(row["payload"])
        bid = normalize_blocker_id(payload.get("blocker_id"))
        if bid:
            results[bid] = payload
    return results


def apply_check_clears(conn, states) -> None:
    """Fold durable success into derived states; the event carrier is NOT the clearer."""
    from hermes_cli.kanban_shared_blockers import _mark_cleared

    results = _latest_results(conn)
    if not results:
        return
    tokens = _assertion_tokens(conn)
    for bid, result in results.items():
        st = states.get(bid)
        if st is not None and result.get("rc") == 0 and result.get("assertion_run_id") == tokens.get(bid):
            _mark_cleared(st, "", None, max(int(result["checked_at"]), st.asserted_at + 1), via="check")


def _run_check(command: str) -> tuple[object, str]:
    from tools.environments.local import build_subprocess_env
    from hermes_cli.kanban_wake_check import _kill

    try:
        # detached-spawn: ok — communicate is bounded and timeout kills the process tree.
        proc = subprocess.Popen(
            ["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=build_subprocess_env(strip_launch_profile=True),
            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as exc:
        return "error", str(exc)
    try:
        output, _ = proc.communicate(timeout=CHECK_TIMEOUT_SECONDS)
        return proc.returncode, output[-OUTPUT_TAIL_CHARS:].strip()
    except subprocess.TimeoutExpired:
        _kill(proc)
        try:
            output, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            output = ""
        return "timeout", output[-OUTPUT_TAIL_CHARS:].strip()


def run_due_checks(conn, *, cfg=None, now=None, due_out=None) -> list[str]:
    """Check each open registered wall on its home, even with a worker probe in flight.

    Exit 1 keeps it open. Three consecutive errors disable the cheap check for
    this assertion/command, leaving the existing worker-probe interval in charge.
    due_out contributes the next cheap-check deadline to the maintenance gate.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import blocker_states
    from hermes_cli.kanban_wake_check import foreign_ids

    if conn.in_transaction:
        raise ValueError("shared blocker checks must run outside a write transaction")
    ts = int(time.time()) if now is None else int(now)
    commands = registered_checks(cfg)
    states = blocker_states(conn, now=ts)
    apply_check_clears(conn, states)
    foreign = foreign_ids(conn)
    tokens, previous = _assertion_tokens(conn), _latest_results(conn)
    cleared = []
    for bid, st in states.items():
        command = commands.get(bid)
        # Events need a real card FK; a home-owned card carries evidence only.
        homes = sorted(set(st.behind) - foreign)
        if not st.is_open or not command or not homes:
            continue
        token = tokens[bid]
        last = previous.get(bid, {})
        if last.get("assertion_run_id") != token or last.get("command") != command:
            last = {}
        fails = int(last.get("fails", 0))
        if fails >= BROKEN_AFTER:
            continue
        due = int(last.get("checked_at", ts - CHECK_EVERY_SECONDS)) + CHECK_EVERY_SECONDS
        if due > ts:
            if due_out is not None:
                due_out.append(due)
            continue
        rc, output = _run_check(command)
        fails = 0 if rc in (0, 1) else fails + 1
        payload = {"blocker_id": bid, "command": command, "rc": rc, "output": output,
                   "checked_at": ts, "assertion_run_id": token, "fails": fails}
        with kb.write_txn(conn):
            # A shell check can overlap a new assertion or a home ownership change.
            if _assertion_tokens(conn).get(bid) != token or homes[0] in foreign_ids(conn):
                continue
            kb._append_event(conn, homes[0], CHECK_EVENT, payload)
        if rc == 0:
            cleared.append(bid)
        elif fails < BROKEN_AFTER and due_out is not None:
            due_out.append(ts + CHECK_EVERY_SECONDS)
        elif fails >= BROKEN_AFTER:
            logger.warning("shared blocker %s check failed %s times; falling back to worker probes", bid, fails)
    return cleared
