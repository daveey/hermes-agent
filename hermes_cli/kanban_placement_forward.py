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
normal worker command, so the card still runs, here. Never a silent stall.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone, UTC
from typing import Callable, Optional

DEFERRED = "placement_deferred"
FORWARDED = "placement_forwarded"
CANCELLED = "placement_deferred_cancelled"
NOT_LIVE = "placement_forward_not_live"
MAX_NOT_LIVE = 3
REQUEUE_SECONDS = 600          # retry a not-live target / a still-queued request after this long
SETTLE_PARK_SECONDS = 14 * 86400  # timer backstop of the settle park; the wake probe ends it first
CHECK_EVERY = 300
AWAIT_BUDGET = 45.0
UNTIL = ("done", "archived")
MODULE = "hermes_cli.kanban_placement_forward"


# --- state -------------------------------------------------------------------------------------

def record_deferral(conn: sqlite3.Connection, task_id: str, *, target: str, origin_session: Optional[str],
                    origin_profile: str) -> None:
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, DEFERRED, {
            "target": target, "origin_session": origin_session or None, "origin_profile": origin_profile})


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
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)  # noqa: kanban-lockguard — short-lived read-only handle, closed in the finally below
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


# --- the forwarder -----------------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def wake_probe(target: str, remote_tid: str) -> str:
    return shlex.join(["hermes", "kanban", "wait-check", "task", remote_tid, "--gateway", target,
                       "--until", ",".join(UNTIL)])


def remote_args(conn: sqlite3.Connection, task, *, self_gw: str) -> dict:
    """The ``kanban_create`` view of the local card sent to the target: its options, and the body with
    the parents' handoffs (the target cannot read this board)."""
    from hermes_cli import kanban_db as kb
    parents = kb.parent_ids(conn, task.id)
    summaries = kb.latest_summaries(conn, parents) if parents else {}
    lines = []
    for pid in parents:
        p = kb.get_task(conn, pid)
        handoff = (summaries.get(pid) or (p.result if p else None) or "(no handoff)").strip()
        lines.append(f"- {pid} ({p.title if p else '?'}): {handoff[:1500]}")
    body = (task.body or "").rstrip()
    if lines:
        body += (f"\n\n---\nParent handoffs (the parents ran on {self_gw}; read them there with "
                 f"cross_task @task:default@{self_gw}/<id>):\n" + "\n".join(lines))
    body += f"\n\nThis card was {self_gw} card {task.id}; it completes there when this one is done."
    return {
        "title": task.title, "body": body, "assignee": task.assignee or "default",
        "skills": task.skills or None, "requires": task.requires or None,
        "max_runtime_seconds": task.max_runtime_seconds, "priority": task.priority or None,
        "model": task.model_override, "provider": task.provider_override if task.model_override else None,
        "goal_mode": bool(task.goal_mode), "goal_max_turns": task.goal_max_turns if task.goal_mode else None,
        "completion_contract": task.completion_contract,
        # One request per local card: a re-forward after a crash maps to the same hub request.
        "idempotency_key": f"placement-forward:{self_gw}:{task.id}",
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
        self.cfg = kp.config()
        self.self_gw = reg.self_gateway() or "local"
        self.hub_cli = reg.hub_cli_path(self.cfg.get("hub_cli"))
        self.live = lambda t: kp.target_live(t, now=self.now(), live_seconds=kp._Cfg(self.cfg).live_seconds())
        self.create = kp.create_remote
        self.await_id = kp.await_task_id
        self.remote_task_id = kp.remote_task_id
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

    def _forward(self, task, target: str, st: dict) -> str:
        dead = self.live(target)
        if dead:
            if int(st.get("not_live") or 0) + 1 >= MAX_NOT_LIVE:
                return self._cancel(f"{target} not live {MAX_NOT_LIVE} times: {dead}")
            self._event(NOT_LIVE, {"why": dead})
            return self._park_clock(f"{target} is not live yet ({dead}); retrying the forward")
        args = remote_args(self.conn, task, self_gw=self.self_gw)
        placed = self.create(args, target=target, origin_session=st.get("origin_session"),
                             origin_profile=st.get("origin_profile") or "default", self_gw=self.self_gw,
                             cfg=self.cfg, hub_cli=self.hub_cli)
        if not placed.get("ok"):
            return self._cancel(f"{target} refused the card: {placed.get('error')}")
        rid = placed["request_id"]
        self._event(FORWARDED, {"request_id": rid, "queued_only": bool(placed.get("queued_only"))})
        return self._await(task, target, rid)

    def _await(self, task, target: str, rid: str) -> str:
        tid, _status = self.await_id(rid, hub_cli=self.hub_cli, budget=AWAIT_BUDGET)
        if not tid:
            return self._park_clock(f"request {rid} is queued for {target}; waiting for {target} to make the card")
        self._event(FORWARDED, {"request_id": rid, "remote_task_id": tid})
        from hermes_cli import kanban_db as kb
        kb.add_comment(self.conn, self.task_id, "placement",
                       f"Forwarded to {target} as {tid} (kanban.placement: its parents finished and nothing "
                       f"here needs this machine). This card completes when {tid} is done.")
        return self._park_remote(target, tid)

    def _settle(self, task, target: str, rtid: str) -> str:
        from hermes_cli import kanban_db as kb
        rc, out = self.check(target, rtid)
        words = (out or "").split()
        status = words[1] if rc == 0 and len(words) >= 2 and words[0] == rtid else ""
        if status == "done":
            ok = kb.complete_task(
                self.conn, self.task_id, expected_run_id=self.run_id,
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
        self._event(CANCELLED, {"why": why})
        return "fallback"

    def _park_remote(self, target: str, rtid: str) -> str:
        from hermes_cli import kanban_awaiting as kaw
        from hermes_cli import kanban_db as kb
        awaiting = {"kind": "card", "ref": rtid, "gateway": target}
        kaw.confirm_remote(awaiting)  # ssh, before the write txn
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
            awaiting={"kind": "clock"},
            reason=f"SCHEDULED_UNTIL={_iso(self.now() + REQUEUE_SECONDS)} {why} (deferred placement)")
        return "requeued" if ok else "park-refused"


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    worker_cmd = argv[argv.index("--") + 1:] if "--" in argv else []
    task_id = os.environ.get("HERMES_KANBAN_TASK") or ""
    run_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    action = "fallback"
    try:
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing() as conn:
            fwd = Forwarder(conn, task_id, int(run_raw) if run_raw else None)
            try:
                action = fwd.run()
            except Exception as exc:  # before the target accepted it: run it here instead
                print(f"placement forward failed: {exc!r}", flush=True)
                st = state(conn, task_id)
                if st and (st.get("request_id") or st.get("remote_task_id")):
                    # Already sent: a local run would duplicate it. Retry the forward later.
                    action = fwd._park_clock(f"forward step failed ({type(exc).__name__}); retrying")
                else:
                    action = fwd._cancel(f"forward failed: {exc!r}"[:400])
    except Exception as exc:
        print(f"placement forward could not open the board: {exc!r}", flush=True)
        action = "fallback"
    print(f"placement forward {task_id}: {action}", flush=True)
    if action == "fallback" and worker_cmd:
        os.execvp(worker_cmd[0], worker_cmd)
    return 0 if action != "fallback" else 1


if __name__ == "__main__":
    raise SystemExit(main())
