"""Cheap registered shared-wall checks, persisted in the existing event log.

Call run_due_checks before the dispatcher's release lane, and apply_check_clears
at the end of blocker_states. No subprocess runs under a SQLite write lock.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shlex
import subprocess
import time

from hermes_cli.kanban_blocker_inputs import input_pass, read

logger = logging.getLogger(__name__)
CHECK_EVENT = "shared_blocker_check"
# Raced/discarded runs: cadence only, never evidence (not read by apply_check_clears or the fence).
ATTEMPT_EVENT = "shared_blocker_check_attempt"
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
    section = cfg.get("kanban") or {}
    if not isinstance(section, dict):
        return {}
    commands = {"github-rest-quota": f"python3 {shlex.quote(str(get_hermes_home() / 'bin' / 'github_quota.py'))} --reserve-ok"}
    overrides = section.get("shared_blocker_checks") or {}
    if isinstance(overrides, dict):
        for raw, command in overrides.items():
            bid = normalize_blocker_id(raw, cfg=cfg)
            if bid:
                commands[bid] = command.strip() if isinstance(command, str) else ""
    return {bid: cmd for bid, cmd in commands.items() if cmd}


def _lookback(now) -> int:
    from hermes_cli.kanban_shared_blockers import _LOOKBACK_SECONDS

    return (int(time.time()) if now is None else int(now)) - _LOOKBACK_SECONDS


def _assertion_tokens(conn, now=None) -> dict[str, str]:
    ts = int(time.time()) if now is None else int(now)
    return read('tokens', conn, ts, lambda: _read_assertion_tokens(conn, ts))


def _read_assertion_tokens(conn, now=None) -> dict[str, str]:
    """Portable fingerprint of the latest assertion timestamp's complete cohort.

    Synced run UIDs survive integer-id remapping and same-second runs. Sorting
    identities also survives opposite import order. Older assertions aging out
    must not revoke a successful check. Unsynced legacy schemas use task/time.

    G2c: the cohort is every in-window assertion of the id whose ended_at epoch is the newest, an
    order-free function of the assertion scan, so it reads the rows ``blocker_states`` decoded
    (``kanban_blocker_rows``; afresh inside a transaction).
    """
    from hermes_cli import kanban_blocker_rows as rows

    ts = int(time.time()) if now is None else int(now)
    window, _tasks = rows.inputs(conn, ts)
    tokens, newest = {}, {}
    for row in window:
        if row.effect == "assert":
            bid = row.bid
            raw = str(row.uid or f"{row.task_id}|{row.started}|{row.ended}|{bid}")
            if row.ended > newest.get(bid, -1):
                newest[bid], tokens[bid] = row.ended, []
            if row.ended == newest[bid]:
                tokens[bid].append(raw)
    return {bid: hashlib.sha256('\n'.join(sorted(ids)).encode()).hexdigest()
            for bid, ids in tokens.items()}


def _checked(payload) -> int:
    try:
        return int(payload.get("checked_at") or 0)
    except (TypeError, ValueError):
        return 0


def run_identity(conn, run_id) -> str:
    """Portable identity of a run: its synced UID, else task/start."""
    from hermes_cli import kanban_db as kb

    columns = {row[1] for row in conn.execute('PRAGMA table_info(task_runs)')}
    identity = 'uid' if 'uid' in columns else 'NULL AS uid'
    row = conn.execute(f'SELECT {identity}, task_id, started_at FROM task_runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        return ''
    return str(row['uid'] or f"{row['task_id']}|{kb._to_epoch(row['started_at']) or 0}")


def _active_run_ids(conn) -> list[str]:
    """Identities of runs open right now."""
    return sorted(filter(None, (run_identity(conn, r[0]) for r in
                                conn.execute('SELECT id FROM task_runs WHERE ended_at IS NULL'))))


def _latest_attempts(conn, now=None) -> dict[str, dict]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id

    results = {}
    for row in conn.execute("SELECT payload FROM task_events WHERE kind=? AND created_at >= ? ORDER BY id",
                            (ATTEMPT_EVENT, _lookback(now))):
        payload = kb._json_dict(row["payload"])
        bid = normalize_blocker_id(payload.get("blocker_id"))
        if bid and (bid not in results or _checked(payload) >= _checked(results[bid])):
            results[bid] = payload
    return results


def _result_key(payload, token) -> tuple:
    # Equal-second conflicting observations for one cohort fail closed. A newer
    # cohort supersedes its predecessor even when the wall reopened that second.
    return (_checked(payload), payload.get("assertion_id") == token, payload.get("rc") != 0,
            json.dumps(payload, sort_keys=True, ensure_ascii=True))


def _check_payloads(conn, now):
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)

    def load():
        return [kb._json_dict(row['payload']) for row in conn.execute(
            'SELECT payload FROM task_events WHERE kind=? AND created_at >= ? ORDER BY id',
            (CHECK_EVENT, _lookback(now)))]

    return read('checks', conn, now, load)


def _latest_results(conn, now=None) -> dict[str, dict]:
    # All lanes reduce the same inputs. Retain the reduction only within the
    # existing commit/config-validated scope, with private copies for callers.
    ts = int(time.time()) if now is None else int(now)
    results, _trusted = read('latest_check_results', conn, ts,
                             lambda: _reduce_latest_results(conn, ts), store_if=lambda value: value[1])
    return results


def _reduce_latest_results(conn, now):
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    from hermes_cli.kanban_blocker_inputs import config
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        cfg, trusted = config(), True
    except ConfigBackendUnavailable:
        cfg, trusted = {}, False  # preserve slug fallback, but never publish it as an alias-aware result
    results = {}
    tokens = _assertion_tokens(conn, now)
    for payload in _check_payloads(conn, now):
        bid = normalize_blocker_id(payload.get("blocker_id"), cfg=cfg)
        token = tokens.get(bid)
        if bid and (bid not in results or _result_key(payload, token) > _result_key(results[bid], token)):
            results[bid] = payload
    return results, trusted


def apply_check_clears(conn, states, now=None) -> None:
    """Fold durable success into derived states; the event carrier is NOT the clearer."""
    from hermes_cli.kanban_shared_blockers import _mark_cleared

    results = _latest_results(conn, now)
    if not results:
        return
    tokens = _assertion_tokens(conn, now)
    for bid, result in results.items():
        st = states.get(bid)
        if (st is not None and result.get("rc") == 0 and result.get("assertion_id")
                and result.get("assertion_id") == tokens.get(bid)):
            _mark_cleared(st, "", None, max(int(result["checked_at"]), st.asserted_at + 1), via="check")


def _run_check(command: str) -> tuple[object, str]:
    from tools.environments.local import build_subprocess_env
    from hermes_cli.kanban_wake_check import _kill

    try:
        # detached-spawn: ok — communicate is bounded and timeout kills the process tree.
        proc = subprocess.Popen(
            ["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=build_subprocess_env(strip_launch_profile=True),
            # detached-spawn: ok — bounded communicate; timeout kills the process group.
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


def _carriers(conn, task_ids):
    """Evidence carriers must still be locally writable, not merely home-owned."""
    from hermes_cli import kanban_blocker_rows as rows
    from hermes_cli.kanban_home_transfer import frozen

    tasks = set(task_ids)
    foreign = rows.foreign_among(conn, tasks)
    return sorted(t for t in tasks - foreign if not frozen(conn, t))


@input_pass
def run_due_checks(conn, *, cfg=None, now=None, due_out=None) -> list[str]:
    """Check each open registered wall on its home, even with a worker probe in flight.

    Exit 1 keeps it open. Three consecutive errors disable the cheap check for
    this assertion/command, leaving the existing worker-probe interval in charge.
    due_out contributes the next cheap-check deadline to the maintenance gate.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import blocker_states
    from hermes_cli import kanban_blocker_rows as blocker_rows

    if conn.in_transaction:
        raise ValueError("shared blocker checks must run outside a write transaction")
    ts = int(time.time()) if now is None else int(now)
    commands = registered_checks(cfg)
    states = blocker_states(conn, now=ts)
    apply_check_clears(conn, states, ts)
    foreign = blocker_rows.foreign_among(conn, (t for st in states.values() for t in st.behind))   # G2c
    tokens, previous = _assertion_tokens(conn, ts), _latest_results(conn, ts)
    attempts = _latest_attempts(conn, ts)
    cleared = []
    for bid, st in states.items():
        command = commands.get(bid)
        # Events need a real card FK; a home-owned card carries evidence only.
        homes = _carriers(conn, set(st.behind) - foreign)
        if not st.is_open or not command or not homes:
            continue
        token = tokens.get(bid)
        if token is None:
            continue
        last = previous.get(bid, {})
        # Backoff belongs to the open episode, not the newest assertion: a worker re-park must
        # neither reset the error count nor postpone the check. A success, a command change
        # or a clear since the last result starts a new episode.
        if last.get("command") != command or last.get("rc") == 0 or _checked(last) < st.cleared_at:
            last = {}
        fails = int(last.get("fails", 0))
        if fails >= BROKEN_AFTER:
            continue
        started = int(last.get("started_at", last.get("checked_at", ts - CHECK_EVERY_SECONDS)))
        attempt = attempts.get(bid, {})
        # A raced (discarded) run keeps cadence without counting as success or failure.
        if (attempt.get("command") == command and _checked(attempt) >= st.cleared_at
                and int(attempt.get("started_at") or 0) > started):
            started = int(attempt["started_at"])
        due = started + CHECK_EVERY_SECONDS
        if due > ts:
            if due_out is not None:
                due_out.append(due)
            continue
        rc, output = _run_check(command)
        # Fence workers admitted while the command was in flight as well. An
        # explicit now is the caller's deterministic clock for replay/tests.
        checked_at = int(time.time()) if now is None else ts
        fails = 0 if rc in (0, 1) else fails + 1
        payload = {"blocker_id": bid, "command": command, "rc": rc, "output": output,
                   "started_at": ts, "checked_at": checked_at, "assertion_id": token, "fails": fails}
        with kb.write_txn(conn):
            # A shell check can overlap a new assertion or a home ownership change.
            homes = _carriers(conn, homes)
            if not homes:
                continue
            if _assertion_tokens(conn, ts).get(bid) != token:
                kb._append_event(conn, homes[0], ATTEMPT_EVENT, {
                    "blocker_id": bid, "command": command, "rc": "discarded", "started_at": ts,
                    "checked_at": checked_at, "reason": "assertion changed during check"})
                if due_out is not None:
                    due_out.append(ts + CHECK_EVERY_SECONDS)
                continue
            # Millisecond evidence complements the local snapshot for remote
            # claims not yet synced. Explicit replay clocks have second precision.
            payload['checked_at_ms'] = int(time.time() * 1000) if now is None else ts * 1000
            payload["active_runs"] = _active_run_ids(conn)
            kb._append_event(conn, homes[0], CHECK_EVENT, payload)
        if rc == 0:
            cleared.append(bid)
        elif fails < BROKEN_AFTER and due_out is not None:
            due_out.append(ts + CHECK_EVERY_SECONDS)
        elif fails >= BROKEN_AFTER:
            logger.warning("shared blocker %s check failed %s times; falling back to worker probes", bid, fails)
    return cleared
