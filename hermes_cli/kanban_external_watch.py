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
import time
from typing import Any, Iterable, Optional

from hermes_cli import kanban_external_state as ks

logger = logging.getLogger(__name__)

WATCH_EVENT = "external_watch"
RECOVERED_EVENT = "external_recovered"
SERVICE_DOWN = "external_service_down"
BLOCKER_PREFIX = "service-down"
AUTHOR = "kanban-dispatcher"
ADVERTISED_SCAN = 2000  # newest watch events read to learn which hosts write records
SHELL_DEFAULT_EVERY = 900


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


def advertising_hosts(conn: sqlite3.Connection) -> set[str]:
    """Hosts seen writing watch records on this board (``external-watch-v1``)."""
    try:
        rows = conn.execute("SELECT payload FROM task_events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                            (WATCH_EVENT, ADVERTISED_SCAN)).fetchall()
    except sqlite3.Error:
        return set()
    return {str(h) for h in (_json(r[0]).get("host") for r in rows) if h}


def own_host(conn: Optional[sqlite3.Connection]) -> str:
    from hermes_cli.kanban_wake_check import own_gateway
    return own_gateway(conn) or "local"


def plan(conn: sqlite3.Connection, task_id: str, ctx: dict) -> Optional[dict]:
    """How the card is watched: ``{adapter, ref, every, hint}`` or None when nothing watches it. A declared
    ``watch`` on the park wins, then a GitHub run/PR ref of a ``ci`` park, then the park's shell probe."""
    from hermes_cli.kanban_external_adapters import github
    payload, awaiting = ctx["payload"], ctx["awaiting"] or {}
    watch = payload.get("watch") if isinstance(payload.get("watch"), dict) else {}
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


def _shell_seen(conn: sqlite3.Connection, task_id: str, rec: dict, every: Any, local: bool) -> tuple[dict, Any]:
    """A shell record is written on change only, so its freshness is the newest sign the probe still runs: the
    check's own ``last_at`` on this host, the ``wake_check_alive`` a worker node sends every ``ALIVE_EVERY``. A
    probe relayed from another host is judged against that slower heartbeat plus its interval, as the lost-probe
    guard does, or a healthy remote probe would read as silent."""
    from hermes_cli import kanban_wake_check as kwc
    seen = float(rec.get("observed_at") or 0)
    try:
        row = conn.execute("SELECT last_at FROM task_wake_checks WHERE task_id = ?", (task_id,)).fetchone()
        seen = max(seen, float(row[0] or 0) if row else 0.0)
        alive = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = ?",
                             (task_id, kwc.ALIVE_EVENT)).fetchone()
        seen = max(seen, float(alive[0] or 0) if alive else 0.0)
    except sqlite3.Error:
        pass
    every = ks.clamp_every(every, SHELL_DEFAULT_EVERY)
    if not local:
        every = every + kwc.ALIVE_EVERY // 2
    return dict(rec, observed_at=seen, next_due_at=seen + ks.clamp_every(rec.get("every"), SHELL_DEFAULT_EVERY)), every


def card_state(conn: sqlite3.Connection, task_id: str, *, now: Optional[float] = None,
               hosts: Optional[set[str]] = None, status: Optional[str] = None) -> Optional[dict]:
    """The derived external state of one card (see ``kanban_external_state.derive``), or None: not a scheduled
    ci/deploy/external park, an old host's silence, or a deadline-only wait. Adds ``blocker_id`` / ``triage_card``
    to ``external`` while a service-down blocker holds the card."""
    now = time.time() if now is None else float(now)
    if status is None:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        status = row[0] if row else None
    if status != "scheduled":
        return None
    ctx = park_context(conn, task_id)
    if ctx is None or (ctx["awaiting"] or {}).get("kind") not in ks.EXTERNAL_KINDS:
        return None
    watch = plan(conn, task_id, ctx)
    rec = latest_record(conn, task_id, ctx["event_id"])
    probe_host = ctx["payload"].get("probe_host")
    local = not probe_host or probe_host == own_host(conn) or probe_host == "local"
    advertises = rec is not None or local or probe_host in (advertising_hosts(conn) if hosts is None else hosts)
    if rec is None and watch is None:
        derived = ks.derive(None, now=now, every=ks.DEFAULT_CI_EVERY, since=ctx["since"], watch_missing=local,
                            advertises=advertises)
    else:
        every = (rec or {}).get("every") or (watch or {}).get("every")
        if rec is not None and rec.get("adapter") == "shell":
            rec, every = _shell_seen(conn, task_id, rec, every, local)
        derived = ks.derive(rec, now=now, every=every, since=ctx["since"], advertises=advertises,
                            adapter=(rec or {}).get("adapter") or (watch or {}).get("adapter"),
                            service_name=(rec or {}).get("service_name"))
    if derived is not None:
        derived["adapter"] = derived["external"]["adapter"]
        _name_blocker(conn, derived, rec)
    return derived


def _name_blocker(conn: sqlite3.Connection, derived: dict, rec: Optional[dict]) -> None:
    if derived["state"] != ks.DOWN or not rec or not rec.get("adapter") or not rec.get("scope"):
        return
    if derived["health"] not in ("down", "unreachable", "auth_failed"):
        return
    bid = blocker_id(rec["adapter"], rec["scope"])
    try:
        from hermes_cli import kanban_shared_blockers as ksb
        st = ksb.blocker_states(conn).get(bid)
    except (sqlite3.Error, ValueError):
        return
    if st is not None and st.is_open:
        derived["external"]["blocker_id"] = bid
        derived["external"]["triage_card"] = next(iter(sorted(st.behind)), None)


def card_states(conn: sqlite3.Connection, task_ids: Iterable[str], *, now: Optional[float] = None) -> dict[str, dict]:
    """``{task id: derived state}`` for the scheduled external cards among ``task_ids``. Never raises."""
    out: dict[str, dict] = {}
    try:
        hosts = advertising_hosts(conn)
        for tid in dict.fromkeys(str(t) for t in task_ids):
            derived = card_state(conn, tid, now=now, hosts=hosts)
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
                         rand=random.random() if rand is None else rand, host=own_host(conn))
    name = service_name or poll.get("service_name") or ks.SERVICE_NAMES.get(adapter, "external service")
    derived = ks.derive(rec, now=now, every=every, adapter=adapter, service_name=name) or {}
    prev_written = (prev or {}).get("state")
    changed = (prev is None or prev_written != derived["state"] or prev.get("job") != rec["job"]
               or prev.get("service") != rec["service"])
    due = prev is None or (adapter != "shell" and now - float(prev.get("observed_at") or 0) >= 0.9 * every)
    # A shell probe's heartbeat is not an event per check (test_clean_checks_do_not_append_one_event_each): the
    # check's own ``task_wake_checks.last_at`` (this host) and ``wake_check_alive`` (a worker node, every
    # ALIVE_EVERY) carry it; ``_shell_seen`` reads them.
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
               cmd: str = "", terminal: Optional[bool] = None) -> Optional[str]:
    """Record one poll for a scheduled card and act on it: a finished job wakes the card with ``external_result``
    (``condition_met``; a FAILED job wakes too and is not an outage); a service gone red asserts the shared
    blocker; an ok poll clears it. Returns the event kind that woke the card, else None. Opens its own write txn."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc
    with kb.write_txn(conn):
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if status is None or status[0] != "scheduled":
            return None
        got = record_poll(conn, task_id, poll, now=now, adapter=adapter, scope=scope, every=every,
                          service_name=service_name, rand=rand)
        if got is None:
            return None
        derived, rec = got["derived"], got["rec"]
        finished = poll.get("terminal") if terminal is None else terminal
        if finished or derived.get("wake"):
            kwc.episode.reset(conn, task_id)
            woke = kwc._wake(conn, task_id, kwc.CONDITION_MET_EVENT, {
                "wake_check": cmd or None, "output": rec.get("detail") or "",
                "external_result": _result(rec, derived)})
            if woke:
                kb.add_comment(conn, task_id, AUTHOR, _finished_note(rec, derived))
            return woke
        if derived["state"] == ks.DOWN and derived["health"] in ("down", "unreachable") and scope:
            return _assert_blocker(conn, task_id, rec, derived)
        if derived["health"] in ("ok", "degraded") and scope:
            _clear_blocker(conn, rec)
        return None


def _finished_note(rec: dict, derived: dict) -> str:
    res = _result(rec, derived)
    where = f" ({res['url']})" if res.get("url") else ""
    return (f"Woken: the job on {res['service']} finished: {res['outcome']}{where}. "
            + ("A failed job is a result for this card, not an outage." if res["outcome"] == "failed" else ""))


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
    """A legacy wake-check probe's result as a watch record (``kanban_external_adapters.shell``), for a scheduled
    ci/deploy/external park. In the caller's write txn; never raises. Returns the ``external_result`` to carry on
    a wake (exit 0 or a ``HERMES_WATCH`` line with a terminal job), else None. A GitHub run/PR park is also
    recorded by the CI sweep (the richer adapter); the later record wins."""
    from hermes_cli.kanban_external_adapters import shell
    try:
        ctx = park_context(conn, task_id)
        if ctx is None or (ctx["awaiting"] or {}).get("kind") not in ks.EXTERNAL_KINDS:
            return None
        if (plan(conn, task_id, ctx) or {}).get("adapter") != "shell":
            return None  # a GitHub run/PR park is recorded by the CI sweep's richer adapter
        poll = shell.poll(rc, out)
        got = record_poll(conn, task_id, poll, now=now, adapter="shell", scope="", every=every,
                          service_name=poll.get("service_name"))
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        logger.warning("kanban external watch: could not record the probe result of %s", task_id, exc_info=True)
        return None
    if got is None or not poll.get("terminal"):
        return None
    result = _result(got["rec"], got["derived"])
    result["outcome"] = poll.get("outcome") or result["outcome"]
    return result
