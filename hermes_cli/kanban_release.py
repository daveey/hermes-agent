"""``hermes kanban release``: one step that lets a spend- or scope-stopped card finish. [daveey]

A card the spend budget or its own run stopped waits in triage for a recorded decision
(``kanban_spend_decision``). Releasing one took four operator steps (a mandate comment, ``renew-scope``,
``spend-budget``, ``promote``), and the mandates agents wrote for each other were one-step ones that stopped
again within minutes: 60 scope stops on 27 cards and 38 decision cards in 24 h on 2026-11-23.

``release`` composes the existing steps, per card:

1. the target is the card, or the stopped card an administrative decision card was filed for;
2. a provider safety restriction and a containment hold are refused, as everywhere else;
3. the caller is a human operator, the card's owning chat or a delegate chat
   (``kanban_scope_renewal_cli.resolve_actor``, ``kanban_scope_delegate``). A run of the stopped card is
   refused, and worker and delegated-child shells are fenced from the verb. A trusted in-process caller states
   a human act with ``writer=human_writer(...)`` (the Desktop Unblock click);
4. everything that can refuse is checked before anything is written: status, parents, authority, and for an
   agent caller what it has not read (a human comment or a budget decision after the stop needs ``--seen``);
5. ONE goal-level mandate comment (:func:`mandate_text`; a rerun reuses it), a scope renewal on it when the stop
   is a scope stop, a spend budget of the runs already counted plus ``runs`` with no cache-read limit (an
   exempt card stays exempt), then the promotion. A ``stop_released`` event is written in the transaction that
   records the decision: it, not a comment, is what makes a rerun ``already_released``. Open decision cards
   filed for the released stop are archived with a note.

A card with an open parent is not promoted (the claim would refuse it): the decision is recorded and the card
waits in ``todo`` as a dependency wait, so it starts when its parents finish instead of returning to triage.
If that park fails after the decision was recorded, a rerun finishes it.

Only a card whose home is this board is released here; a replica-only copy is refused (``no_such_task``).

The Desktop Unblock button releases only an ask that offered it: the stranded-park brief stamps its ask with
``release_target`` (:func:`offer`), no agent-supplied request can carry that field
(``kanban_human_request.validate_human_request`` drops it), and both click paths ask :func:`offered`.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Sequence

DEFAULT_RUNS = 10
RELEASED = "stop_released"  # the durable record of one release; payload version 1
STAMP = "release_target"  # human_request field only the stranded-park brief sets
MARK = "Released to finish this card's acceptance."
_HUMAN_LIMITS = ("Narrower run counts, dollar caps and stop-on-finding conditions that agents wrote earlier are "
                 "superseded.")
_AGENT_LIMITS = ("Narrower run counts, dollar caps and stop-on-finding conditions written by agents earlier are "
                 "superseded; a limit David set stays in force.")
MANDATE = (MARK + " Budget: {runs} more runs. {limits} Stop only on: a provider safety refusal; a step that needs "
           "David physically; the same error three times; the run budget. Findings, design changes, review "
           "CHANGES, CI waits and sandbox refusals of -c scripts are work inside the round. Safety exclusions in "
           "the card body stay. If it stops, say what was built, what is left and the one next step.")
ONE_STEP = ("One step instead, for a human operator, the card's owning chat or a delegate chat: "
            "`hermes kanban release {task}` writes a goal-level mandate (the card's acceptance, not a run count, "
            "dollar cap or stop-on-finding rule), then renews, budgets and promotes")
WORKER_NOTE = ("A human operator, the owning chat of {task} or a delegate chat can release it in one step "
               "(`hermes kanban release`); this card's worker cannot run that")
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


def mandate_text(runs: int, note: Optional[str] = None, *, agent: bool = False) -> str:
    """The goal-level mandate. An agent authority supersedes only what agents wrote; a human's wording is whole."""
    body = MANDATE.format(runs=runs, limits=_AGENT_LIMITS if agent else _HUMAN_LIMITS)
    return body + (f"\n\n{note.strip()}" if note and note.strip() else "")


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


def _decision(conn: sqlite3.Connection, task_id: str) -> dict:
    """The newest ``administrative_decision`` payload of a decision card, else ``{}``."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='administrative_decision' "
                       "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return _payload(row[0]) if row else {}


def target(conn: sqlite3.Connection, task_id: str) -> str:
    """The stopped card ``task_id`` stands for: the root of an administrative decision card, else itself."""
    root = _decision(conn, task_id).get("root_task")
    return root if isinstance(root, str) and root else task_id


def held(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The card :func:`release` would act on when ``task_id`` (or the stop it is the decision card for) waits on
    a spend or scope decision, else None. A safety-restricted card counts: ``release`` refuses it with the reason."""
    from hermes_cli import kanban_spend_decision as ksd
    tid = target(conn, task_id)
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    return tid if row is not None and row[0] in _HELD and ksd.pending(conn, tid) else None


def offer(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The ``release_target`` stamp for an ask the system files on ``task_id``: the stopped card an Unblock would
    release and its pending stop, or None when no release can be offered (not held, safety-restricted, or a
    decision card filed for an earlier stop than the pending one). Only ``kanban_stranded_brief`` calls this."""
    from hermes_cli.kanban_refusal_receipt import restricted
    current = _pending_stop(conn, task_id)
    return None if current is None or restricted(conn, current["task"]) else current


def _pending_stop(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """``{task, episode}``: the held card ``task_id`` stands for and its pending stop; None when nothing is held
    or ``task_id`` is a decision card filed for another stop than the pending one."""
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_spend_decision as ksd
    tid = held(conn, task_id)
    if tid is None:
        return None
    episode = renewal.key(ksd.pending_episode(conn, tid))
    if tid != task_id and _decision(conn, task_id).get("root_episode") != episode:
        return None
    return {"task": tid, "episode": episode}


def offered(conn: sqlite3.Connection, task_id: str, ask: Any) -> Optional[str]:
    """The card an Unblock on ``ask`` (the bound card's stored human_request) releases, else None.

    The ask must carry the stamp the brief wrote and the stamp must still be true: same stopped card, same
    pending stop, and for a decision card the stop it was filed for. An agent-written ask, an ask filed before
    the stamp existed and a stale decision card all return None: the click is then a plain unblock. A safety
    restriction that arrived after the offer does not hide it: ``release`` then refuses with the reason."""
    stamp = ask.get(STAMP) if isinstance(ask, dict) else None
    current = _pending_stop(conn, task_id)
    if not isinstance(stamp, dict) or current is None:
        return None
    return current["task"] if (stamp.get("task"), stamp.get("episode")) == (current["task"], current["episode"]) else None


def _release_event(conn: sqlite3.Connection, tid: str) -> Optional[dict]:
    """The ``stop_released`` payload recorded after the card's newest stop, else None."""
    from hermes_cli.kanban_spend_decision import EVENT
    stop = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?", (tid, EVENT)).fetchone()[0]
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? AND id>? ORDER BY id DESC LIMIT 1",
                       (tid, RELEASED, stop or 0)).fetchone()
    payload = _payload(row[0]) if row else {}
    return payload if payload.get("version") == 1 else None


def released_by(conn: sqlite3.Connection, task_id: str, note: str) -> Optional[str]:
    """The card a release carrying exactly ``note`` already released for ``task_id`` (or for the stop it is the
    decision card of), else None. A caller that died between the release and its own receipt uses this on the
    retry to record the receipt instead of releasing twice or refusing. Read from the ``stop_released`` event."""
    tid = target(conn, task_id)
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None or row[0] not in _RELEASED or not note or not note.strip():
        return None
    event = _release_event(conn, tid)
    return tid if event is not None and event.get("note") == note.strip() else None


def _refused(out: dict, code: str, message: str, **more) -> dict:
    return {**out, "ok": False, "outcome": "refused", "refusal": code, "message": message, **more}


def _decision_cards(conn: sqlite3.Connection, tid: str, episode: str) -> list[str]:
    """Open ``Spend budget decision:`` cards filed for stop ``episode`` of ``tid``."""
    return [r[0] for r in conn.execute(
        "SELECT t.id FROM tasks t WHERE t.created_by='kanban-spend-budget' AND t.status NOT IN ('done','archived') "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.id AND e.kind='administrative_decision' "
        "AND json_valid(e.payload) AND json_extract(e.payload,'$.root_task')=? "
        "AND json_extract(e.payload,'$.root_episode')=?) ORDER BY t.created_at, t.id", (tid, episode))]


def _unread_human(conn: sqlite3.Connection, tid: str, stop: dict, seen: Optional[int]) -> list[int]:
    """Ids of comments a person wrote (or a chat relayed for one) after the stop, newer than ``seen``."""
    from hermes_cli.kanban_sync_divergence import _event_order
    after, out = _event_order(stop), []
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='commented'", (tid,)):
        event = dict(row)
        p = _payload(event["payload"])
        cid = p.get("comment_id")
        if ((p.get("writer_kind") == "human" or p.get("relayed_human") is not None) and _event_order(event) > after
                and isinstance(cid, int) and cid > (seen or 0)):
            out.append(cid)
    return sorted(out)


def _existing_mandate(conn: sqlite3.Connection, tid: str, stop: dict, body: str, kind: str,
                      session: Optional[str]) -> Optional[int]:
    """This caller's identical mandate, written after the stop by an earlier attempt that failed past it."""
    from hermes_cli.kanban_sync_divergence import _event_order
    after = _event_order(stop)
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC", (tid,)):
        event = dict(row)
        p = _payload(event["payload"])
        if (_event_order(event) <= after or p.get("writer_kind") != kind
                or (kind == "agent" and p.get("writer_session") != session)):
            continue
        found = conn.execute("SELECT id, body FROM task_comments WHERE task_id=? AND id=?",
                             (tid, p.get("comment_id"))).fetchone()
        if found is not None and found[1] == body.strip():
            return int(found[0])
    return None


def _place(conn: sqlite3.Connection, tid: str, by: str, reason: str, parents: list[str]) -> tuple[str, Optional[str]]:
    """Move a card whose decision is already recorded out of triage: ``(outcome, refusal message)``.

    With an open parent it is ``block_task(kind='dependency')``, the transition ``hermes kanban block --kind
    dependency`` makes; without one a plain promotion. Also what a rerun does after a failed first placement."""
    from hermes_cli import kanban_db as kb
    if not parents:
        promoted, why = kb.promote_task(conn, tid, actor=by, reason=reason)
        return ("released", None) if promoted else ("refused", str(why))
    try:
        landed = kb.block_task(conn, tid, reason=reason, kind="dependency", actor=by)
    except ValueError as exc:
        return "refused", str(exc)
    if not landed or conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] != "todo":
        return "refused", f"{tid} could not be parked as a dependency wait"
    return "waiting_on_parents", None


class _Changed(Exception):
    """Rolls back the deciding transaction when a fence no longer holds."""


class _Refusal(Exception):
    """One phase's refusal: ``(code, message, extra result fields)``."""

    def __init__(self, code: str, message: str, **more):
        super().__init__(message)
        self.code, self.more = code, more


def _eligible(conn: sqlite3.Connection, tid: str) -> tuple[str, Optional[dict], bool]:
    """``(status, recorded release, resume)`` for a card ``release`` may act on, else :class:`_Refusal`.
    ``resume``: the decision is recorded but the card never left triage (the park or promotion failed)."""
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_refusal_receipt import restricted as safety_restricted
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if row is None:
        raise _Refusal("no_such_task", f"no card {tid} on this board; release it on its home gateway")
    status = row[0]
    if safety_restricted(conn, tid):
        raise _Refusal("safety_restricted", ksd.execution_refusal(conn, tid) or "Provider safety restriction retained")
    if contained(conn, tid):
        raise _Refusal("containment_hold", CONTAINMENT)
    recorded = _release_event(conn, tid)
    resume = recorded is not None and status == "triage" and not ksd.pending(conn, tid)
    done = recorded is not None and status in _RELEASED
    if not done and not resume and (status not in _HELD or not ksd.pending(conn, tid)):
        raise _Refusal("not_held", f"{tid} is {status}, not held by a spend or scope stop")
    return status, recorded, resume


def _authority(conn: sqlite3.Connection, tid: str, writer: tuple, env: Mapping[str, str],
               ancestry: Optional[Sequence[Mapping[str, str]]], board: Optional[str]) -> tuple[str, Optional[Sequence]]:
    """``(authority kind, ancestry read)`` for the caller, else :class:`_Refusal`. A human needs no ancestry."""
    from hermes_cli import kanban_scope_delegate as delegates
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    from hermes_cli.kanban_comment_writer import Inconclusive, ancestry_environs
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    if writer[0] == "human":
        return "human", ancestry
    try:
        if ancestry is None:
            ancestry = ancestry_environs()
        gateway, profile = gateway_names()[0], get_active_profile_name() or "default"
        # The grant lookup renew() makes: a chat holding a live human grant may release a card it does not
        # own. Without it this pre-check would refuse a delegate before the renewal is ever reached.
        actor = renewal_cli.resolve_actor(
            conn, tid, writer=writer, environs=[*ancestry, env], gateway=gateway, profile=profile,
            delegate=lambda s: delegates.live_grant(conn, board, gateway=gateway, profile=profile, session=s))
    except Inconclusive as exc:
        raise _Refusal("identity_unavailable", f"the caller's process ancestry could not be read ({exc})")
    except renewal.RenewalRefused as exc:
        raise _Refusal(exc.code, str(exc))
    return actor.authority, ancestry


def _open_parents(conn: sqlite3.Connection, tid: str) -> list[str]:
    from hermes_cli import kanban_db as kb
    states = kb.parent_states(conn, tid)
    missing = [pid for pid, st in states if st is None]
    if missing:
        raise _Refusal("parent_unresolved", f"parent(s) {', '.join(missing)} of {tid} are not on this board; "
                       "unlink the dangling edge or wait for the sync, then rerun")
    return [pid for pid, st in states if st not in ("done", "archived")]


def _read_first(conn: sqlite3.Connection, tid: str, stop: dict, writer: tuple, seen: Optional[int],
                board: Optional[str]) -> Optional[int]:
    """Read before acting. Returns the ``seen`` id the budget fence gets, or :class:`_Refusal`.

    An agent authority does not decide over a person's words or a budget decision it has not been shown; a
    human proceeds and acknowledges what is on the card now, read before the mandate is written."""
    from hermes_cli import kanban_release_comments as unread
    from hermes_cli import kanban_spend_override_fence as fence
    if writer[0] == "human":
        return conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    rerun = "hermes kanban release " + tid
    if people := _unread_human(conn, tid, stop, seen):
        raise _Refusal("budget_decision_unread", f"a human commented on {tid} after the stop (comment {people[-1]}); "
                       f"read it, then `{rerun} --seen {people[-1]}`", unread=people)
    try:
        fence.check(conn, tid, seen=seen, board=board)
        unread.check(conn, tid, session=writer[1], seen=seen, verb="release", board=board)
    except unread.UnreadComments as exc:
        ids = [c["id"] for c in exc.comments if c["id"] is not None]
        raise _Refusal("budget_decision_unread", f"{exc}\nAfter reading: `{rerun} --seen "
                       f"{max(ids) if ids else '<comment id>'}`", unread=ids)
    return seen


def _resume(conn: sqlite3.Connection, out: dict, tid: str, by: str, recorded: dict, parents: list[str]) -> dict:
    reason = ("hermes kanban release: finishing the release recorded with mandate comment "
              f"{recorded.get('mandate_comment_id')}")
    outcome, why = _place(conn, tid, by, reason, parents)
    if why:
        raise _Refusal("dependency_wait_refused" if parents else "promote_refused",
                       why + "; the decision is recorded, rerun to finish")
    return {**out, "ok": True, "outcome": outcome, "runs": recorded.get("runs"), "resumed": True,
            **({"parents": parents} if parents else {}),
            "message": f"{tid} -> {'todo' if parents else 'ready'}: finished the release already recorded"}


def _mandate_and_budget(conn: sqlite3.Connection, out: dict, s) -> None:
    """The mandate comment (reused on a rerun), the scope renewal and the budget; fills ``out`` and ``s``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release_comments as unread
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    from hermes_cli import kanban_spend_budget as ksb
    kind, session = s.writer[0], s.writer[1]
    body = mandate_text(s.runs, s.note, agent=kind != "human")
    comment = _existing_mandate(conn, s.tid, s.stop, body, kind, session)
    reused = comment is not None
    if comment is None:
        comment = kb.add_comment(conn, s.tid, s.by, body, writer_kind=kind if kind in ("human", "agent") else None,
                                 writer_session=session if kind == "agent" else None)
    out.update(runs=s.runs, episode=s.episode, mandate_comment=comment, mandate_reused=reused, scope_renewed=False)
    s.comment = comment
    s.reason = f"hermes kanban release: {s.runs} more runs (mandate comment {comment})"
    s.kept = f"; mandate comment {comment} is on the card and a rerun reuses it"
    if s.scope:
        try:
            renewal_cli.renew(conn, s.tid, episode=s.episode, comment_id=comment, reason=s.reason, board=s.board,
                              writer=s.writer, env=s.env, ancestry=s.ancestry)
        except renewal.RenewalRefused as exc:
            raise _Refusal(exc.code, str(exc) + s.kept)
        out["scope_renewed"] = True
    spent = ksb.spend(conn, s.tid).runs
    if ksb._limit_value((ksb.card_override(conn, s.tid) or {}).get("runs")) == 0:
        s.budget = 0  # an exempt card (or one with no run limit) stays that way: a release never makes it finite
    else:
        s.budget = spent + s.runs
        try:
            ksb.set_override(conn, s.tid, runs=s.budget, cache_read_tokens=0, by=s.by, reason=s.reason,
                             seen=s.budget_seen, board=s.board)
        except unread.UnreadComments as exc:  # a budget decision recorded since the check above
            raise _Refusal("budget_decision_unread", f"{exc}{s.kept}")
    out.update(spent_runs=spent, budget_runs=s.budget)


def _decide(conn: sqlite3.Connection, s) -> str:
    """Record the decision and the ``stop_released`` event in one transaction, then place the card. The outcome."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release_comments as unread
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli import kanban_spend_decision as ksd
    kind, session = s.writer[0], s.writer[1]
    row = conn.execute("SELECT * FROM task_comments WHERE id=?", (s.comment,)).fetchone()
    record = {"version": 1, "episode": s.episode, "authority_kind": s.authority, "actor_kind": kind,
              "actor_session": session if kind == "agent" else None, "runs": s.runs, "budget_runs": s.budget,
              "mandate_comment_id": s.comment, "mandate_comment_uid": renewal.comment_ref(dict(row)),
              "note": s.note.strip() if s.note and s.note.strip() else None}
    stale: list[str] = []  # the caller's own fence refused inside the deciding transaction

    def decided(c: sqlite3.Connection) -> Optional[str]:
        """Inside the deciding transaction: the fences once more, then the durable record."""
        if s.precondition and (why := s.precondition(c)):
            stale.append(str(why))
            return str(why)
        now = c.execute("SELECT status FROM tasks WHERE id=?", (s.tid,)).fetchone()
        if now is None or now[0] != s.status or not ksd.pending(c, s.tid):
            return f"{s.tid} changed during the release"
        if kind != "human":
            if people := _unread_human(c, s.tid, s.stop, s.seen):
                return f"a human commented on {s.tid} during the release (comment {people[-1]})"
            unread.check(c, s.tid, session=session, seen=s.seen, verb="release", board=s.board)
        kb._append_event(c, s.tid, RELEASED, record)
        return None

    try:
        if not s.parents:
            promoted, why = kb.promote_task(conn, s.tid, actor=s.by, reason=s.reason, precondition=decided)
            if not promoted:
                raise _Refusal("ask_changed" if stale else "promote_refused", str(why) + s.kept)
            return "released"
        with kb.write_txn(conn):
            if why := decided(conn):
                raise _Changed(why)
            window = ({"spend_budget": ksb.card_limits(conn, s.tid, ksb.limits())._asdict()}
                      if ksb.limit_window(conn, s.tid) == "next_promote" else {})
            # promote_task refuses an open parent and readiness skips a card whose spend decision is pending,
            # so the decision is recorded here (kanban_spend_decision.pending_episode) and the card parked.
            kb._append_event(conn, s.tid, "promoted_manual", {
                "actor": s.by, "reason": s.reason, "from_status": "triage", "waiting_on_parents": s.parents,
                **window})
    except _Changed as exc:
        raise _Refusal("ask_changed" if stale else "promote_refused", str(exc) + s.kept)
    except unread.UnreadComments as exc:
        raise _Refusal("budget_decision_unread", f"{exc}{s.kept}")
    outcome, why = _place(conn, s.tid, s.by, s.reason, s.parents)
    if why:
        raise _Refusal("dependency_wait_refused", why + "; the decision is recorded, rerun to finish")
    return outcome


def release(conn: sqlite3.Connection, task_id: str, *, runs: Optional[int] = None, note: Optional[str] = None,
            writer: Optional[tuple] = None, author: Optional[str] = None, board: Optional[str] = None,
            seen: Optional[int] = None,
            precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None,
            env: Optional[Mapping[str, str]] = None,
            ancestry: Optional[Sequence[Mapping[str, str]]] = None) -> dict:
    """Release one card; the result's ``outcome`` is ``released``, ``waiting_on_parents``, ``already_released``
    or ``refused`` (with ``refusal``: a code, and ``message``). Never raises for a refusal.

    ``writer`` is ``(kind, session, evidence)`` as ``kanban_comment_writer.cli_comment_writer`` returns it; when
    omitted it is read from the process, exactly as ``hermes kanban comment`` and ``renew-scope`` read it.
    :func:`human_writer` states an authenticated human act. ``seen`` is the newest comment id an agent caller
    has read (``--seen``). ``precondition(conn)`` returns a refusal or None; it is asked before anything is
    written and again inside the transaction that records the decision (a click's ask fence).
    ``env``/``ancestry`` are the renewal's test seams.
    """
    tid = target(conn, task_id)
    out: dict = {"task_id": task_id, "target": tid}
    try:
        return _release(conn, out, SimpleNamespace(
            tid=tid, runs=default_runs() if runs is None else runs, note=note, writer=writer, author=author,
            board=board, seen=seen, precondition=precondition, env=os.environ if env is None else env,
            ancestry=ancestry))
    except _Refusal as exc:
        return _refused(out, exc.code, str(exc), **exc.more)


def _release(conn: sqlite3.Connection, out: dict, s) -> dict:
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_comment_writer import cli_comment_writer
    from hermes_cli.profiles import current_profile_name
    tid = s.tid
    if isinstance(s.runs, bool) or not isinstance(s.runs, int) or s.runs < 1:
        raise _Refusal("invalid_runs", f"--runs must be a whole number of at least 1, not {s.runs!r}")
    s.status, recorded, resume = _eligible(conn, tid)
    if recorded is not None and s.status in _RELEASED:
        return {**out, "ok": True, "outcome": "already_released",
                "message": f"{tid} is {s.status}: already released, nothing written"}
    s.writer = s.writer or cli_comment_writer(env=s.env)
    s.authority, s.ancestry = _authority(conn, tid, s.writer, s.env, s.ancestry, s.board)
    s.parents = _open_parents(conn, tid)
    s.by = (s.author or current_profile_name("user") or "user").strip()
    if resume:
        return _resume(conn, out, tid, s.by, recorded, s.parents)
    s.stop = ksd.pending_episode(conn, tid)
    s.episode = renewal.key(s.stop)
    s.scope = ksd.execution_restricted(conn, tid)
    if s.scope and (s.stop["kind"] != renewal.STOP or s.status != "triage"):
        raise _Refusal("not_stopped", f"{tid} carries a scope restriction without a renewable stop in triage")
    if s.parents and s.status != "triage":
        raise _Refusal("scheduled_open_parent", f"{tid} is {s.status} with open parent(s) {', '.join(s.parents)}; "
                       "it cannot wait on them from there. Release it once they finish")
    s.budget_seen = _read_first(conn, tid, s.stop, s.writer, s.seen, s.board)
    if s.precondition and (why := s.precondition(conn)):
        raise _Refusal("ask_changed", str(why))
    _mandate_and_budget(conn, out, s)
    outcome = _decide(conn, s)
    parents = s.parents
    out.update(ok=True, outcome=outcome, **({"parents": parents} if parents else {}),
               message=(f"{tid} -> todo: released with {s.runs} more runs, waiting on {', '.join(parents)}" if parents
                        else f"{tid} -> ready: released with {s.runs} more runs"))
    out["archived_decisions"], out["decisions_left"] = _close_decisions(conn, tid, s.episode, s.by, s.comment)
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
    seen = getattr(args, "seen_comment_id", None)
    if seen is not None and len(ids) > 1:
        print("kanban release: --seen belongs to one card; release each such card separately", file=sys.stderr)
        return 2
    results = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            try:
                results.append(release(conn, tid, runs=args.runs, note=args.note, seen=seen,
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
