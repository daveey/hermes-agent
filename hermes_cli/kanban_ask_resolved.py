"""Human asks that resolve outside Hermes: ``human_request.resolved_check``. [daveey]

A blocked card's ask can name a probe that exits 0 once David's step happened anywhere (he pressed the device's
pair button, flipped the Settings toggle), so the card does not sit blocked until he also clicks unblock.

* Exit codes are literal: 0 = the asked-for state holds, 1 = not yet, anything else (exit >= 2, a timeout, a
  probe that could not start) = the probe failed. Output is never read for meaning, only quoted.
* Filing runs the probe once, synchronously, before the write (``filing_error``), in the cwd and env the sweep
  will use for that card (``filing_context``): exit 1 = the ask is filed; exit 0 = refused (``ALREADY_RESOLVED``)
  so the worker continues instead of asking; anything else = refused as ``BROKEN`` with the output tail. The
  command filed is the command stored: a probe that redaction would change is refused (``kanban_human_request``).
* A filed ask registers one ``task_wake_checks`` row with ``purpose='ask_resolved'`` (``sync_row``), and the
  filing event names this gateway as ``probe_host`` on a synced board. The dispatcher's condition-wake sweep
  (``kanban_wake_check.run_due_wake_checks``) probes it while the card is ``blocked`` with the same runner,
  concurrency cap and failure count as a park's ``wake_check``, and the same placement rules: a worker node keeps
  a blocked copy whose probe it runs (``kanban_worker_node.evict``); a board that lost the probe of an ask it
  filed re-registers it once (``RESTORED_EVENT``), and a second loss comments once (``LOST_EVENT``) and stops.
  A worker node's copy says ``ALIVE_EVENT`` at most every ``ALIVE_EVERY`` while it probes; when the probe host
  of a relayed ask has been silent past that plus two intervals, the home board comments once
  (``SILENT_EVENT``). It never runs another gateway's probe and never unblocks for it: the card stays blocked on
  David's ask, which is the safe default when nothing watches the probe.
* A create files the ask before the card exists, so its filing run has the target board's pin, the assignee
  profile's env and the workspace cwd, but no ``HERMES_KANBAN_TASK`` (the sweep's runs have it). A probe that
  needs its own card id is filed with ``kanban block`` after the create. An idempotent create that returns an
  existing card runs no probe.
* One exit 0 appends an ``ask_resolved`` event, comments, and unblocks through the normal unblock path
  (``ready``, or ``todo`` behind an open parent). ``BROKEN_AFTER`` consecutive failures spanning at least
  ``BROKEN_MIN_SECONDS`` drop the probe, mark the ask and notify its origins once. Exit 1 resets the streak.
  The card stays blocked: a broken probe never answers for David and is never restored until re-filed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

PURPOSE = "ask_resolved"
EVENT = "ask_resolved"
BROKEN_EVENT = "resolved_check_broken"
ALREADY_RESOLVED = "ask_already_resolved"
BROKEN = "resolved_check_broken"
AUTHOR = "kanban-dispatcher"
RESTORED_EVENT = "resolved_check_restored"
LOST_EVENT = "resolved_check_lost"
ALIVE_EVENT = "resolved_check_alive"
SILENT_EVENT = "resolved_check_host_silent"
BROKEN_MIN_SECONDS = 6 * 60 * 60


def _request(value: Any) -> Optional[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def resolved_check(value: Any) -> tuple[Optional[str], Optional[int]]:
    """``(cmd, every)`` of a stored/validated human_request, or ``(None, None)``."""
    req = _request(value) or {}
    cmd = req.get("resolved_check")
    if not isinstance(cmd, str) or not cmd.strip():
        return None, None
    from hermes_cli.kanban_human_request import RESOLVED_CHECK_EVERY_DEFAULT
    every = req.get("resolved_check_every")
    return cmd.strip(), every if isinstance(every, int) and not isinstance(every, bool) else RESOLVED_CHECK_EVERY_DEFAULT


# --- filing --------------------------------------------------------------------------------------

def refusal_code(message: str) -> Optional[str]:
    from hermes_cli.kanban_human_request import RESOLVED_CHECK_SECRET

    return next((c for c in (ALREADY_RESOLVED, BROKEN, RESOLVED_CHECK_SECRET) if (message or "").startswith(c)), None)


def filing_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """``cwd``/``env`` the sweep will probe ``task_id`` with, so the filing run sees what the sweep sees."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_wake_check as kwc

    task = kb.get_task(conn, task_id)
    db_path = kbc._main_db_file(conn)
    if task is None or not db_path:
        return {}
    return {"cwd": kwc.probe_cwd(task.workspace_path), "env": kwc._probe_env(task, str(db_path))}


def filing_refusal(conn: sqlite3.Connection, task_ids: Any, human_request: Any) -> Optional[tuple[str, str]]:
    """``(task_id, refusal)`` for the first of ``task_ids`` whose own probe context refuses the ask, else None.
    Every card is checked before any is touched: cards differ in workspace and assignee, so one card's pass
    says nothing about another's. Unknown ids are skipped (the transition refuses them)."""
    from hermes_cli import kanban_db as kb

    if not resolved_check(human_request)[0]:
        return None
    for tid in task_ids or ():
        if not tid or kb.get_task(conn, tid) is None:
            continue
        err = filing_error(human_request, **filing_context(conn, tid))
        if err:
            return tid, err
    return None


def filing_error(human_request: Any, *, cwd: Optional[str] = None, env: Optional[dict] = None) -> Optional[str]:
    """Run the ask's ``resolved_check`` once before it is filed. None = file it (exit 1, or no probe); else the
    refusal text, starting with ``ALREADY_RESOLVED`` (exit 0) or ``BROKEN`` (anything else). Raises ValueError
    on an invalid request (including a probe that would not be stored as given)."""
    from hermes_cli import kanban_wake_check as kwc
    from hermes_cli.kanban_human_request import validate_human_request

    cmd, _every = resolved_check(validate_human_request(human_request))
    if not cmd:
        return None
    env = dict(env if env is not None else os.environ)
    if env.get("HERMES_KANBAN_TASK"):
        env["HERMES_KANBAN_WAKE_PROBE"] = "1"
    rc, out = kwc._run_probe(("ask-filing", cmd), cmd, cwd or str(Path.home()), env)
    tail = kwc._tail(out)
    if rc == 1:
        return None
    if rc == 0:
        return (f"{ALREADY_RESOLVED}: the asked-for state already holds; don\u2019t ask, continue the work. "
                f"human_request.resolved_check `{cmd}` exited 0 at filing. Probe output: {tail!r}")
    return (f"{BROKEN}: human_request.resolved_check `{cmd}` was run once at filing and exited {rc}, so it could "
            "never say the ask is done. Exit codes: 0 = David's step is done, 1 = not yet, anything else = "
            f"broken. Fix the probe or drop resolved_check. Output tail: {tail!r}")


class AskFilingRefused(ValueError):
    """``filing_error`` as an exception for the CLI create path; ``code`` is the refusal code."""

    def __init__(self, message: str):
        self.code = refusal_code(message) or BROKEN
        super().__init__(message[len(self.code) + 2:] if message.startswith(self.code + ": ") else message)


def create_context(conn: Optional[sqlite3.Connection], workspace_path: Any, assignee: Any) -> dict:
    """``filing_context`` for a card not created yet: the sweep's cwd for that workspace and the sweep's env for
    that assignee on ``conn``'s board, minus the card id that does not exist yet (no ``HERMES_KANBAN_TASK``; the
    creator's own run/claim/session ids are cleared as the sweep clears them)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_wake_check as kwc

    cwd = kwc.probe_cwd(workspace_path if isinstance(workspace_path, str) else None)
    db_path = kbc._main_db_file(conn) if conn is not None else None
    if not db_path:
        return {"cwd": cwd}
    return {"cwd": cwd, "env": kwc.probe_env(None, assignee if isinstance(assignee, str) else None, str(db_path))}


def idempotent_hit(conn: sqlite3.Connection, idempotency_key: Any) -> bool:
    """Whether a create with this key returns an existing card (``create_task``'s own lookup): then nothing is
    filed, so the filing probe must not run again on a retry."""
    if not idempotency_key:
        return False
    return conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1",
                        (idempotency_key,)).fetchone() is not None


def create_filing_error(human_request: Any, initial_status: Optional[str], *,
                        context: Optional[dict] = None) -> Optional[str]:
    """``filing_error`` for an ask that lands ``blocked``. A probe that would not be stored as given
    (``ResolvedCheckRefused``: the tool paths store the ask redacted, so the write's own validation would only
    see the redacted command) is refused here on any status; any other invalid request is left to the write's
    own validation (``create_task`` / ``block_task``), which refuses it before anything is filed."""
    from hermes_cli.kanban_human_request import ResolvedCheckRefused, validate_human_request

    try:
        validate_human_request(human_request)
    except ResolvedCheckRefused as exc:
        return str(exc)
    except ValueError:
        return None
    if initial_status != "blocked":
        return None
    try:
        return filing_error(human_request, **(context or {}))
    except ValueError:
        return None


def check_create_filing(human_request: Any, initial_status: Optional[str], *, context: Optional[dict] = None) -> None:
    """Raise ``AskFilingRefused`` when a blocked create's resolved_check already passes or is broken."""
    err = create_filing_error(human_request, initial_status, context=context)
    if err:
        raise AskFilingRefused(err)


def ask_checks(human_request: Any, initial_status: Optional[str] = "blocked", *,
               context: Optional[dict] = None) -> tuple:
    """``_check`` args for the tool entry points: the ask-line lint, then (for an ask that will land blocked)
    the filing probe in ``context`` (``filing_context`` / ``create_context``). An invalid request is left to the
    transition's own validation."""
    from hermes_cli.kanban_block_policy import ask_lint_check

    lint = ask_lint_check(human_request)
    if not lint[0]:
        return lint
    err = create_filing_error(human_request, initial_status, context=context)
    return err is None, err or "", refusal_code(err or "")


def create_ask_check(conn: sqlite3.Connection, args: dict, assignee: Optional[str]) -> tuple:
    """``ask_checks`` for the kanban_create tool on the board it writes to: the ask-line lint and the secret
    refusal always; the filing probe (in ``create_context``) only when the create files a new card, never on a
    retry whose ``idempotency_key`` returns the existing one."""
    request, status = args.get("human_request"), str(args.get("initial_status") or "running")
    if idempotent_hit(conn, args.get("idempotency_key")):
        status = None
    context = create_context(conn, args.get("workspace_path"), assignee) if resolved_check(request)[0] else None
    return ask_checks(request, status, context=context)


def filing_fields(conn: sqlite3.Connection, human_request: Any) -> dict:
    """Keys for the filing event: on a synced board, the gateway that ran the filing probe runs the sweep's."""
    from hermes_cli import kanban_wake_check as kwc

    if not resolved_check(human_request)[0]:
        return {}
    host = kwc.own_gateway(conn)
    return {"probe_host": host} if host else {}


# --- state row -----------------------------------------------------------------------------------

def store_request(conn: sqlite3.Connection, task_id: str, human_request: Optional[dict]) -> None:
    """Write the card's human_request and (re)register its resolved_check probe. Caller holds the write txn."""
    conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (request_json(human_request), task_id))
    sync_row(conn, task_id)


def request_json(human_request: Optional[dict]) -> Optional[str]:
    """The stored form of an ask (drop markers stripped); None for no ask."""
    if not human_request:
        return None
    return json.dumps({k: v for k, v in human_request.items()
                       if k not in ("resolved_check_dropped_at", "resolved_check_drop_reason")})


def sync_row(conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None, due_now: bool = False) -> None:
    """A ``blocked`` card whose ask has a resolved_check gets one ``ask_resolved`` probe row, first due one
    interval after filing (filing just ran it; ``due_now`` for a restored probe, which nobody ran while it was
    lost); any other card loses its ``ask_resolved`` row. In the txn."""
    row = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    cmd, every = resolved_check(row[1]) if row is not None and row[0] == "blocked" else (None, None)
    try:
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ? AND purpose = ?", (task_id, PURPOSE))
    except sqlite3.OperationalError:  # board predating ask probes; init_db migrates it
        return
    if not cmd or every is None:
        return
    now = int(time.time()) if now is None else int(now)
    conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
    conn.execute(
        "INSERT INTO task_wake_checks (task_id, cmd, every, next_at, fails, generation, purpose) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (task_id, cmd, int(every), now if due_now else now + int(every), uuid.uuid4().hex, PURPOSE))


# --- sweep result --------------------------------------------------------------------------------

def satisfied_note(cmd: str, tail: str) -> str:
    return (f"David's ask was satisfied outside Hermes (probe `{cmd}` exited 0: `{tail}`). "
            "Verify the state and continue; don't redo the asked action.")


def mark_dropped(conn: sqlite3.Connection, task_id: str, kind: str, now: int) -> None:
    """Persist probe custody loss alongside its event; keep the filed action intact."""
    row = conn.execute("SELECT human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    request = _request(row[0]) or {}
    request.update(resolved_check_dropped_at=now, resolved_check_drop_reason=kind)
    conn.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (json.dumps(request), task_id))


def broken_note(cmd: str, rc: Any, fails: int, tail: str) -> str:
    return (f"This ask's resolved_check `{cmd}` failed {fails} times in a row (last rc {rc}): `{tail}`. It is no "
            "longer probed and the card stays blocked on David's ask; re-file the ask with a fixed resolved_check "
            "(exit 0 = done, 1 = not yet) if it should resume on its own.")


def _current(conn: sqlite3.Connection, task_id: str, cmd: str, generation: str) -> Optional[dict]:
    from hermes_cli import kanban_wake_check as kwc

    state = kwc.get_state(conn, task_id)
    row = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if (state is None or state.get("purpose") != PURPOSE or state["cmd"] != cmd or state["generation"] != generation
            or row is None or row[0] != "blocked" or resolved_check(row[1])[0] != cmd):
        return None
    return state


def _unblock(conn: sqlite3.Connection, task_id: str, cmd: str, generation: str, tail: str) -> Optional[str]:
    from hermes_cli import kanban_db as kb

    def precondition(c: sqlite3.Connection) -> Optional[str]:
        if _current(c, task_id, cmd, generation) is None:
            return "stale"
        kb._append_event(c, task_id, EVENT, {"cmd": cmd, "rc": 0, "output": tail})
        c.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
        return None

    ok = kb.unblock_task(conn, task_id, comment=(AUTHOR, satisfied_note(cmd, tail)),
                         event_extra={"via": EVENT}, precondition=precondition)
    return EVENT if ok else None


def apply_result(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any, out: str, *, generation: str,
                 now: Optional[int] = None) -> Optional[str]:
    """Record one ``ask_resolved`` probe result, reading ``rc`` literally (0 done, 1 not yet, else failed).
    Returns ``EVENT`` (unblocked), ``BROKEN_EVENT`` (gave up) or None. A result for a row that is no longer the
    card's (re-filed, unblocked, archived) is dropped."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    now = int(time.time()) if now is None else int(now)
    tail = kwc._tail(out)
    if rc == 0:
        return _unblock(conn, task_id, cmd, generation, tail)
    with kb.write_txn(conn):
        state = _current(conn, task_id, cmd, generation)
        if state is None:
            return None
        nxt = now + int(state["every"])
        if rc == 1:
            conn.execute("UPDATE task_wake_checks SET last_at = ?, last_rc = 1, last_out = ?, fails = 0, first_failure_at = NULL, next_at = ? "
                         "WHERE task_id = ?", (now, tail, nxt, task_id))
            note_alive(conn, task_id, cmd, now)
            return None
        fails = int(state["fails"] or 0) + 1
        first = conn.execute("SELECT first_failure_at FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()[0]
        # Legacy rows have a count but no start time: grant a fresh grace period.
        first = now if first is None else int(first)
        rc_col = rc if isinstance(rc, int) else -1
        if fails < kwc.BROKEN_AFTER or now - first < BROKEN_MIN_SECONDS:
            conn.execute("UPDATE task_wake_checks SET last_at = ?, last_rc = ?, last_out = ?, fails = ?, next_at = ?, first_failure_at = ? "
                         "WHERE task_id = ?", (now, rc_col, tail, fails, nxt, first, task_id))
            note_alive(conn, task_id, cmd, now)
            return None
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))
        mark_dropped(conn, task_id, BROKEN_EVENT, now)
        kb._append_event(conn, task_id, BROKEN_EVENT, {"cmd": cmd, "rc": rc, "fails": fails, "output": tail})
        kb.add_comment(conn, task_id, AUTHOR, broken_note(cmd, rc, fails, tail))
        return BROKEN_EVENT


# --- lost probes (synced boards) -----------------------------------------------------------------

def note_alive(conn: sqlite3.Connection, task_id: str, cmd: str, now: int) -> None:
    """On a worker node's copy, say (at most every ``ALIVE_EVERY``) that the relayed ask probe still runs, so
    the home board can tell a live remote probe from a silent one (``kanban_wake_check._note_alive`` for
    parks). In the caller's txn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    if not kwc.foreign_copy(conn, task_id):
        return
    last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind IN (?, 'blocked')",
                        (task_id, ALIVE_EVENT)).fetchone()[0] or 0
    if now - int(last) >= kwc.ALIVE_EVERY:
        kb._append_event(conn, task_id, ALIVE_EVENT, {"cmd": cmd, "host": kwc.own_gateway(conn)})


def silent_note(host: str, cmd: str, minutes: int) -> str:
    return (f"This ask's resolved_check `{cmd}` runs on {host}, the gateway that filed it, and {host} has not "
            f"reported it for {minutes} min, so it may not be watched. The card stays blocked on David's ask; "
            "unblock it when his step is done.")

def lost_note(cmd: str) -> str:
    return (f"This ask's resolved_check `{cmd}` went missing twice while the card was blocked, so it is no longer "
            "probed; the card stays blocked on David's ask. Re-file the ask (kanban block --metadata-only "
            "--human-request) if it should resume on its own.")


def reconcile(conn: sqlite3.Connection, *, now: Optional[int] = None) -> dict:
    """Keep each blocked ask's probe row in step with its stored ask. A row whose card no longer carries that
    resolved_check is dropped. A blocked ask with a resolved_check but no probe row on this board (evicted copy,
    a row lost to an older sweep or core) is restored once per filing when this board runs it: its filing event
    names this gateway as ``probe_host``, or names none on a card this gateway owns. A probe lost again after a
    restore comments once (``LOST_EVENT``) and stays off; a probe that exhausted its failures
    (``BROKEN_EVENT``) or already resolved the ask is never restored, and takes no write txn. A relayed ask whose
    probe host went silent comments once (``SILENT_EVENT``). Never unblocks. Returns ids per outcome."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    out: dict = {"restored": [], "lost": [], "dropped": [], "silent": []}
    now = int(time.time()) if now is None else int(now)
    try:
        rows = conn.execute(
            "SELECT w.task_id, w.cmd, t.human_request FROM task_wake_checks w JOIN tasks t ON t.id = w.task_id "
            "WHERE w.purpose = ? AND t.status = 'blocked'", (PURPOSE,)).fetchall()
        missing = conn.execute(
            "SELECT t.id FROM tasks t LEFT JOIN task_wake_checks w ON w.task_id = t.id WHERE t.status = 'blocked' "
            "AND w.task_id IS NULL AND t.human_request LIKE '%\"resolved_check\"%'").fetchall()
    except sqlite3.OperationalError:
        return out
    from hermes_cli.kanban_home_transfer import frozen
    rows = [r for r in rows if not frozen(conn, r[0])]
    missing = [r for r in missing if not frozen(conn, r[0])]
    stale = [r[0] for r in rows if resolved_check(r[2])[0] != r[1]]
    if stale:
        with kb.write_txn(conn):
            for tid in stale:
                sync_row(conn, tid)
                out["dropped"].append(tid)
    if not missing:
        return out
    from hermes_cli.kanban_ask_resolved_backfill import backfill

    backfill(conn)  # legacy drops without a mark; a no-op once marked
    own = kwc.own_gateway(conn)
    foreign = kwc.foreign_ids(conn, own)
    terminal = (BROKEN_EVENT, EVENT, LOST_EVENT)

    def after(tid: str, filed_id: int, kinds: tuple) -> bool:
        m = ",".join("?" for _ in kinds)
        return conn.execute(f"SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ({m}) LIMIT 1",
                            (tid, filed_id, *kinds)).fetchone() is not None

    for (tid,) in missing:
        filed = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind = 'blocked' "
                             "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if filed is None:
            continue
        filed_id, payload, filed_at = int(filed[0]), kb._json_dict(filed[1]), int(filed[2] or 0)
        from hermes_cli import kanban_home_probe
        host = kanban_home_probe.host(conn, tid, filed_id, payload.get("probe_host"))
        if host == kanban_home_probe.UNKNOWN:
            continue
        if host and own and host != own:
            # Another gateway runs this ask's probe. On its home board, say once when that host went silent;
            # a worker node's copy of another gateway's card is left alone. Never run it here, never unblock.
            if tid in foreign or after(tid, filed_id, (SILENT_EVENT, *terminal)):
                continue
            row = conn.execute("SELECT human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
            cmd, every = resolved_check(row[0]) if row is not None else (None, None)
            if not cmd:
                continue
            last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND id > ? AND kind = ?",
                                (tid, filed_id, ALIVE_EVENT)).fetchone()[0]
            baseline = kanban_home_probe.remote_baseline(conn, tid, filed_id, host, payload.get("probe_host"), now)
            silent = now - max(filed_at, int(last or 0), baseline)
            if silent <= kwc.ALIVE_EVERY + 2 * int(every) + kwc.SILENT_GRACE:
                continue
            with kb.write_txn(conn):
                st = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
                if (st is None or st[0] != "blocked" or resolved_check(st[1])[0] != cmd
                        or after(tid, filed_id, (SILENT_EVENT, *terminal))):
                    continue
                kb._append_event(conn, tid, SILENT_EVENT, {"cmd": cmd, "probe_host": host, "silent_seconds": silent,
                                                            "filed_event_id": filed_id})
                kb.add_comment(conn, tid, AUTHOR, silent_note(host, cmd, silent // 60))
                out["silent"].append(tid)
            continue
        if tid in foreign and not host:
            continue  # a legacy copy: its home board runs this ask's probe
        # Read-only first: a probe that stopped for good (broken, resolved, lost twice) takes no write txn.
        if after(tid, filed_id, terminal):
            continue
        restored = after(tid, filed_id, (RESTORED_EVENT,))
        with kb.write_txn(conn):
            row = conn.execute("SELECT status, human_request FROM tasks WHERE id = ?", (tid,)).fetchone()
            cmd = resolved_check(row[1])[0] if row is not None and row[0] == "blocked" else None
            if (not cmd or kwc.get_state(conn, tid) is not None or after(tid, filed_id, terminal)
                    or after(tid, filed_id, (RESTORED_EVENT,)) != restored):
                continue
            if not restored:
                sync_row(conn, tid, now=now, due_now=True)
                kb._append_event(conn, tid, RESTORED_EVENT, {"cmd": cmd, "filed_event_id": filed_id,
                                                              **({"probe_host": host} if host else {})})
                out["restored"].append(tid)
            else:
                mark_dropped(conn, tid, LOST_EVENT, now)
                kb._append_event(conn, tid, LOST_EVENT, {"cmd": cmd, "filed_event_id": filed_id})
                kb.add_comment(conn, tid, AUTHOR, lost_note(cmd))
                out["lost"].append(tid)
    return out
