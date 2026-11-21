"""Deferred placement: a card with parents runs on the routed gateway once its parents finish. [daveey]

:func:`hermes_cli.kanban_placement.decide` keeps a card with parents here (parents are local links),
but when nothing else pins it to this machine it answers ``defer=<target>`` and ``kanban_create``
records a ``placement_deferred`` event (:func:`record_deferral`). The card waits in ``todo`` like any
child. When its parents finish and the dispatcher claims it, ``_default_spawn`` runs this module
instead of an LLM worker (:func:`forward_argv`); it costs seconds, not a worker:

1. Forward: create the card on the target through ``hermes-hub create-task`` (the same origin-bound
   path :func:`kanban_placement.create_remote` uses, so it shows in the chat that asked for it), with
   the parents' handoffs in the body. Then park the local card ``scheduled`` with a wake probe on the
   target's card (``hermes kanban wait-check task <id> --gateway <target> --until done,archived``).
   The local card stays open, so local cards that depend on it keep waiting for the real work.
2. Settle (woken by that probe): the target's card is done -> complete the local card with a pointer
   to it, which releases the local children. Archived there -> block here for a decision. Anything
   else -> park again.

Any failure before the target accepted the card (target not live for :data:`MAX_NOT_LIVE` attempts,
refused, an exception) cancels the deferral (``placement_deferred_cancelled``) and ``exec``s the
normal worker command, so the card still runs, here. Never a silent stall. That includes a request the
target rejected after it was queued (a hub ``rejected``/``expired``/``failed`` state): it is never parked
on. The forward names the local card in ``distinct_from`` (the target sees it through the replica and
would refuse its own copy as an overlap); a ``create_overlap`` naming only this card is re-forwarded
once, under a new request id; an overlap with another card blocks this one for a decision.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone, UTC
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFERRED = "placement_deferred"
FORWARDED = "placement_forwarded"
CANCELLED = "placement_deferred_cancelled"
NOT_LIVE = "placement_forward_not_live"
ARCHIVE_FORWARDED = "placement_archive_forwarded"
MAX_NOT_LIVE = 3
REQUEUE_SECONDS = 600          # retry a not-live target / a still-queued request after this long
SETTLE_PARK_SECONDS = 14 * 86400  # timer backstop of the settle park; the wake probe ends it first
CHECK_EVERY = 300
AWAIT_BUDGET = 45.0
CONFIRM_RETRIES = 2            # confirm a just-forwarded card this many extra times before parking unconfirmed
CONFIRM_RETRY_DELAY = 3.0
ARCHIVE_AWAIT_BUDGET = 5.0     # archive: wait this long for the target's card id before recording ``unresolved``
ARCHIVE_SWEEP_GIVE_UP = 7 * 86400  # the dispatcher sweep stops retrying an ``unresolved`` archive after this
CANCEL_INTENT = "placement_cancel_intent"  # written in the terminal txn: the placed copy must be cancelled
INTENT_GRACE = 60.0            # an intent with no forward event this long after it = the forward never ran (crash)
FAILED_RETRY_BASE = 300.0      # a ``failed`` archive send is retried after 300 s, 600 s, ... capped at FAILED_RETRY_MAX
FAILED_RETRY_MAX = 6 * 3600.0
UNTIL = ("done", "archived")
MODULE = "hermes_cli.kanban_placement_forward"


# --- state -------------------------------------------------------------------------------------

def record_deferral(conn: sqlite3.Connection, task_id: str, *, target: str, origin_session: Optional[str],
                    origin_profile: str, origin_gateway: Optional[str] = None) -> None:
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, DEFERRED, {
            "target": target, "origin_session": origin_session or None, "origin_profile": origin_profile,
            **({"origin_gateway": origin_gateway} if origin_gateway else {})})


def state(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The active deferral of ``task_id`` (``target``, origin, and once forwarded ``request_id`` /
    ``remote_task_id``), or None when there is none or it was cancelled."""
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN (?, ?, ?, ?) ORDER BY id",
        (task_id, DEFERRED, FORWARDED, CANCELLED, NOT_LIVE)).fetchall()
    out: Optional[dict] = None
    for kind, raw in rows:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {}
        if kind == DEFERRED:
            out = {**payload, "not_live": 0}
        elif kind == CANCELLED:
            out = None
        elif out is not None and kind == FORWARDED:
            out.update({k: v for k, v in payload.items() if v})
        elif out is not None and kind == NOT_LIVE:
            out["not_live"] = int(out.get("not_live") or 0) + 1
    return out if out and out.get("target") else None


def forward_argv(task, board: Optional[str], worker_cmd: list) -> Optional[list]:
    """The command the dispatcher runs for ``task`` instead of ``worker_cmd`` when its placement is
    deferred, else None. Review-lane spawns and any error return None (the normal worker). Never raises."""
    try:
        if "sdlc-review" in (getattr(task, "skills", None) or []):
            return None
        from hermes_cli import kanban_db as kb
        path = kb.kanban_db_path(board=board)
        if not os.path.exists(path):
            return None
        from hermes_cli import kanban_lockguard
        conn = kanban_lockguard.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            st = state(conn, task.id)
        finally:
            conn.close()
        if not st:
            return None
        # ``-P``: the forwarder runs with cwd = the task workspace; see ``hermes_module_argv``.
        from cron.scheduler_worker_env import hermes_module_argv
        return [*hermes_module_argv(sys.executable, MODULE), "--", *[str(x) for x in worker_cmd]]
    except Exception:
        return None


# --- lifecycle: archive the placed copy ---------------------------------------------------------

def _archive_request_id(self_gw: str, task_id: str) -> str:
    return f"placement-archive:{self_gw}:{task_id}"


def _archive_events(conn: sqlite3.Connection, task_id: str) -> list:
    out = []
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                               (task_id, ARCHIVE_FORWARDED)).fetchall():
        try:
            out.append(json.loads(raw or "{}"))
        except ValueError:
            continue
    return out


def record_cancel_intent(conn: sqlite3.Connection, task_id: str, reason: str, *, settled: bool = False) -> None:
    """Inside the caller's terminal write txn (archive / manual completion): record that the placed copy of
    ``task_id`` must be cancelled. Committed atomically with the status flip, so a crash before the forward runs
    leaves the intent for :func:`sweep_unresolved_archives` to replay. No-op for a card never forwarded, and for
    ``settled`` (the forwarder completed it because the copy there is done: nothing to cancel). Never
    raises: a broken placement record must not roll back the archive/completion it rides on."""
    if settled:
        return
    try:
        st = state(conn, task_id)
        if not st or not (st.get("request_id") or st.get("remote_task_id")):
            return
        from hermes_cli import kanban_db as kb
        kb._append_event(conn, task_id, CANCEL_INTENT, {
            "reason": reason, "gateway": st.get("target"), "request_id": st.get("request_id"),
            "remote_task_id": st.get("remote_task_id")})
    except Exception as exc:  # health: allow BLE001 -- the post-commit forward still runs
        print(f"placement cancel intent for {task_id} not recorded: {exc!r}", file=sys.stderr)


def archive_forward_view(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """What ``kanban_archive`` reports about the placed copy of ``task_id``: ``{gateway, task_id, status}`` with
    status ``archived`` (the target applied the request), ``pending`` (queued or sent, not applied yet) or
    ``unresolved`` (the target had not made the card when this one was archived), else None (never forwarded)."""
    events = _archive_events(conn, task_id)
    if not events:
        return None
    last = events[-1]
    status = last.get("status") or "pending"
    rid = last.get("request_id")
    if status == "pending" and rid:
        from hermes_cli import kanban_placement as kp
        if kp.remote_state(rid) == "done":
            status = "archived"
    view = {"gateway": last.get("gateway"), "task_id": last.get("remote_task_id"), "status": status}
    if last.get("error"):
        view["error"] = last["error"]
    if last.get("note"):
        view["note"] = last["note"]
    return view


def _resolve_remote_id(st: dict, budget: float) -> Optional[str]:
    """The target's card id for the forward request in ``st``: the recorded one, else the replica's, else (when
    ``budget`` > 0) what the target answers within ``budget`` seconds (:func:`kanban_placement.await_task_id`)."""
    from hermes_cli import kanban_placement as kp
    rid = st.get("request_id")
    rtid = st.get("remote_task_id") or (kp.remote_task_id(rid) if rid else None)
    if rtid or not rid or budget <= 0:
        return rtid
    try:
        from hermes_cli import kanban_capability_registry as reg
        rtid, _status = kp.await_task_id(rid, hub_cli=reg.hub_cli_path(kp.config().get("hub_cli")), budget=budget)
    except Exception as exc:  # health: allow BLE001 -- unresolved is reported, and the sweep retries it
        print(f"placement archive forward: resolving {rid} failed: {exc!r}", file=sys.stderr)
        return None
    return rtid


def forward_archive(conn: sqlite3.Connection, task_id: str, *, runner: Optional[Callable] = None,
                    await_budget: Optional[float] = None, record_unresolved: bool = True) -> Optional[dict]:
    """Archive the copy of ``task_id`` that placement forwarded to another gateway. Never raises.

    Sends ``hermes-hub request <gw> tasks default/<remote id> archive``: the hub client queues it durably
    (rc 3 = target or hub unreachable, applied when it reconnects). Records ``placement_archive_forwarded``
    on the local card and returns :func:`archive_forward_view`. None when the card was never forwarded.

    When the target has not made the card yet (request sent, no id), it waits up to ``await_budget`` seconds
    (default :data:`ARCHIVE_AWAIT_BUDGET`) for the id, then records ``unresolved``; the dispatcher sweep
    (:func:`sweep_unresolved_archives`) sends the archive once the id arrives. ``record_unresolved=False`` (the
    sweep) records nothing and returns None while the id is still unknown."""
    try:
        st = state(conn, task_id)
        if not st or not (st.get("request_id") or st.get("remote_task_id")):
            return None
        prior = _archive_events(conn, task_id)
        if prior and prior[-1].get("status") in ("pending", "archived", "expired"):
            return archive_forward_view(conn, task_id)  # one request per card
        from hermes_cli import kanban_capability_registry as reg
        from hermes_cli import kanban_placement as kp
        target = st["target"]
        rtid = _resolve_remote_id(st, ARCHIVE_AWAIT_BUDGET if await_budget is None else await_budget)
        if not rtid and not record_unresolved:
            return None
        payload: dict = {"gateway": target, "request_id": None, "remote_task_id": rtid, "status": "pending"}
        if not rtid:
            payload.update(status="unresolved", note=(
                f"{target} had not made the card for request {st.get('request_id')} yet; the dispatcher archives "
                f"the copy there once {target} makes it (gives up after {ARCHIVE_SWEEP_GIVE_UP // 86400} days)."))
        else:
            cfg = kp.config()
            self_gw = reg.self_gateway() or "local"
            rid = _archive_request_id(self_gw, task_id)
            argv = [reg.hub_cli_path(cfg.get("hub_cli")), "request", target, "tasks", f"default/default/{rtid}",
                    "archive", "--id", rid]
            rc, out, err = (runner or kp.hub_runner)(argv)
            payload["request_id"] = rid
            if rc not in (0, 3):  # 3: queued, sent on the next sync
                payload.update(status="failed", error=(err or out or f"rc {rc}")[-300:])
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, ARCHIVE_FORWARDED, payload)
        return archive_forward_view(conn, task_id)
    except Exception as exc:  # health: allow BLE001 -- an archive must never fail on the courtesy forward
        print(f"placement archive forward failed: {exc!r}", file=sys.stderr)
        _record_failure(conn, task_id, exc)
        return None


def _record_failure(conn: sqlite3.Connection, task_id: str, exc: BaseException) -> None:
    """Best effort: a send that raised is ``failed`` (retried by the sweep with backoff), not silently lost."""
    try:
        st = state(conn, task_id)
        if not st:
            return
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, ARCHIVE_FORWARDED, {
                "gateway": st.get("target"), "request_id": None, "remote_task_id": st.get("remote_task_id"),
                "status": "failed", "error": repr(exc)[-300:]})
    except Exception as exc2:  # health: allow BLE001 -- the cancel intent still drives the sweep's replay
        print(f"placement archive forward: recording the failure failed: {exc2!r}", file=sys.stderr)


def _last_event(conn: sqlite3.Connection, task_id: str, kind: str) -> Optional[tuple]:
    """``(event id, payload, created_at)`` of the newest ``kind`` event of ``task_id``, else None."""
    row = conn.execute("SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind = ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, kind)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[1] or "{}")
    except ValueError:
        payload = {}
    return int(row[0]), payload if isinstance(payload, dict) else {}, float(row[2] or 0)


def _first_event_ts(conn: sqlite3.Connection, task_id: str, kind: str) -> float:
    row = conn.execute("SELECT MIN(created_at) FROM task_events WHERE task_id = ? AND kind = ?",
                       (task_id, kind)).fetchone()
    return float(row[0] or 0) if row else 0.0


def _failures_since(conn: sqlite3.Connection, task_id: str, after_id: int) -> int:
    n = 0
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND id > ?",
                               (task_id, ARCHIVE_FORWARDED, after_id)).fetchall():
        try:
            n += json.loads(raw or "{}").get("status") == "failed"
        except (ValueError, AttributeError):
            continue
    return n


def _give_up(conn: sqlite3.Connection, task_id: str, payload: dict, gw: str, why: str) -> None:
    from hermes_cli import kanban_db as kb
    days = ARCHIVE_SWEEP_GIVE_UP // 86400
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, ARCHIVE_FORWARDED, {
            **payload, "gateway": gw, "status": "expired",
            "note": f"{why} for {days} days; stopped retrying the archive."})
    kb.add_comment(conn, task_id, "placement",
                   f"Placement archive forward gave up: {why} for {days} days after this card was closed, so no "
                   f"copy was archived there. If one appears later, archive it with `hermes-hub tasks --gw {gw}`.")


def _sent(view: Optional[dict]) -> bool:
    return bool(view) and view.get("status") in ("pending", "archived")


def sweep_unresolved_archives(conn: sqlite3.Connection, *, now: Optional[float] = None,
                              runner: Optional[Callable] = None) -> list[str]:
    """Dispatcher tick: drive the cancellation of every archived/done card's placed copy to a closed state.

    * ``unresolved`` (the target had not made the card): send the archive once the target's card id reached this
      gateway's hub replica (no waiting here: the tick reads the replica only).
    * ``failed`` (the send was refused or raised): resend after :data:`FAILED_RETRY_BASE` seconds, doubling per
      failure up to :data:`FAILED_RETRY_MAX`.
    * a :data:`CANCEL_INTENT` with no forward event after it, older than :data:`INTENT_GRACE`: the process died
      between the terminal commit and the forward. Replay it: an archive forwards the archive, a manual completion
      reruns :func:`forward_completion` (a copy already done/archived there is left alone).

    ``pending``/``archived``/``expired``/``skipped`` are closed. An open cancellation that still fails one more
    try :data:`ARCHIVE_SWEEP_GIVE_UP` after its intent (legacy cards without one: after their first forward event)
    gets an ``expired`` event and a comment, and is not swept again. Resends reuse the card's hub request id
    (:func:`_archive_request_id`), so a replay racing the original forward is applied once. Returns the ids whose
    archive request was sent."""
    now = time.time() if now is None else now
    # [daveey] G2: the candidate set is structural (status + event kinds), memoized on the task/event revision; the
    # per-card retry, grace and give-up clocks below still run every tick.
    from hermes_cli.kanban_idle_candidates import candidate_task_ids
    rows = [(tid,) for tid in candidate_task_ids(conn, ("archived", "done"), (ARCHIVE_FORWARDED, CANCEL_INTENT))]
    sent: list[str] = []
    for (task_id,) in rows:
        intent = _last_event(conn, task_id, CANCEL_INTENT)
        last = _last_event(conn, task_id, ARCHIVE_FORWARDED)
        status = last[1].get("status") if last else None
        if status in ("pending", "archived", "expired"):
            continue  # closed; one archive request per card covers a later intent too
        if intent and (last is None or last[0] < intent[0]):
            # Crash gap: the terminal txn committed, the forward recorded nothing after it. Always try first: a
            # dispatcher that was down past the give-up still sends once it is back.
            age = now - intent[2]
            if age < INTENT_GRACE:
                continue  # the closing process may still be forwarding
            if intent[1].get("reason") == "completed":
                view = forward_completion(conn, task_id, runner=runner)
            else:
                view = forward_archive(conn, task_id, runner=runner, await_budget=0)
            if _sent(view):
                sent.append(task_id)
            elif age >= ARCHIVE_SWEEP_GIVE_UP and _last_event(conn, task_id, ARCHIVE_FORWARDED) == last:
                gw = intent[1].get("gateway") or "the target"
                _give_up(conn, task_id, {}, gw, f"the cancellation of the copy on {gw} never ran")
            continue
        if last is None or status not in ("unresolved", "failed"):
            continue  # ``skipped``: the copy there was already terminal
        payload = last[1]
        gw = payload.get("gateway") or "the target"
        # The clock never moves with retries: the intent, else (legacy) the first forward event.
        clock = intent[2] if intent else _first_event_ts(conn, task_id, ARCHIVE_FORWARDED)
        expired = now - clock >= ARCHIVE_SWEEP_GIVE_UP
        if status == "failed":
            n = _failures_since(conn, task_id, intent[0] if intent else 0)
            if not expired and now - last[2] < min(FAILED_RETRY_BASE * 2 ** max(n - 1, 0), FAILED_RETRY_MAX):
                continue
            if _sent(forward_archive(conn, task_id, runner=runner, await_budget=0)):
                sent.append(task_id)
            elif expired:  # one last try failed too
                _give_up(conn, task_id, payload, gw, f"sending the archive to {gw} kept failing ({n + 1} times)")
            continue
        view = forward_archive(conn, task_id, runner=runner, await_budget=0, record_unresolved=False)
        if _sent(view):
            sent.append(task_id)
            continue
        if view is not None:
            continue  # the id arrived but the send failed: recorded ``failed``, the backoff path retries it
        if expired:
            _give_up(conn, task_id, payload, gw,
                     f"{gw} never made the card for request {payload.get('request_id') or '?'}")
    return sent


def forward_completion(conn: sqlite3.Connection, task_id: str, *, runner: Optional[Callable] = None,
                       settled: bool = False) -> Optional[dict]:
    """Manual origin completion cancels outstanding placed work, never completes it as successful.

    Reuse archive's durable queue and audit event. Unknown/offline observations still send;
    only a confirmed terminal copy is left untouched (recorded ``skipped``, which closes the
    completion's cancel intent). Called after local completion commits, and by the sweep's replay.
    ``settled`` (the forwarder's own completion: the copy there is done) forwards nothing.
    """
    if settled:
        return None
    try:
        st = state(conn, task_id)
        if not st or not (st.get("request_id") or st.get("remote_task_id")):
            return None
        from hermes_cli import kanban_placement as kp
        from hermes_cli import kanban_wait_replica as replica
        rtid = st.get("remote_task_id") or kp.remote_task_id(st["request_id"])
        if rtid:
            try:
                # Local fresh snapshot only: do not hold completion/child release on sleeping-host SSH.
                rc, out = replica.check(st["target"], rtid, until=UNTIL)
                words = (out or "").split()
                if rc == 0 and len(words) >= 2 and words[0] == rtid and words[1] in UNTIL:
                    from hermes_cli import kanban_db as kb
                    with kb.write_txn(conn):  # closes the cancel intent: nothing to cancel there
                        kb._append_event(conn, task_id, ARCHIVE_FORWARDED, {
                            "gateway": st["target"], "request_id": None, "remote_task_id": rtid,
                            "status": "skipped", "note": f"{rtid} was already {words[1]} on {st['target']}"})
                    return None
            except Exception as exc:  # health: allow BLE001 -- unavailable observation must not prevent durable cancellation
                print(f"placement completion observation failed; forwarding archive: {exc!r}", file=sys.stderr)
        return forward_archive(conn, task_id, runner=runner, await_budget=0)
    except Exception as exc:  # health: allow BLE001 -- courtesy forwarding must not undo a committed completion
        print(f"placement completion forward failed: {exc!r}", file=sys.stderr)
        _record_failure(conn, task_id, exc)
        return None


# --- the forwarder -----------------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wake_probe(target: str, remote_tid: str) -> str:
    return shlex.join(["hermes", "kanban", "wait-check", "task", remote_tid, "--gateway", target,
                       "--until", ",".join(UNTIL)])


def _quoted(text: str) -> str:
    """Parent text safe inside the evidence fence: a close marker in it would end the fence early and expose
    the rest of the handoff to the placement scans."""
    from hermes_cli.kanban_placement import EVIDENCE_CLOSE
    return text.replace(EVIDENCE_CLOSE, "<!-- /placement-evidence (quoted) -->")


def _own_text(body: str) -> str:
    """The card's own body, safe above the footer's fence: an open marker after its last close marker is
    unclosed and would pair with the footer's close, so ``without_evidence`` would strip the card's own
    text (its home commands included) up to the footer. Neutralise those; complete fences stay."""
    from hermes_cli.kanban_placement import EVIDENCE_CLOSE, EVIDENCE_OPEN
    cut = body.rfind(EVIDENCE_CLOSE)
    cut = 0 if cut < 0 else cut + len(EVIDENCE_CLOSE)
    return body[:cut] + body[cut:].replace(EVIDENCE_OPEN, "<!-- placement-evidence (quoted) -->")


def opened_board(conn: sqlite3.Connection) -> str:
    """Slug of the board file ``conn`` opened. ``connect_closing()`` resolves ``HERMES_KANBAN_DB`` before
    ``HERMES_KANBAN_BOARD`` but ``get_current_board()`` ignores the DB pin, so read the slug off the file;
    a pinned file outside the board layout (or no file) falls back to the active board."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    path = sync._db_key(conn)
    if path:
        slug = sync.board_slug_for_path(path)
        if slug != kb.DEFAULT_BOARD or Path(path).resolve() == kb.board_own_db_path(None).resolve():
            return slug
    return kb.get_current_board()


def remote_args(conn: sqlite3.Connection, task, *, self_gw: str, board: Optional[str] = None) -> dict:
    """The ``kanban_create`` view of the local card sent to the target: its options, and the body with
    the parents' handoffs (the target cannot read this board). ``board`` is the source card's board slug
    (default: the board ``conn`` opened), named in the footer's read target."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_placement import EVIDENCE_CLOSE, EVIDENCE_OPEN
    from hermes_cli.kanban_model_neutral import forwarded_text, routing_view, validate_text
    task = routing_view(task)
    title = task.title
    title_evidence = ''
    try:
        validate_text(title)
    except ValueError:
        title = f"Continue forwarded work {task.id}"
        title_evidence = '\n\nHistorical source title:\n' + forwarded_text(task.title)
    parents = kb.parent_ids(conn, task.id)
    summaries = kb.latest_summaries(conn, parents) if parents else {}
    lines = []
    for pid in parents:
        p = kb.get_task(conn, pid)
        handoff = (summaries.get(pid) or (p.result if p else None) or "(no handoff)").strip()
        lines.append(forwarded_text(_quoted(f"- {pid} ({p.title if p else '?'}): {handoff[:1500]}")))
    body = forwarded_text(_own_text((task.body or "").rstrip())) + title_evidence
    if lines:
        board = board or opened_board(conn)
        # Spell the tool's read call: "with cross_task @task:..." parses as an unknown explicit invocation in
        # kanban_placement_coordination._actions (ACTION_REASON), which kept such forwarded cards on the
        # target's home instead of the pool (t_01a1d49003c65ca4). A read only needs coordination-read.
        # The parents' titles and handoffs are quoted evidence of work done on this gateway, not this card's
        # work: fence them so a handoff describing `with cross_task x` or `hermes-hub restart` (or the hub,
        # ssh, ...) does not keep the forwarded card home (t_01a1d4bd40dca767). The worker still reads them.
        body += (f"\n\n---\nParent handoffs (the parents ran on {self_gw}; read them there with "
                 f"cross_task(action='read', target='@task:{board}@{self_gw}/<id>')):\n"
                 f"{EVIDENCE_OPEN}\n" + "\n".join(lines) + f"\n{EVIDENCE_CLOSE}")
    body += f"\n\nThis card was {self_gw} card {task.id}; it completes there when this one is done."
    return {
        "title": title, "body": body, "assignee": task.assignee or "default",
        "skills": task.skills or None, "requires": task.requires or None,
        "max_runtime_seconds": task.max_runtime_seconds, "priority": task.priority or None,
        "model": task.model_override, "provider": task.provider_override if task.model_override else None,
        "goal_mode": bool(task.goal_mode), "goal_max_turns": task.goal_max_turns if task.goal_mode else None,
        "completion_contract": task.completion_contract,
        "review_required": bool(task.review_required),

    }


class Forwarder:
    """One forwarder run for one claimed card. The collaborators are attributes so tests swap them."""

    def __init__(self, conn: sqlite3.Connection, task_id: str, run_id: Optional[int], *,
                 now: Optional[Callable[[], float]] = None):
        from hermes_cli import kanban_capability_registry as reg
        from hermes_cli import kanban_placement as kp
        from hermes_cli import kanban_wait_check as kwait
        self.conn, self.task_id, self.run_id = conn, task_id, run_id
        self.now = now or time.time
        self.sleep = time.sleep
        self.cfg = kp.config()
        self.self_gw = reg.self_gateway() or "local"
        self.board = opened_board(conn)  # the board whose DB main() opened, HERMES_KANBAN_DB pin included
        self.hub_cli = reg.hub_cli_path(self.cfg.get("hub_cli"))
        self.live = lambda t: kp.target_live(t, now=self.now(), live_seconds=kp._Cfg(self.cfg).live_seconds())
        self.create = kp.create_remote
        self.await_id = kp.await_task_id
        self.remote_task_id = kp.remote_task_id
        self.remote_state = kp.remote_state
        self.rejection = kp.remote_rejection
        self.carries_options = lambda: kp.hub_carries_options(self.hub_cli)
        self.check = lambda gw, tid: kwait.check_remote(gw, "task", tid, until=UNTIL)

    # Each step returns a short action word; "fallback" means: exec the normal worker.
    def run(self) -> str:
        from hermes_cli import kanban_db as kb
        task = kb.get_task(self.conn, self.task_id)
        st = state(self.conn, self.task_id)
        if task is None or st is None:
            return "fallback"
        target = st["target"]
        rtid = st.get("remote_task_id")
        if not rtid and st.get("request_id"):
            rtid = self.remote_task_id(st["request_id"])
            if rtid:
                self._event(FORWARDED, {"request_id": st["request_id"], "remote_task_id": rtid})
        if rtid:
            return self._settle(task, target, rtid)
        if st.get("request_id"):
            # Sent earlier, the target has not applied it yet: wait for it, never send a second card.
            return self._await(task, target, st["request_id"])
        return self._forward(task, target, st)

    def _forward(self, task, target: str, st: dict, *, reforward_of: Optional[str] = None) -> str:
        dead = self.live(target)
        if dead:
            if int(st.get("not_live") or 0) + 1 >= MAX_NOT_LIVE:
                return self._cancel(f"{target} not live {MAX_NOT_LIVE} times: {dead}")
            self._event(NOT_LIVE, {"why": dead})
            return self._park_clock(f"{target} is not live yet ({dead}); retrying the forward")
        args = remote_args(self.conn, task, self_gw=self.self_gw, board=self.board)
        distinct = self.carries_options()
        if distinct:
            # The target's overlap guard sees this local card through the replica and would refuse its own copy
            # as a duplicate of it (create_overlap naming only this card), so name it as distinct up front.
            args.update(distinct_from=[task.id], distinct_reason=(
                f"placement copy of {self.self_gw} card {task.id}: that card is this card's deferred-placement "
                f"placeholder and completes when this one is done, not separate work"))
        # This is transport identity, not a caller's receiver-side creation key. A re-forward after a
        # rejection needs a new one: the request id is derived from it, and the old id is terminal.
        key = f"placement-forward:{self.self_gw}:{task.id}" + (":distinct" if reforward_of else "")
        placed = self._create(args, target, st, key)
        if distinct and not placed.get("ok") and "create_option_unknown" in str(placed.get("error") or "") \
                and "distinct_" in str(placed.get("error") or ""):
            # A client that predates distinct_from: send the plain card (as before this option existed).
            args.pop("distinct_from", None)
            args.pop("distinct_reason", None)
            placed = self._create(args, target, st, key)
        if not placed.get("ok"):
            return self._cancel(f"{target} refused the card: {placed.get('error')}")
        rid = placed["request_id"]
        payload = {"request_id": rid, "queued_only": bool(placed.get("queued_only"))}
        if reforward_of:
            payload.update(reforwarded=True, reforward_of=reforward_of)
        self._event(FORWARDED, payload)
        return self._await(task, target, rid)

    def _create(self, args: dict, target: str, st: dict, key: str) -> dict:
        forwarding = {}
        if st.get("origin_gateway") and st["origin_gateway"] != self.self_gw:
            from hermes_cli.kanban_placement_provenance import forwarding_authority
            forwarding = {"origin_gateway": st["origin_gateway"], **forwarding_authority(
                self.conn, self.task_id, self.run_id, self_gw=self.self_gw)}
        return self.create(args, target=target, origin_session=st.get("origin_session"),
                           origin_profile=st.get("origin_profile") or "default", self_gw=self.self_gw,
                           cfg=self.cfg, hub_cli=self.hub_cli, transport_key=key, **forwarding)

    def _await(self, task, target: str, rid: str) -> str:
        from hermes_cli.kanban_placement import FAILED_STATES
        if self.remote_state(rid) in FAILED_STATES:
            return self._rejected(task, target, rid)
        tid, _status = self.await_id(rid, hub_cli=self.hub_cli, budget=AWAIT_BUDGET)
        if not tid:
            if self.remote_state(rid) in FAILED_STATES:  # refused during the wait (seconds, usually)
                return self._rejected(task, target, rid)
            return self._park_clock(f"request {rid} is queued for {target}; waiting for {target} to make the card")
        self._event(FORWARDED, {"request_id": rid, "remote_task_id": tid})
        from hermes_cli import kanban_db as kb
        kb.add_comment(self.conn, self.task_id, "placement",
                       f"Forwarded to {target} as {tid} (kanban.placement: its parents finished and nothing "
                       f"here needs this machine). This card completes when {tid} is done.")
        return self._park_remote(target, tid)

    def _rejected(self, task, target: str, rid: str) -> str:
        """The target refused request ``rid`` for good (a terminal state other than done): it will never make
        the card, so never park on it. A ``create_overlap`` naming only this card (the target saw the local
        placeholder through the replica) is re-forwarded once with ``distinct_from``; a real overlap blocks for a
        decision; anything else cancels the deferral and runs the card here."""
        from hermes_cli import kanban_db as kb
        why = self.rejection(rid) or {}
        st = state(self.conn, self.task_id) or {}
        code = why.get("code") or why.get("state") or "rejected"
        text = (why.get("error") or "").strip()[:400]
        ids = [str(m.get("id")) for m in why.get("matches") or []]
        if code == "create_overlap" and ids:
            others = [i for i in ids if i != task.id]
            self_only = not others and int(why.get("total_matches") or len(ids)) <= len(ids)
            if self_only and not st.get("reforwarded") and self.carries_options():
                print(f"placement forward: {target} refused {rid} as an overlap with this card itself; "
                      "re-forwarding with distinct_from", flush=True)
                return self._forward(task, target, st, reforward_of=rid)
            if not self_only:
                named = ", ".join(others) or f"{why.get('total_matches')} (not all named in the refusal)"
                ok = kb.block_task(
                    self.conn, self.task_id, kind="needs_input", expected_run_id=self.run_id,
                    reason=(f"{target} refused to make this card (create_overlap): it already has open card(s) "
                            f"{named} that look like this work. {text} "
                            "1) If that card covers it, archive this one. 2) If this is separate work, unblock "
                            "this card: it then runs here."))
                if ok:
                    self._event(CANCELLED, {"why": f"{target} rejected {rid}: create_overlap with {others}"})
                return "blocked" if ok else "block-refused"
        return self._cancel(f"{target} rejected request {rid} ({code}): {text}"[:600])

    def _settle(self, task, target: str, rtid: str) -> str:
        from hermes_cli import kanban_db as kb
        rc, out = self.check(target, rtid)
        words = (out or "").split()
        status = words[1] if rc == 0 and len(words) >= 2 and words[0] == rtid else ""
        if status == "done":
            ok = kb.complete_task(
                self.conn, self.task_id, expected_run_id=self.run_id, placement_settled=True,
                summary=(f"Ran on {target} as {rtid} (deferred placement), which is done. Its handoff is on "
                         f"that card: cross_task @task:default@{target}/{rtid}."),
                metadata={"placement": {"gateway": target, "remote_task_id": rtid}})
            return "completed" if ok else "complete-refused"
        if status == "archived":
            ok = kb.block_task(
                self.conn, self.task_id, kind="needs_input", expected_run_id=self.run_id,
                reason=(f"This card ran on {target} as {rtid}, which was archived there without finishing. "
                        "1) If the work is still wanted, unblock this card: it then runs here. "
                        "2) Otherwise archive this card."))
            if ok:
                self._event(CANCELLED, {"why": f"{rtid} archived on {target}"})
            return "blocked"
        return self._park_remote(target, rtid)

    # --- transitions ---------------------------------------------------------------------------

    def _event(self, kind: str, payload: dict) -> None:
        from hermes_cli import kanban_db as kb
        with kb.write_txn(self.conn):
            kb._append_event(self.conn, self.task_id, kind, payload)

    def _cancel(self, why: str) -> str:
        st = state(self.conn, self.task_id) or {}
        if st.get("origin_gateway") and st["origin_gateway"] != self.self_gw:
            return self._park_clock(f"qualified origin forwarding refused; no local fallback: {why}")
        self._event(CANCELLED, {"why": why})
        return "fallback"

    def _park_remote(self, target: str, rtid: str) -> str:
        from hermes_cli import kanban_awaiting as kaw
        from hermes_cli import kanban_db as kb
        awaiting = {"kind": "card", "ref": rtid, "gateway": target}
        for attempt in range(CONFIRM_RETRIES + 1):
            try:
                kaw.confirm_remote(awaiting)  # ssh, before the write txn
                break
            except kaw.AwaitingError as exc:
                # It finished between the settle read and this confirm: settle it (completes this card), once.
                if exc.code == kaw.PRODUCER_DONE and not getattr(self, "_resettled", False):
                    self._resettled = True
                    return self._settle(kb.get_task(self.conn, self.task_id), target, rtid)
                if exc.code != kaw.INVALID_AWAITING:
                    raise  # archived/triage producer: genuinely not live
                # The target just made this card and its replica has not synced it yet (``not found in <gw>
                # replica``), or ssh is down. The id came from the target's own create outcome, so it exists:
                # retry briefly, then park on it anyway; the wake probe re-reads it every CHECK_EVERY.
                if attempt < CONFIRM_RETRIES:
                    self.sleep(CONFIRM_RETRY_DELAY)
                    continue
                print(f"placement forward: {rtid} on {target} not confirmable yet ({exc.code}: {str(exc)[:160]}); "
                      "parking on it unconfirmed", flush=True)
                kaw._REMOTE_CONFIRMED[(target, rtid)] = (time.monotonic(), "just forwarded; not in replica yet")
        ok = kb.schedule_task(
            self.conn, self.task_id, expected_run_id=self.run_id, awaiting=awaiting,
            wake_check=wake_probe(target, rtid), check_every=CHECK_EVERY,
            reason=(f"SCHEDULED_UNTIL={_iso(self.now() + SETTLE_PARK_SECONDS)} running on {target} as {rtid} "
                    "(deferred placement); this card completes when that one is done"))
        return "parked" if ok else "park-refused"

    def _park_clock(self, why: str) -> str:
        from hermes_cli import kanban_db as kb
        ok = kb.schedule_task(
            self.conn, self.task_id, expected_run_id=self.run_id, clock_only=True,
            awaiting={"kind": "clock", "why": f"deferred placement retry: {why}"[:600]},
            # [daveey] A requeue loop (request still queued, refused with no local fallback, a failed forward
            # step) is bounded by the repeat-park escalation: the third requeue goes to an owner card.
            count_repeats=True,
            reason=f"SCHEDULED_UNTIL={_iso(self.now() + REQUEUE_SECONDS)} {why} (deferred placement)")
        return "requeued" if ok else "park-refused"


def _qualified_origin(st: Optional[dict], self_gw: Optional[str]) -> bool:
    """A card forwarded here from a qualified origin: it has no local fallback and must never run here."""
    return bool(st and st.get("origin_gateway") and st["origin_gateway"] != self_gw)


def hold(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], why: str) -> str:
    """Block the card (agent-owned, so the block sweep hands it to triage) instead of running it here. [daveey]"""
    from hermes_cli import kanban_db as kb
    # The quoted errors are data: a SCHEDULED_UNTIL= marker in them would turn this transient block into a timed park.
    quoted = why.replace("SCHEDULED_UNTIL=", "SCHEDULED_UNTIL:")
    reason = (f"placement forward failed and this card must not run here ({quoted})"[:900]
              + "; fix the forward or re-place the card, then unblock it.")
    try:
        ok = kb.block_task(conn, task_id, kind="transient", reason=reason, expected_run_id=run_id)
    except Exception:  # boundary: whatever the block raises, the card stays claimed (the dispatcher's crash path
        # reclaims it) and main() exits non-zero; it is never run here, so nothing may escape to main()'s fallback.
        logger.exception("placement forward: could not block %s", task_id)
        return "hold-failed"
    return "held" if ok else "hold-failed"


def recover(conn: sqlite3.Connection, task_id: str, run_id: Optional[int], fwd: Optional[Forwarder],
            exc: BaseException) -> str:
    """The action after a forward step raised. A local run (``fallback``) only for a card that was never sent and
    has a local fallback; a sent or qualified-origin card is re-queued, and if even that raises (a hub refusal text
    the park refused, a broken board) it is held, never run here. [daveey]"""
    try:
        st = state(conn, task_id)
    except Exception:  # boundary: an unreadable placement state is "unknown", which holds the card (never a local run)
        logger.exception("placement forward: placement state of %s unreadable", task_id)
        st = None
        unknown = True
    else:
        unknown = False
    if fwd is not None:
        self_gw = fwd.self_gw
    else:
        from hermes_cli import kanban_capability_registry as reg
        self_gw = reg.self_gateway() or "local"
    sent = bool(st and (st.get("request_id") or st.get("remote_task_id")))
    local_ok = not unknown and not sent and not _qualified_origin(st, self_gw)
    try:
        if fwd is None:
            raise exc
        if sent:
            # Already sent: a local run would duplicate it. Retry the forward later.
            return fwd._park_clock(f"forward step failed ({type(exc).__name__}: {str(exc)[:160]}); retrying")
        return fwd._cancel(f"forward failed: {exc!r}"[:400])
    except Exception as again:  # boundary: any failure of the requeue decides fallback-or-hold here, never escapes
        logger.exception("placement forward: recovery of %s failed", task_id)
        print(f"placement forward recovery failed: {again!r}", flush=True)
        if local_ok:
            return "fallback"
        return hold(conn, task_id, run_id, f"{type(exc).__name__}: {str(exc)[:200]}; then "
                                          f"{type(again).__name__}: {str(again)[:200]}")


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    worker_cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    action = "fallback"
    try:
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            run_id = int(run_raw) if run_raw else None
            fwd = None
            try:
                fwd = Forwarder(conn, task_id, run_id)
                action = fwd.run()
            except Exception as exc:  # before the target accepted it: run it here only if that is allowed
                print(f"placement forward failed: {exc!r}", flush=True)
                action = recover(conn, task_id, run_id, fwd, exc)
    except Exception as exc:
        print(f"placement forward could not open the board: {exc!r}", flush=True)
        action = "fallback"
    print(f"placement forward {task_id}: {action}", flush=True)
    if action == "fallback" and worker_cmd:
        os.execvp(worker_cmd[0], worker_cmd)
    return 0 if action not in ("fallback", "hold-failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
