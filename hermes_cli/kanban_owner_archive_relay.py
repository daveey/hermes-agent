"""Archive a relayed capability owner card from the Board worker node whose card it served. [daveey]

On a ``relay=home`` route (:mod:`hermes_cli.kanban_capability_routing`) the worker node parks the waiting card
and its home gateway opens the owner card there (``_local_owner``). That owner card exists only on the home
gateway's board. ``kanban_archive`` reads the local board, so from the waiting card's own run on the worker
node it answered ``no such task`` and the scope rule for owner cards never ran.

Now, when the local lookup misses and the worker's task has a ``relay=home`` route whose owner is that card,
the archive goes to the home over the same channel the route used: an event on the waiting card, which the
Board carries both ways (events are not fenced).

1. Worker node (:func:`forward`): append ``capability_owner_archive_requested`` (owner, requesting task, run id
   and run uid, a request uid) on the waiting card. Then wait, bounded, for the matching result event.
2. Home (:func:`apply_requests`, each dispatcher tick; the pull wakes the dispatcher): apply the same
   ``archive_scope_for`` rule as a local worker (a done or unclaimed owner card keyed
   ``capability-owner:<waiting>:...``, with ``<waiting>`` the requesting task or a card it created). Archive
   with the forwarded run on the ``archived`` event, then append ``capability_owner_archive_result`` with
   the outcome or the refusal text.
3. The worker returns that result verbatim. If the result does not arrive in time, it reports
   ``archive_pending``. The request stays queued, and the home still answers it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

REQUEST_KIND = "capability_owner_archive_requested"
RESULT_KIND = "capability_owner_archive_result"
WAIT_S = 60.0
POLL_S = 1.0
VIA = "forwarded capability owner archive"


def _sleep(seconds: float) -> None:  # tests replace this with a fake Board round trip
    time.sleep(seconds)


def _events(conn: sqlite3.Connection, task_id: str, kind: str) -> list[dict]:
    out = []
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                               (task_id, kind)):
        try:
            p = json.loads(raw or "{}")
        except ValueError:
            continue
        if isinstance(p, dict):
            out.append(p)
    return out


def relayed_owner_route(conn: sqlite3.Connection, env_tid: str, owner_id: str) -> Optional[dict]:
    """``{waiting, gateway, request_id}`` when ``owner_id`` is the owner card of a ``relay=home`` route of
    ``env_tid`` or a card it created on this board; None otherwise (the miss stays ``no such task``)."""
    from hermes_cli import kanban_capability_routing as kcr

    candidates = [env_tid]
    for (tid, raw) in conn.execute("SELECT task_id, payload FROM task_events WHERE kind = 'created'"):
        try:
            if json.loads(raw or "{}").get("creator_task_id") == env_tid:
                candidates.append(tid)
        except (ValueError, AttributeError):
            continue
    for waiting in candidates:
        routes = [r for p in _events(conn, waiting, kcr.ROUTED_EVENT) for r in (p.get("routes") or [])
                  if isinstance(r, dict)]
        for r in routes:
            if r.get("owner_task") != owner_id or not r.get("request_id"):
                continue
            rid = r["request_id"]
            if kcr._task_of(rid) != waiting:
                continue
            home = next((x for x in routes if x.get("request_id") == rid and x.get("relay") == kcr.RELAY_HOME), None)
            if home is None:
                continue
            return {"waiting": waiting, "gateway": home.get("gateway") or r.get("gateway"), "request_id": rid}
    return None


def _outbox_rows(conn: sqlite3.Connection) -> Optional[int]:
    try:
        return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_events'").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def _result(conn: sqlite3.Connection, waiting: str, uid: str) -> Optional[dict]:
    return next((p for p in _events(conn, waiting, RESULT_KIND) if p.get("request_uid") == uid), None)


def forward(conn: sqlite3.Connection, route: dict, owner_id: str, *, requester: str, run_id: Optional[int],
            wait_s: float = WAIT_S) -> dict:
    """Send the archive of ``owner_id`` to the route's home gateway and wait for its answer.

    Returns the home's result payload (``ok`` True with ``status``, or ``ok`` False with ``error`` verbatim), or
    ``{"ok": True, "status": "archive_pending", ...}`` when no answer arrived within ``wait_s``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli import kanban_capability_routing as kcr
    from hermes_cli.kanban_remote_stop import _run_uid

    waiting, gateway = route["waiting"], route["gateway"]
    uid = uuid.uuid4().hex
    try:
        synced = bool(sync.sync_state(conn))
    except Exception:  # health: allow BLE001 -- an unreadable sync state is an unsynced board
        synced = False
    try:
        run_uid = _run_uid(conn, requester)
    except sqlite3.OperationalError:  # an unsynced board has no run uids
        run_uid = None
    payload: dict[str, Any] = {
        "owner_task": owner_id, "requested_by_task": requester, "run_id": run_id,
        "run_uid": run_uid, "request_id": route["request_id"], "gateway": gateway,
        "request_uid": uid, "from_gateway": kcr._own_gateway(conn),
    }
    with write_txn(conn):
        before = _outbox_rows(conn)
        # Run ids are per board file: the forwarded run rides in the payload, not the event's run_id column.
        kb._append_event(conn, waiting, REQUEST_KIND, payload)
        if synced and (before is None or (_outbox_rows(conn) or 0) <= before):
            conn.execute("DELETE FROM task_events WHERE id = (SELECT MAX(id) FROM task_events WHERE task_id = ? "
                         "AND kind = ?)", (waiting, REQUEST_KIND))
            return {"ok": False, "error": f"cannot archive {owner_id}: it is the capability owner card of {waiting} "
                                          f"on {gateway}, and this board cannot forward the request there. "
                                          "Nothing changed."}
    deadline = time.monotonic() + wait_s
    while True:
        res = _result(conn, waiting, uid)
        if res is not None:
            return {k: v for k, v in res.items() if k != "request_uid"}
        if time.monotonic() >= deadline:
            return {"ok": True, "status": "archive_pending", "forwarded_to": gateway, "request_uid": uid,
                    "message": f"forwarded to {gateway}; it archives {owner_id} on its next dispatcher pass"}
        _sleep(POLL_S)


def _home_of(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    from hermes_cli import kanban_capability_routing as kcr
    return kcr.card_home(conn, task_id)


def apply_requests(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Home side (each dispatcher tick): answer every forwarded owner archive this gateway has not answered.

    Only the waiting card's home answers (a worker node's copy has a home elsewhere). Returns the ids archived.
    """
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn

    from hermes_cli.kanban_idle_candidates import candidate_task_ids
    # The task/event set is structural; home authority and answered UIDs below
    # are still checked every tick, including changes in remote replicas.
    rows = candidate_task_ids(conn, ('archived',), (REQUEST_KIND,), exclude_statuses=True)
    done: list[str] = []
    for waiting in rows:
        if _home_of(conn, waiting) is not None:
            continue
        answered = {p.get("request_uid") for p in _events(conn, waiting, RESULT_KIND)}
        for req in _events(conn, waiting, REQUEST_KIND):
            uid = req.get("request_uid")
            if not uid or uid in answered:
                continue
            answered.add(uid)
            owner, requester = str(req.get("owner_task") or ""), str(req.get("requested_by_task") or "")
            try:
                result = _apply_one(conn, kcli, kb, waiting, owner, requester, req, signal_fn=signal_fn)
            except Exception as exc:  # health: allow BLE001 -- one request must not stop the dispatcher tick
                _log.warning("kanban: forwarded archive of %s failed", owner, exc_info=True)
                result = {"ok": False, "error": f"cannot archive {owner}: {exc}"}
            if result.get("ok"):
                done.append(owner)
            with write_txn(conn):
                kb._append_event(conn, waiting, RESULT_KIND, {**result, "request_uid": uid, "task_id": owner})
    return done


def _apply_one(conn, kcli, kb, waiting: str, owner: str, requester: str, req: dict, *, signal_fn) -> dict:
    if not owner or not requester:
        return {"ok": False, "error": "forwarded archive request without owner_task or requested_by_task"}
    task = kb.get_task(conn, owner)
    if task is None:
        return {"ok": False, "error": f"no such task: {owner}"}
    if kcli._capability_owner_waiting_card(task) != waiting:
        return {"ok": False, "error": f"cannot archive {owner}: it is not a capability owner card of {waiting}. "
                                      "Only an owner card of the card the request came from is archived this way."}
    refusal, via = kcli.archive_scope_for(conn, owner, requester)
    if refusal:
        return {"ok": False, "error": refusal}
    extra = {"forwarded_from": req.get("from_gateway") or None, "forwarded_task": requester,
             "forwarded_run_id": req.get("run_id"), "forwarded_run_uid": req.get("run_uid")}
    extra = {k: v for k, v in extra.items() if v is not None}
    if not kb.archive_task(conn, owner, via=f"{via} ({VIA})" if via else VIA, signal_fn=signal_fn,
                           event_extra=extra):
        return {"ok": False, "error": f"cannot archive {owner}"}
    _log.info("kanban: archived capability owner %s forwarded by %s run %s", owner, requester, req.get("run_id"))
    return {"ok": True, "status": "archived"}


def worker_archive(conn: sqlite3.Connection, tid: str, *,
                   run_id_fn: Callable[[str], Optional[int]]) -> tuple[Optional[str], Optional[dict]]:
    """``kanban_archive`` from a worker or an orchestrator: ``(refusal, result)``.

    A ``relay=home`` owner card this board lacks is forwarded home (:func:`forward`). Any other card gets the local
    scope rule, then :func:`hermes_cli.kanban_remote_stop.archive`: a card another machine runs under its live
    Board lease gets a stop request, since the Board would reject the status write (live_claim). A card this
    gateway has only as a replica copy is forwarded by the authenticated creator_archive courier only for
    its exact creator task; unscoped callers still raise ForeignCardError.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban import worker_archive_scope
    from hermes_cli.kanban_remote_stop import archive

    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != tid and kb.get_task(conn, tid) is None:
        route = relayed_owner_route(conn, env_tid, tid)
        if route is not None:
            out = forward(conn, route, tid, requester=env_tid, run_id=run_id_fn(env_tid))
            if not out.pop("ok", False):
                return str(out.get("error") or f"cannot archive {tid}"), None
            out.pop("task_id", None)
            return None, out
    from hermes_cli import kanban_foreign_card as kfc
    if env_tid and kfc.owner_of(conn, tid)[0]:
        from hermes_cli.kanban_creator_archive import forward as forward_creator
        return forward_creator(conn, tid, env_tid, run_id_fn(env_tid))
    kfc.check(conn, tid)  # Unscoped callers retain the foreign-card refusal.
    refusal, via = worker_archive_scope(conn, tid)
    if refusal:
        return refusal, None
    out = archive(conn, tid, via=via)
    if out and out.get("status") == "archived":
        from hermes_cli.kanban_placement_forward import archive_forward_view
        placed = archive_forward_view(conn, tid)  # [daveey] the placed copy on another gateway, if any
        if placed:
            out = {**out, "placed_copy": placed}
    return None, out


def apply_remote_archives(conn: sqlite3.Connection) -> list[str]:
    """Dispatcher tick: apply stop requests (:mod:`hermes_cli.kanban_remote_stop`) and forwarded owner archives
    (:func:`apply_requests`), then send the archives of placed copies whose remote id has resolved since their card
    was archived (:func:`hermes_cli.kanban_placement_forward.sweep_unresolved_archives`). Returns the ids archived
    here; a failing source is logged and skipped."""
    from hermes_cli.kanban_remote_stop import STOP_KIND, apply_stop_requests
    from hermes_cli.kanban_idle_candidates import event_candidates

    done: list[str] = []
    sources = (
        ('remote stop requests', apply_stop_requests, ('running',), (STOP_KIND,)),
        ('forwarded owner archives', apply_requests,
         ('archived',), (REQUEST_KIND,)),
    )
    for label, apply, statuses, kinds in sources:
        try:
            if event_candidates(conn, statuses, kinds, exclude_statuses=apply is apply_requests):
                done += apply(conn)
        except Exception:  # health: allow BLE001 -- one failing source must not stop the dispatcher tick
            _log.warning("kanban: applying %s failed", label, exc_info=True)
    # [daveey] Placed copies whose remote id was unknown when their card was archived here: archive them there now.
    try:
        from hermes_cli.kanban_placement_forward import ARCHIVE_FORWARDED, CANCEL_INTENT, sweep_unresolved_archives
        if event_candidates(conn, ('done', 'archived'), (ARCHIVE_FORWARDED, CANCEL_INTENT)):
            for tid in sweep_unresolved_archives(conn):
                _log.info("kanban: sent the deferred archive of %s's placed copy", tid)
    except Exception:  # health: allow BLE001 -- one failing source must not stop the dispatcher tick
        _log.warning("kanban: sweeping unresolved placement archives failed", exc_info=True)
    return done
