"""``hermes kanban release``: one step that lets a spend- or scope-stopped card finish. [daveey]

A card the spend budget or its own run stopped waits in triage for a recorded decision
(``kanban_spend_decision``). Releasing one took four operator steps (a mandate comment, ``renew-scope``,
``spend-budget``, ``promote``), and the mandates agents wrote for each other were one-step ones that stopped
again within minutes: 60 scope stops on 27 cards and 38 decision cards in 24 h on 2026-11-23.

``release`` composes the existing steps, per card, and writes nothing on a refusal:

1. the target is the card, or the stopped card an administrative decision card was filed for;
2. a provider safety restriction and a containment hold are refused, as everywhere else;
3. the caller is whoever ``renew-scope`` accepts (``kanban_scope_renewal_cli.resolve_actor``): a human
   operator, the owning chat or the creator task. A run of the stopped card is refused. A trusted in-process
   caller states a human act with ``writer=human_writer(...)`` (the Desktop Unblock click);
4. ONE goal-level mandate comment (:data:`MANDATE`), a scope renewal on it when the stop is a scope stop, a
   spend budget of the runs already counted plus ``runs`` with no cache-read limit, then promotion. Open
   decision cards filed for the released stop are archived with a note.

A card with an open parent is not promoted (the claim would refuse it): the decision is recorded and the card
waits in ``todo`` as a dependency wait, so it starts when its parents finish instead of returning to triage.

Only a card whose home is this board is released here; a replica-only copy is refused (``no_such_task``).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Mapping, Optional, Sequence

DEFAULT_RUNS = 10
MARK = "Released to finish this card's acceptance."
MANDATE = (MARK + " Budget: {runs} more runs. Narrower run counts, dollar caps and stop-on-finding conditions "
           "that agents wrote earlier are superseded. Stop only on: a provider safety refusal; a step that needs "
           "David physically; the same error three times; the run budget. Findings, design changes, review "
           "CHANGES, CI waits and sandbox refusals of -c scripts are work inside the round. Safety exclusions in "
           "the card body stay. If it stops, say what was built, what is left and the one next step.")
ONE_STEP = ("One step instead: `hermes kanban release {task}` writes a goal-level mandate (the card's acceptance, "
            "not a run count, dollar cap or stop-on-finding rule), then renews, budgets and promotes")
CONTAINMENT = "containment_hold: use scoped resume-contained; generic writes cannot satisfy containment"
_HELD = ("triage", "scheduled")
_RELEASED = ("ready", "todo", "running")
_OK = ("released", "waiting_on_parents", "already_released")


def human_writer(evidence: str) -> tuple[str, None, str]:
    """The ``writer`` a trusted in-process caller passes for an authenticated human act (never read from the
    process environment). ``evidence`` names the act, e.g. ``desktop-operation:<id>``."""
    return ("human", None, str(evidence or "explicit"))


def desktop_note(operation_id: str) -> str:
    """The mandate note of a Desktop Unblock click; it identifies the operation that released the card."""
    return "Desktop Unblock, operation " + str(operation_id)


def released_by(conn: sqlite3.Connection, task_id: str, note: str) -> Optional[str]:
    """The card a release carrying exactly ``note`` already released for ``task_id`` (or for the stop it is the
    decision card of), else None. A caller that died between the release and its own receipt uses this on the
    retry to record the receipt instead of releasing twice or refusing."""
    tid = target(conn, task_id)
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None or row[0] in _HELD or not note or not note.strip():
        return None
    tail = "\n\n" + note.strip()
    for (body,) in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC", (tid,)):
        if str(body or "").startswith(MARK) and str(body).endswith(tail):
            return tid
    return None


def default_runs(cfg: Optional[dict] = None) -> int:
    """``kanban.release_runs`` (default 10): how many more runs a release grants."""
    if cfg is None:
        from hermes_cli.config import load_config
        cfg = load_config()
    raw = ((cfg or {}).get("kanban") or {}).get("release_runs", DEFAULT_RUNS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return DEFAULT_RUNS
    return raw


def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def target(conn: sqlite3.Connection, task_id: str) -> str:
    """The stopped card ``task_id`` stands for: the root of an administrative decision card, else itself."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrative_decision' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    root = _payload(row[0]).get("root_task") if row else None
    return root if isinstance(root, str) and root else task_id


def held(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The card :func:`release` would act on when ``task_id`` (or the stop it is the decision card for) waits on
    a spend or scope decision, else None. A safety-restricted card counts: ``release`` refuses it with the reason."""
    from hermes_cli import kanban_spend_decision as ksd
    tid = target(conn, task_id)
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    return tid if row is not None and row[0] in _HELD and ksd.pending(conn, tid) else None


def _released_since_stop(conn: sqlite3.Connection, tid: str) -> bool:
    """A release mandate written after the card's newest stop."""
    from hermes_cli.kanban_spend_decision import EVENT
    stop = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?", (tid, EVENT)).fetchone()[0]
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='commented' AND id>?",
                               (tid, stop or 0)):
        row = conn.execute("SELECT body FROM task_comments WHERE task_id=? AND id=?",
                           (tid, _payload(raw).get("comment_id"))).fetchone()
        if row is not None and str(row[0] or "").startswith(MARK):
            return True
    return False


def _refused(out: dict, code: str, message: str, **more) -> dict:
    return {**out, "ok": False, "outcome": "refused", "refusal": code, "message": message, **more}


def _decision_cards(conn: sqlite3.Connection, tid: str, episode: str) -> list[str]:
    """Open ``Spend budget decision:`` cards filed for stop ``episode`` of ``tid``."""
    return [r[0] for r in conn.execute(
        "SELECT t.id FROM tasks t WHERE t.created_by='kanban-spend-budget' AND t.status NOT IN ('done','archived') "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='administrative_decision' "
        "AND json_valid(e.payload) AND json_extract(e.payload,'$.root_task')=? "
        "AND json_extract(e.payload,'$.root_episode')=?) ORDER BY t.created_at, t.id", (tid, episode))]


def _wait_on_parents(conn: sqlite3.Connection, tid: str, by: str, reason: str, parents: list[str]) -> Optional[str]:
    """Record the decision, then park the card as a dependency wait in ``todo``. None when it landed.

    ``promote_task`` refuses a card with an open parent (the claim re-checks parents), and readiness skips a
    card whose spend decision is still pending, so without the recorded decision the card would wait in
    ``todo`` for ever. The ``promoted_manual`` event is that decision (``kanban_spend_decision.pending_episode``),
    with the same window key ``promote_task`` writes; ``block_task(kind='dependency')`` then makes the
    transition ``hermes kanban block --kind dependency`` makes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_budget as ksb
    with kb.write_txn(conn):
        if conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] != "triage":
            return f"{tid} left triage during the release"
        window = ({"spend_budget": ksb.card_limits(conn, tid, ksb.limits())._asdict()}
                  if ksb.limit_window(conn, tid) == "next_promote" else {})
        kb._append_event(conn, tid, "promoted_manual", {
            "actor": by, "reason": reason, "from_status": "triage", "waiting_on_parents": parents, **window})
    try:
        landed = kb.block_task(conn, tid, reason=reason, kind="dependency", actor=by)
    except ValueError as exc:
        return str(exc)
    if not landed or conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] != "todo":
        return f"{tid} could not be parked as a dependency wait"
    return None


def release(conn: sqlite3.Connection, task_id: str, *, runs: Optional[int] = None, note: Optional[str] = None,
            writer: Optional[tuple] = None, author: Optional[str] = None, board: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None,
            ancestry: Optional[Sequence[Mapping[str, str]]] = None) -> dict:
    """Release one card; the result's ``outcome`` is ``released``, ``waiting_on_parents``, ``already_released``
    or ``refused`` (with ``refusal``: a code, and ``message``). Never raises for a refusal.

    ``writer`` is ``(kind, session, evidence)`` as ``kanban_comment_writer.cli_comment_writer`` returns it; when
    omitted it is read from the process, exactly as ``hermes kanban comment`` and ``renew-scope`` read it.
    :func:`human_writer` states an authenticated human act. ``env``/``ancestry`` are the renewal's test seams.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_comment_writer import Inconclusive, ancestry_environs, cli_comment_writer
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_refusal_receipt import restricted as safety_restricted
    from hermes_cli.kanban_release_comments import UnreadComments

    tid = target(conn, task_id)
    out: dict = {"task_id": task_id, "target": tid}
    runs = default_runs() if runs is None else runs
    if isinstance(runs, bool) or not isinstance(runs, int) or runs < 1:
        return _refused(out, "invalid_runs", f"--runs must be a whole number of at least 1, not {runs!r}")
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        return _refused(out, "no_such_task", f"no card {tid} on this board; release it on its home gateway")
    status = row[0]
    if safety_restricted(conn, tid):
        return _refused(out, "safety_restricted",
                        ksd.execution_refusal(conn, tid) or "Provider safety restriction retained")
    if contained(conn, tid):
        return _refused(out, "containment_hold", CONTAINMENT)
    if status in _RELEASED and _released_since_stop(conn, tid):
        return {**out, "ok": True, "outcome": "already_released",
                "message": f"{tid} is {status}: already released, nothing written"}
    if status not in _HELD or not ksd.pending(conn, tid):
        return _refused(out, "not_held", f"{tid} is {status}, not held by a spend or scope stop")

    env = os.environ if env is None else env
    writer = writer or cli_comment_writer(env=env)
    kind, session = writer[0], writer[1]
    if kind != "human":
        from hermes_cli.kanban_origin import gateway_names
        from hermes_cli.profiles import get_active_profile_name
        try:
            if ancestry is None:
                ancestry = ancestry_environs()
            renewal_cli.resolve_actor(conn, tid, writer=writer, environs=[*ancestry, env],
                                      gateway=gateway_names()[0], profile=get_active_profile_name() or "default")
        except Inconclusive as exc:
            return _refused(out, "identity_unavailable", f"the caller's process ancestry could not be read ({exc})")
        except renewal.RenewalRefused as exc:
            return _refused(out, exc.code, str(exc))
    stop = ksd.pending_episode(conn, tid)
    episode = renewal.key(stop)
    scope = ksd.execution_restricted(conn, tid)
    if scope and (stop["kind"] != renewal.STOP or status != "triage"):
        return _refused(out, "not_stopped", f"{tid} carries a scope restriction without a renewable stop in triage")

    from hermes_cli.profiles import current_profile_name
    by = (author or current_profile_name("user") or "user").strip()
    body = MANDATE.format(runs=runs) + (f"\n\n{note.strip()}" if note and note.strip() else "")
    comment = kb.add_comment(conn, tid, by, body, writer_kind=kind if kind in ("human", "agent") else None,
                             writer_session=session if kind == "agent" else None)
    out.update(runs=runs, episode=episode, mandate_comment=comment, scope_renewed=False)
    reason = f"hermes kanban release: {runs} more runs (mandate comment {comment})"
    written = f"; mandate comment {comment} was written, nothing else changed"
    if scope:
        try:
            renewal_cli.renew(conn, tid, episode=episode, comment_id=comment, reason=reason, board=board,
                              writer=writer, env=env, ancestry=ancestry)
        except renewal.RenewalRefused as exc:
            return _refused(out, exc.code, str(exc) + written)
        out["scope_renewed"] = True
    spent = ksb.spend(conn, tid).runs
    seen = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    try:
        ksb.set_override(conn, tid, runs=spent + runs, cache_read_tokens=0, by=by, reason=reason, seen=seen,
                         board=board)
    except UnreadComments as exc:  # an earlier budget decision whose comment has not synced here yet
        return _refused(out, "budget_decision_unread", f"{exc}; the mandate was written, nothing was promoted")
    out.update(spent_runs=spent, budget_runs=spent + runs)
    parents = [pid for pid, st in kb.parent_states(conn, tid) if st not in ("done", "archived")]
    if parents:
        if why := _wait_on_parents(conn, tid, by, reason, parents):
            return _refused(out, "dependency_wait_refused", why + "; the mandate and budget were written")
        out.update(ok=True, outcome="waiting_on_parents", parents=parents,
                   message=f"{tid} -> todo: released with {runs} more runs, waiting on {', '.join(parents)}")
    else:
        promoted, why = kb.promote_task(conn, tid, actor=by, reason=reason)
        if not promoted:
            return _refused(out, "promote_refused", str(why) + "; the mandate and budget were written")
        out.update(ok=True, outcome="released", message=f"{tid} -> ready: released with {runs} more runs")
    out["archived_decisions"], out["decisions_left"] = _close_decisions(conn, tid, episode, by, comment)
    return out


def _close_decisions(conn: sqlite3.Connection, tid: str, episode: str, by: str, comment: int) -> tuple[list, list]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_remote_stop import RemoteArchiveRefused, archive
    closed, left = [], []
    for card in _decision_cards(conn, tid, episode):
        kb.add_comment(conn, card, by, f"Closed by `hermes kanban release {tid}` (mandate comment {comment} on "
                                       f"{tid}): the stop this card was filed for is released.", system=True)
        try:
            done = archive(conn, card)
        except RemoteArchiveRefused:
            done = None
        (closed if done else left).append(card)
    return closed, left


def command(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_db_connect as kbc
    ids = list(dict.fromkeys([args.task_id, *(getattr(args, "ids", None) or [])]))
    results = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            try:
                results.append(release(conn, tid, runs=args.runs, note=args.note,
                                       board=getattr(args, "board", None)))
            except (ValueError, RuntimeError, sqlite3.Error) as exc:  # one card's failure never hides the others
                results.append(_refused({"task_id": tid, "target": tid}, "error", f"{type(exc).__name__}: {exc}"))
    if getattr(args, "json", False):
        print(json.dumps(results[0] if len(results) == 1 else results, sort_keys=True))
    else:
        for r in results:
            if r["outcome"] == "refused":
                print(f"{r['task_id']}: refused: {r['refusal']}: {r['message']}", file=sys.stderr)
                continue
            line = f"{r['task_id']}: {r['outcome']}: {r['message']}"
            if r.get("archived_decisions"):
                line += f"; archived decision card(s) {', '.join(r['archived_decisions'])}"
            print(line)
    return int(any(r["outcome"] not in _OK for r in results))
