"""External-run cards, database side: record polls, derive card state, wake, shared service-down blocker. [daveey]

A card parked with ``awaiting.kind`` in ci/deploy/external waits on work that runs on another system. A watch poll
(``kanban_external_adapters``) is recorded as an ``external_watch`` event on the card; ``derive_card`` turns the
newest record into the state in ``kanban_external_state`` by the READER's own clock, so a dead probe host turns the
card light-blue-unknown after one interval and red after two even though it wrote nothing.

Why events: the Board sync carries ``task_events`` but no new table (``kanban_board_row_fields.json`` is the synced
row contract), and ``task_wake_checks`` is machine-local, so a worker's poll would be invisible on the hub. One
event per poll (state change or the heartbeat that keeps the record fresh) is the cost; the writer is
:func:`write_record`, the one place to swap for a synced table.

Mixed versions: a probe host that never wrote an ``external_watch`` event is an older host. Its silence stays grey
(``advertises`` False); only a host seen writing records, or this one, can turn a card red by being silent.

Service outage (not a failed job): the first card whose service goes red for a scope is woken as that scope's
probe with the shared blocker ``service-down.<adapter>.<scope>`` (``kanban_shared_blockers``, as
``runners-offline.*``); the other cards stay parked and red with the blocker named. The first ok poll of any card
on that scope clears the blocker and writes one ``external_recovered`` event on each card that was red.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from hermes_cli import kanban_external_state as ks

logger = logging.getLogger(__name__)

WATCH_EVENT = "external_watch"
RECOVERED_EVENT = "external_recovered"
SERVICE_DOWN = "external_service_down"
BLOCKER_PREFIX = "service-down"
AUTHOR = "kanban-dispatcher"
SHELL_DEFAULT_EVERY = 900
REF_BROKEN = "external_ref_broken"
CI_WAIT_WIDENED = "ci_wait_widened"  # a run park whose run succeeded now waits for its whole commit
SERVICE_HEALTHS = ("down", "unreachable", "auth_failed")  # the service's trouble, shared by a scope's cards


def _json(raw: Any) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def park_context(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's latest park: ``{event_id, since, payload, awaiting}`` or None."""
    row = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind IN "
                       "('scheduled', 'continued') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return None
    payload = _json(row[1])
    awaiting = payload.get("awaiting") if isinstance(payload.get("awaiting"), dict) else None
    return {"event_id": int(row[0]), "since": float(row[2]) if row[2] is not None else None, "payload": payload,
            "awaiting": awaiting}


def latest_record(conn: sqlite3.Connection, task_id: str, after_event: int = 0) -> Optional[dict]:
    """The newest ``external_watch`` payload written after the card's latest park, else None."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND id > ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, WATCH_EVENT, after_event)).fetchone()
    return _json(row[0]) if row else None


def widened_scope(conn: sqlite3.Connection, task_id: str, after_event: int) -> Optional[dict]:
    """The commit the card's current park was widened to (``{from_run, head_sha, pending_runs}``, the
    ``ci_wait_widened`` event after the park ``after_event``), else None. A park scope, not a new table: the event
    syncs with the card and a new park drops it."""
    row = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ? AND id > ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, CI_WAIT_WIDENED, after_event)).fetchone()
    got = _json(row[0]) if row else {}
    if not got.get("head_sha"):
        return None
    if not got.get("since") and row[1] is not None:
        got["since"] = float(row[1])  # when the watched run was seen green
    return got


_HOSTS_LOCK = threading.Lock()
_HOSTS: dict[str, tuple[int, dict[str, float]]] = {}  # board file -> (last event id read, host -> newest record)


# The board's newest event id and, as ONE JSON array, every watch payload after the cursor (from 0 when the newest
# id is below the cursor): one statement and one result row. Read row by row, a cold read was ~10k rows on the hub
# and each row step can wait out a GIL switch interval in the Desktop backend.
_WATCH_PAYLOADS_SQL = (
    "SELECT m.top, json_group_array(e.payload) FILTER (WHERE e.id IS NOT NULL) "
    "FROM (SELECT COALESCE(MAX(id), 0) AS top FROM task_events) m LEFT JOIN task_events e "
    "ON e.kind = ? AND e.id > (CASE WHEN m.top < ? THEN 0 ELSE ? END)")


def _watch_payloads(conn: sqlite3.Connection, cursor: int) -> tuple[int, list]:
    """``(newest event id, watch payloads with a larger id than the cursor)``; the cursor counts as 0 when the
    newest id is below it."""
    try:
        top, packed = conn.execute(_WATCH_PAYLOADS_SQL, (WATCH_EVENT, cursor, cursor)).fetchone()
        return int(top), json.loads(packed)
    except sqlite3.Error:  # e.g. a payload JSON cannot carry (a BLOB): read row by row, as before
        top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0] or 0
        return top, [payload for _eid, payload in conn.execute(
            "SELECT id, payload FROM task_events WHERE kind = ? AND id > ?",
            (WATCH_EVENT, 0 if top < cursor else cursor))]


def hosts_seen(conn: sqlite3.Connection) -> dict[str, float]:
    """``{host: when it last wrote a watch record}`` on this board: the hosts that advertise
    ``external-watch-v1``. Read incrementally (only events newer than the last read, through the
    ``(kind, task_id)`` index, no sort), because it runs on every wait-info read and session-list refresh."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        key = str(row[2] or id(conn)) if row else str(id(conn))
        with _HOSTS_LOCK:
            cursor, hosts = _HOSTS.get(key, (0, {}))
        top, payloads = _watch_payloads(conn, cursor)
        if top < cursor:  # another database behind the same key (a test's fresh file): start over
            cursor, hosts = 0, {}
        hosts = dict(hosts)
        for payload in payloads:
            rec = _json(payload)
            if rec.get("host"):
                at = float(rec.get("observed_at") or 0)
                hosts[str(rec["host"])] = max(hosts.get(str(rec["host"]), 0.0), at)
        with _HOSTS_LOCK:
            _HOSTS[key] = (int(top), hosts)
        return hosts
    except (sqlite3.Error, TypeError, ValueError):
        return {}


def advertising_hosts(conn: sqlite3.Connection) -> set[str]:
    """Hosts seen writing watch records on this board (``external-watch-v1``)."""
    return set(hosts_seen(conn))


def own_host(conn: Optional[sqlite3.Connection]) -> str:
    from hermes_cli.kanban_wake_check import own_gateway
    return own_gateway(conn) or "local"


def plan(conn: sqlite3.Connection, task_id: str, ctx: dict, *, _wake_row: Any = ...) -> Optional[dict]:
    """How the card is watched: ``{adapter, ref, every, hint}`` or None when nothing watches it. A declared
    ``watch`` on the park wins, then a GitHub run/PR ref of a ``ci`` park, then the park's shell probe."""
    from hermes_cli.kanban_external_adapters import github
    payload, awaiting = ctx["payload"], ctx["awaiting"] or {}
    watch = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
    row = _wake_row
    if row is ...:
        row = None
        try:
            row = conn.execute("SELECT cmd, every FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()
        except sqlite3.Error:
            pass
    cmd = (row[0] if row else None) or payload.get("wake_check") or ""
    hint = " ".join(x for x in (cmd, str(payload.get("reason") or "")) if x)
    ref = str(watch.get("ref") or awaiting.get("ref") or "")
    adapter = str(watch.get("adapter") or "") or None
    if adapter not in ("github_run", "github_pr_checks", "shell"):
        adapter = github.adapter_for(ref, hint) if awaiting.get("kind") == "ci" else None
    if adapter is None and cmd:
        adapter = "shell"
    if adapter is None:
        return None
    default = ks.DEFAULT_CI_EVERY if adapter != "shell" else (
        int(row[1]) if row and row[1] else int(payload.get("check_every") or SHELL_DEFAULT_EVERY))
    return {"adapter": adapter, "ref": ref, "hint": hint, "every": ks.clamp_every(watch.get("every"), default)}


def _shell_seen(conn: sqlite3.Connection, task_id: str, rec: dict, every: Any, *, _last_at: Any = ...) -> tuple[dict, Any]:
    """A shell probe THIS host runs: its freshness is the check's own machine-local ``last_at`` (an unsynced
    board writes a record on change only). A probe another host runs is judged by its records alone: that host
    writes one per interval (``record_poll``), so the 2-interval rule holds for it too."""
    seen = float(rec.get("observed_at") or 0)
    if _last_at is ...:
        try:
            row = conn.execute("SELECT last_at FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()
            seen = max(seen, float(row[0] or 0) if row else 0.0)
        except sqlite3.Error:
            pass
    else:
        seen = max(seen, float(_last_at or 0))
    every = ks.clamp_every(every, SHELL_DEFAULT_EVERY)
    return dict(rec, observed_at=seen, next_due_at=seen + ks.clamp_every(rec.get("every"), SHELL_DEFAULT_EVERY)), every


def card_state(conn: sqlite3.Connection, task_id: str, *, now: Optional[float] = None,
               hosts: Optional[dict[str, float]] = None, status: Optional[str] = None, _capture=None) -> Optional[dict]:
    """The derived external state of one card (see ``kanban_external_state.derive``), or None: not a scheduled
    ci/deploy/external park, an old host's silence, or a deadline-only wait. Adds ``blocker_id`` / ``triage_card``
    to ``external`` while a service-down blocker holds the card."""
    now = time.time() if now is None else float(now)
    if status is None:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        status = row[0] if row else None
    if status != "scheduled":
        return None
    ctx = park_context(conn, task_id) if _capture is None else _capture.context(task_id)
    if ctx is None or (ctx["awaiting"] or {}).get("kind") not in ks.EXTERNAL_KINDS:
        return None
    watch = (plan(conn, task_id, ctx) if _capture is None
             else plan(conn, task_id, ctx, _wake_row=_capture.wakes.get(task_id)))
    rec = (latest_record(conn, task_id, ctx["event_id"]) if _capture is None
           else _capture.record(task_id, ctx["event_id"]))
    probe_host = ctx["payload"].get("probe_host")
    own = own_host(conn) if _capture is None else _capture.own
    hosts = hosts_seen(conn) if hosts is None else hosts
    adapter = (rec or {}).get("adapter") or (watch or {}).get("adapter")
    # A GitHub run/PR is polled by every gateway's own sweep; a shell probe only by its ``probe_host``.
    local = adapter in ("github_run", "github_pr_checks") or not probe_host or probe_host in (own, "local")
    advertises = rec is not None or local or probe_host in hosts
    if rec is None and watch is None:
        derived = ks.derive(None, now=now, every=ks.DEFAULT_CI_EVERY, since=ctx["since"], watch_missing=local,
                            advertises=advertises)
    else:
        every = (rec or {}).get("every") or (watch or {}).get("every")
        if rec is not None and rec.get("adapter") == "shell" and local:
            if _capture is None:
                rec, every = _shell_seen(conn, task_id, rec, every)
            else:
                rec, every = _shell_seen(conn, task_id, rec, every, _last_at=_capture.last_at.get(task_id))
        # This host polls the card and wrote some record within the interval: it is alive, only behind here.
        alive = local and adapter != "shell" and now - hosts.get(own, 0.0) <= ks.clamp_every(every) + ks.GRACE
        derived = ks.derive(rec, now=now, every=every, since=ctx["since"], advertises=advertises,
                            adapter=adapter, service_name=(rec or {}).get("service_name"), host_alive=alive)
    if derived is not None:
        derived["adapter"] = derived["external"]["adapter"]
        if _capture is None:
            _name_blocker(conn, derived, rec)
        else:
            _name_blocker(conn, derived, rec, _states=_capture.blockers)
    return derived


def _name_blocker(conn: sqlite3.Connection, derived: dict, rec: Optional[dict], *, _states=None) -> None:
    if derived["state"] != ks.DOWN or not rec or not rec.get("adapter") or not rec.get("scope"):
        return
    if derived["health"] not in SERVICE_HEALTHS:
        return
    bid = blocker_id(rec["adapter"], rec["scope"])
    try:
        from hermes_cli import kanban_shared_blockers as ksb
        st = (ksb.blocker_states(conn) if _states is None else _states()).get(bid)
    except (sqlite3.Error, ValueError):
        return
    if st is not None and st.is_open:
        derived["external"]["blocker_id"] = bid
        derived["external"]["triage_card"] = next(iter(sorted(st.behind)), None)


def card_states(conn: sqlite3.Connection, task_ids: Iterable[str], *, now: Optional[float] = None) -> dict[str, dict]:
    """``{task id: derived state}`` for the scheduled external cards among ``task_ids``. Never raises."""
    owns = not conn.in_transaction
    try:
        if owns:
            conn.execute('BEGIN')
        return _card_states(conn, task_ids, now=now)
    except sqlite3.Error:
        logger.debug("external card states unavailable", exc_info=True)
        return {}
    finally:
        if owns:
            conn.rollback()


def _card_states(conn, task_ids, *, now):
    """All captured inputs and lazy blocker reads belong to the caller's snapshot."""
    out: dict[str, dict] = {}
    try:
        hosts = hosts_seen(conn)
        ids = list(dict.fromkeys(str(t) for t in task_ids))
        # One packed row, not one per parked card: each row step is a GIL release in the Desktop backend. [daveey]
        from hermes_state_page_pack import read_rows
        statuses = dict(read_rows(
            conn, "SELECT id,status FROM tasks WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),), 2))
        from hermes_cli.kanban_external_display import Capture
        selected = [tid for tid in ids if statuses.get(tid) == 'scheduled']
        if not selected:
            return out
        capture = Capture(conn, selected)
        for tid in ids:
            if statuses.get(tid) != 'scheduled':
                continue
            derived = card_state(conn, tid, now=now, hosts=hosts, status=statuses[tid], _capture=capture)
            if derived is not None:
                out[tid] = derived
    except sqlite3.Error:
        logger.debug("external card states unavailable", exc_info=True)
    return out


def blocker_id(adapter: str, scope: str) -> str:
    from hermes_cli.kanban_shared_blockers import normalize_blocker_id
    return normalize_blocker_id(f"{BLOCKER_PREFIX}.{adapter}.{scope}") or f"{BLOCKER_PREFIX}.{adapter}"


# --- writing -------------------------------------------------------------------------------------------------

def write_record(conn: sqlite3.Connection, task_id: str, rec: dict, derived: dict) -> None:
    """Append one watch event. In the caller's write txn. The single place to swap for a synced table."""
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, WATCH_EVENT, {**rec, "state": derived["state"], "health": derived["health"],
                                                  "service_name": derived["external"]["service"]})


def record_poll(conn: sqlite3.Connection, task_id: str, poll: dict, *, now: float, adapter: str, scope: str = "",
                every: Any = None, service_name: Optional[str] = None,
                rand: Optional[float] = None) -> Optional[dict]:
    """Fold ``poll`` into the card's record and write it when the state changed or the heartbeat is due. In the
    caller's write txn. Returns ``{derived, prev_state, rec}``, or None when the park is gone or is not external."""
    from hermes_cli import kanban_db as kb
    ctx = park_context(conn, task_id)
    if ctx is None or (ctx["awaiting"] or {}).get("kind") not in ks.EXTERNAL_KINDS:
        return None
    prev = latest_record(conn, task_id, ctx["event_id"])
    watch = plan(conn, task_id, ctx) or {}
    every = ks.clamp_every(every if every is not None else (prev or {}).get("every") or watch.get("every"))
    rec = ks.next_record(prev, poll, now=now, every=every, adapter=adapter, scope=scope,
                         rand=_rand() if rand is None else rand, host=own_host(conn))
    name = service_name or poll.get("service_name") or ks.SERVICE_NAMES.get(adapter, "external service")
    derived = ks.derive(rec, now=now, every=every, adapter=adapter, service_name=name) or {}
    prev_written = (prev or {}).get("state")
    changed = (prev is None or prev_written != derived["state"] or prev.get("job") != rec["job"]
               or prev.get("service") != rec["service"])
    # The heartbeat: one record per interval, so a reader on another gateway sees silence within 2 intervals.
    # A shell probe on a board that does not sync has no such reader and its own ``task_wake_checks.last_at``
    # is its freshness (``_shell_seen``): it writes on change only, not one event per clean check.
    beats = adapter != "shell" or own_host(conn) != "local"
    due = prev is None or (beats and now - float(prev.get("observed_at") or 0) >= 0.9 * every)
    if changed or due:
        write_record(conn, task_id, rec, derived)
    was_down = prev_written == ks.DOWN
    if was_down and derived["state"] != ks.DOWN:
        kb._append_event(conn, task_id, RECOVERED_EVENT, {"adapter": adapter, "scope": scope, "job": rec["job"],
                                                          "health": derived["health"]})
    return {"derived": derived, "prev_state": prev_written, "rec": rec, "was_down": was_down}


def _result(rec: dict, derived: dict) -> dict:
    return {"job": rec["job"], "outcome": derived["job"] if derived["job"] in ks.TERMINAL_JOBS else "done_unknown",
            "detail": rec.get("detail") or "", "url": rec.get("url"), "adapter": rec.get("adapter"),
            "service": derived["external"]["service"], "observed_at": rec.get("observed_at")}


def apply_poll(conn: sqlite3.Connection, task_id: str, poll: dict, *, now: float, adapter: str, scope: str = "",
               every: Any = None, service_name: Optional[str] = None, rand: Optional[float] = None,
               cmd: str = "", terminal: Optional[bool] = None, park_id: Optional[int] = None,
               ref: Optional[str] = None, widen: Optional[dict] = None) -> Optional[str]:
    """Record one poll for a scheduled card and act on it: a finished job wakes the card with ``external_result``
    (``condition_met``; a FAILED job wakes too and is not an outage); a service gone red asserts the shared
    blocker; an ok poll clears it (a finished job's read included). Returns the event kind that woke the card,
    else None. Opens its own write txn. ``park_id`` / ``ref`` are the park the caller read the poll for: a card
    parked again while the read was in flight (another ref, or the same ref anew) drops the poll untouched.
    ``widen`` (``{from_run, head_sha, pending_runs}``): the watched run succeeded while other runs of its commit are
    pending, so the park now waits for the whole commit: one ``ci_wait_widened`` event per park, no comment."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc
    from hermes_cli.kanban_home_transfer import frozen
    if frozen(conn, task_id):
        return None
    with kb.write_txn(conn):
        if frozen(conn, task_id):
            return None
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if status is None or status[0] != "scheduled":
            return None
        if not _same_park(conn, task_id, park_id, ref):
            return None
        if widen and not poll.get("terminal"):
            ctx = park_context(conn, task_id)
            if ctx is not None and widened_scope(conn, task_id, ctx["event_id"]) is None:
                kb._append_event(conn, task_id, CI_WAIT_WIDENED, {
                    "from_run": widen["from_run"], "head_sha": widen["head_sha"],
                    "pending_runs": list(widen.get("pending_runs") or []), "since": widen.get("since")})
        got = record_poll(conn, task_id, poll, now=now, adapter=adapter, scope=scope, every=every,
                          service_name=service_name, rand=rand)
        if got is None:
            return None
        derived, rec = got["derived"], got["rec"]
        finished = poll.get("terminal") if terminal is None else terminal
        health = derived["health"]
        if health in ("ok", "degraded") and scope:
            _clear_blocker(conn, rec)  # before a finished job's wake returns: this may be the scope's last card
        if finished or derived.get("wake"):
            kwc.episode.reset(conn, task_id)
            result = _result(rec, derived)
            runs = poll.get("runs") if isinstance(poll.get("runs"), list) else None
            if runs:
                result.update(head_sha=poll.get("head_sha"), runs=runs)
            if poll.get("widen_capped"):
                result.update(outcome="pending", pending_runs=list(poll.get("pending_runs") or []))
            woke = kwc._wake(conn, task_id, kwc.CONDITION_MET_EVENT, {
                "wake_check": cmd or None, "output": rec.get("detail") or "", "external_result": result})
            if woke:
                kb.add_comment(conn, task_id, AUTHOR, _finished_note(rec, derived, runs, poll.get("head_sha"),
                                                                     capped=poll.get("widen_capped")))
            return woke
        if derived["state"] == ks.DOWN and health in SERVICE_HEALTHS and scope and not poll.get("host_only"):
            if _scope_answers(conn, task_id, rec):
                return _wake_ref_broken(conn, task_id, rec, derived, cmd)
            return _assert_blocker(conn, task_id, rec, derived)
        if derived["state"] == ks.DOWN and health == "probe_broken" and adapter != "shell":
            return _wake_ref_broken(conn, task_id, rec, derived, cmd)
        return None


def _same_park(conn: sqlite3.Connection, task_id: str, park_id: Optional[int], ref: Optional[str]) -> bool:
    """The card's latest park is still the one a poll was read for. In the caller's write txn."""
    if park_id is None and ref is None:
        return True
    ctx = park_context(conn, task_id)
    if ctx is None:
        return False
    if park_id is not None and ctx["event_id"] != int(park_id):
        return False
    return ref is None or str((ctx["awaiting"] or {}).get("ref") or "") == ref


def _rand() -> float:
    """Where in ±JITTER the next poll lands; the seam tests pin."""
    return random.random()


def _scope_answers(conn: sqlite3.Connection, task_id: str, rec: dict) -> bool:
    """Another scheduled card on this adapter and scope read the service fine after this card began failing: the
    trouble is this card's ref (a run only it names answers 5xx / 403), not an outage of the scope."""
    since = float(rec.get("failing_since") or rec.get("observed_at") or 0)
    rows = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE e.kind = ? AND e.task_id != ? "
        "AND t.status = 'scheduled' ORDER BY e.id DESC LIMIT 200", (WATCH_EVENT, task_id)).fetchall()
    for row in rows:
        other = _json(row[0])
        if (other.get("adapter"), other.get("scope")) == (rec["adapter"], rec["scope"]) and other.get(
                "service") in ("ok", "degraded") and float(other.get("observed_at") or 0) >= since:
            return True
    return False


def _wake_ref_broken(conn: sqlite3.Connection, task_id: str, rec: dict, derived: dict, cmd: str) -> Optional[str]:
    """What this card waits on cannot be read and that is not the service's doing (a deleted run: 404/410/422, or
    a failure only this ref has). Wake this card alone, no shared blocker: nobody else is affected."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import _wake
    note = (f"Woken: the {derived['external']['service']} job this card waits on cannot be read "
            f"({derived['reason']}) and that is this card's ref, not an outage of {rec.get('scope') or 'the service'}"
            ": no shared blocker is asserted. The run or PR was probably deleted or renamed: re-park on a ref that "
            "exists, or finish without it.")
    woke = _wake(conn, task_id, REF_BROKEN, {"adapter": rec["adapter"], "scope": rec.get("scope"),
                                             "health": derived["health"], "wake_check": cmd or None,
                                             "detail": rec.get("detail") or "", "note": note})
    if woke:
        kb.add_comment(conn, task_id, AUTHOR, note)
    return woke


def _finished_note(rec: dict, derived: dict, runs: Optional[list] = None, head_sha: Any = None,
                   capped: Any = None) -> str:
    res = _result(rec, derived)
    where = f" ({res['url']})" if res.get("url") else ""
    if capped:
        pending = [r for r in runs or [] if isinstance(r, dict) and r.get("status") != "completed"]
        still = ", ".join(f"{r.get('name') or 'run'} #{r.get('id')} ({r.get('status') or 'pending'})"
                          for r in pending) or "none listed"
        parts = [f"Woken: the run this card waits on succeeded, but other workflow runs of its commit were still "
                 f"pending {int(capped) // 60} min later{where}: {still}. Waiting on them is capped; check whether "
                 "they need you (an environment approval, a stuck runner) or finish without them."]
    else:
        parts = [f"Woken: the job on {res['service']} finished: {res['outcome']}{where}."]
    if runs:
        listed = "; ".join(f"{r.get('name') or 'run'} #{r.get('id')}: "
                           f"{r.get('conclusion') or r.get('status') or 'unknown'}" for r in runs if isinstance(r, dict))
        parts.append(f"Workflow runs of commit {str(head_sha or '')[:10]}: {listed}.")
    if res["outcome"] == "failed":
        parts.append("A failed job is a result for this card, not an outage.")
    return " ".join(parts)


def _assert_blocker(conn: sqlite3.Connection, task_id: str, rec: dict, derived: dict) -> Optional[str]:
    """The service is down. The first card for this scope is woken as its probe and the shared blocker is
    asserted on it; a later card stays parked (red, blocker named)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_shared_blockers as ksb
    from hermes_cli.kanban_wake_check import _wake
    bid = blocker_id(rec["adapter"], rec["scope"])
    st = ksb.blocker_states(conn).get(bid)
    if st is not None and st.is_open:
        return None
    note = (f"Woken: {derived['external']['service']} is {derived['health']} for {rec['scope']} "
            f"({derived['reason']}). This card is the probe for shared blocker {bid}: triage the service (credential, "
            "runner fleet, probe) and re-park; other cards on this scope stay parked and red behind it, and every "
            "one clears on the first ok poll.")
    woke = _wake(conn, task_id, SERVICE_DOWN, {"adapter": rec["adapter"], "scope": rec["scope"],
                                               "health": derived["health"], "blocker_id": bid, "note": note})
    if not woke:
        return None
    kb.add_comment(conn, task_id, AUTHOR, note)
    run_id = kb._synthesize_ended_run(conn, task_id, outcome=SERVICE_DOWN, summary=derived["reason"][:300],
                                      metadata={"blocker_id": bid, "adapter": rec["adapter"], "scope": rec["scope"]})
    kb._append_event(conn, task_id, ksb.PROBE_RELEASED_EVENT, {"blocker_id": bid, "via": SERVICE_DOWN,
                                                               "after_run": run_id})
    return woke


def _clear_blocker(conn: sqlite3.Connection, rec: dict) -> None:
    """The first ok poll after an outage: clear the scope's blocker on every card behind it."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_shared_blockers as ksb
    bid = blocker_id(rec["adapter"], rec["scope"])
    st = ksb.blocker_states(conn).get(bid)
    if st is None or not st.is_open:
        return
    for tid in sorted(st.behind):
        kb._synthesize_ended_run(conn, tid, outcome="external_recovered", summary=f"{bid} cleared",
                                 metadata={ksb.CLEAR_FLAG: True})


def note_probe_result(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any, out: str, now: float,
                      every: Any) -> Optional[dict]:
    """:func:`note_probe` without the retry delay."""
    return note_probe(conn, task_id, cmd, rc, out, now, every)[0]


def note_probe(conn: sqlite3.Connection, task_id: str, cmd: str, rc: Any, out: str, now: float,
               every: Any) -> tuple[Optional[dict], int]:
    """Returns ``(external_result, delay)``; ``delay`` is the seconds a rate-limited service asked the probe to
    stay away (``HERMES_WATCH`` ``retry_after``; 0 = none), which the caller puts into the probe's ``next_at``.

    A legacy wake-check probe's result as a watch record (``kanban_external_adapters.shell``), for a scheduled
    ci/deploy/external park. In the caller's write txn; never raises. Returns the ``external_result`` to carry on
    a wake (exit 0 or a ``HERMES_WATCH`` line with a terminal job), else None. A GitHub run/PR park is also
    recorded by the CI sweep (the richer adapter); the later record wins."""
    from hermes_cli.kanban_external_adapters import shell
    try:
        ctx = park_context(conn, task_id)
        if ctx is None or (ctx["awaiting"] or {}).get("kind") not in ks.EXTERNAL_KINDS:
            return None, 0
        if (plan(conn, task_id, ctx) or {}).get("adapter") != "shell":
            return None, 0  # a GitHub run/PR park is recorded by the CI sweep's richer adapter
        poll = shell.poll(rc, out)
        got = record_poll(conn, task_id, poll, now=now, adapter="shell", scope="", every=every,
                          service_name=poll.get("service_name"), rand=0.5)  # a probe runs every ``every``: no jitter
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        logger.warning("kanban external watch: could not record the probe result of %s", task_id, exc_info=True)
        return None, 0
    if got is None:
        return None, 0
    delay = int(got["rec"].get("retry_after") or 0) if got["rec"].get("service") == "rate_limited" else 0
    if not poll.get("terminal"):
        return None, delay
    result = _result(got["rec"], got["derived"])
    result["outcome"] = poll.get("outcome") or result["outcome"]
    return result, delay
