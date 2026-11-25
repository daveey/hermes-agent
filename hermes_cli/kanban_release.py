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
SHOWN = "release_unread_shown"  # what a read-first refusal printed to which agent session; payload version 1
STAMP = "release_target"  # human_request field only the stranded-park brief sets
MARK = "Released to finish this card's acceptance."
_HUMAN_LIMITS = ("Narrower run counts, dollar caps and stop-on-finding conditions that agents wrote earlier are "
                 "superseded.")
_AGENT_LIMITS = ("Narrower run counts, dollar caps and stop-on-finding conditions written by agents earlier are "
                 "superseded. The card's budget after this release: {budget}.")
# Events that can follow a recorded release without moving the card: only then is a triage card "still ours".
_QUIET = frozenset({"commented", RELEASED, "spend_budget_set", "spend_budget_decision_ready", "scope_renewed",
                    "user_steps_answer", "card_answer_released", "stranded_park_escalated", "parent_held",
                    "hold_creator_notified", SHOWN})
ESCALATED = "stranded_park_escalated"  # kanban_stranded_park.EVENT: the home's own record of the ask it filed
REFILED = "stranded_park_refiled"  # the same record for an ask the sweep re-filed; no second desk notice
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


def mandate_text(runs: int, note: Optional[str] = None, *, agent: bool = False, budget: str = "") -> str:
    """The goal-level mandate. A human's wording is whole. An agent authority supersedes only what agents wrote
    and states the ``budget`` it leaves on the card (:func:`_budget_plan`) instead of a claim about anyone's."""
    limits = _AGENT_LIMITS.format(budget=budget or f"{runs} more runs") if agent else _HUMAN_LIMITS
    body = MANDATE.format(runs=runs, limits=limits)
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
    restriction that arrived after the offer does not hide it: ``release`` then refuses with the reason.

    The stored ask alone is not proof: ``tasks.human_request`` is a synced column any gateway may write on the
    Board, and the Board's ``block`` op stores a request as sent. So the stamp must also be on the home's own
    ``stranded_park_escalated`` event for this card (:func:`_home_filed`), whose origin the Board attests."""
    stamp = ask.get(STAMP) if isinstance(ask, dict) else None
    current = _pending_stop(conn, task_id)
    if not isinstance(stamp, dict) or current is None or not _home_filed(conn, task_id, ask):
        return None
    return current["task"] if (stamp.get("task"), stamp.get("episode")) == (current["task"], current["episode"]) else None


def ask_digest(ask: Any) -> str:
    """Identity of a filed ask: the sweep records it with the offer, so stored text it did not write is no offer."""
    import hashlib
    return hashlib.sha256(json.dumps(ask, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def own_events(conn: sqlite3.Connection, task_id: str, kinds: Sequence[str]) -> list[dict]:
    """This board's own events of ``kinds`` on the card, newest first: ``{id, kind, payload}``. A synced event
    keeps the sending gateway the Board authenticated (``origin_gateway``); a local one has none. An event
    another gateway pushed, or one whose origin a foreign write replaced, is not this board's."""
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_origin import gateway_names
    columns = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    origin = "origin_gateway" if "origin_gateway" in columns else "NULL"
    own = {gateway_names()[0], str((sync_state(conn) or {}).get("gateway_id") or "")}  # never the fleet list
    marks = ",".join("?" for _ in kinds)
    return [{"id": int(r[0]), "kind": r[1], "payload": _payload(r[2])} for r in conn.execute(
        f"SELECT id, kind, payload, {origin} FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id DESC",
        (task_id, *kinds)) if not r[3] or r[3] in own]


def _home_filed(conn: sqlite3.Connection, task_id: str, ask: dict) -> bool:
    """This board's own sweep filed ``ask``: its newest own filing record for the card (an escalation or a
    re-file) carries the ask's stamp and, when it recorded one, the ask's digest. A stamp on text the sweep did
    not write, a row another gateway pushed and a later own filing without the offer all fail."""
    filings = own_events(conn, task_id, (ESCALATED, REFILED))
    if not filings or filings[0]["payload"].get(STAMP) != ask.get(STAMP):
        return False
    digest = filings[0]["payload"].get("ask_sha256")
    return digest is None or digest == ask_digest(ask)  # records written before the digest existed carry none


def dead_offer(conn: sqlite3.Connection, task_id: str, ask: Any) -> bool:
    """``ask`` carries a release stamp its button cannot honour although a release could be offered now: the
    stamp names an earlier stop, or this board has no record of filing it (an ask filed before the record
    existed, a card rehomed after the ask, an event whose origin was overwritten, text the sweep did not write).
    A dead offer proves nothing about who wrote it: the stranded-park sweep decides from its own records whether
    to re-file it or to treat the card as any unowned stop (``kanban_stranded_park``)."""
    stamp = ask.get(STAMP) if isinstance(ask, dict) else None
    current = offer(conn, task_id) if isinstance(stamp, dict) else None
    return current is not None and (stamp != current or not _home_filed(conn, task_id, ask))


def _newest_stop(conn: sqlite3.Connection, tid: str) -> Optional[dict]:
    from hermes_cli.kanban_spend_decision import EVENT
    row = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (tid, EVENT)).fetchone()
    return dict(row) if row is not None else None


def _release_event(conn: sqlite3.Connection, tid: str) -> Optional[dict]:
    """The ``stop_released`` payload recorded after the card's newest stop (with its event ``_id``), else None."""
    stop = _newest_stop(conn, tid)
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind=? AND id>? ORDER BY id DESC "
                       "LIMIT 1", (tid, RELEASED, stop["id"] if stop else 0)).fetchone()
    payload = _payload(row[1]) if row else {}
    return {**payload, "_id": row[0]} if payload.get("version") == 1 else None


def _untouched_since(conn: sqlite3.Connection, tid: str, record: dict) -> bool:
    """Nothing moved the card after its release was recorded: every later event is a quiet one, or the
    ``promoted_manual`` the same release wrote for a dependency wait. A card a block loop, the respawn guard or
    anyone else put back in triage afterwards is there for their reason, and is not ours to promote."""
    for kind, raw in conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND id>?",
                                  (tid, record["_id"])):
        if kind not in _QUIET and not (kind == "promoted_manual" and "waiting_on_parents" in _payload(raw)):
            return False
    return True


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
                      session: Optional[str], evidence: Sequence[dict] = ()) -> Optional[int]:
    """This caller's identical mandate, written after the stop by an earlier attempt that failed past it.
    ``evidence``: the scope evidence the renewal must postdate; a mandate older than any of it is not reused,
    because the renewal would refuse it (``mandate_stale``) on every rerun."""
    from hermes_cli.kanban_sync_divergence import _event_order
    after = max([_event_order(stop), *(_event_order(e) for e in evidence)])
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


Refusal = _Refusal  # what :func:`read_first` raises for another verb's caller (``hermes kanban spend-budget``)


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
    resume = (recorded is not None and status == "triage" and not ksd.pending(conn, tid)
              and _untouched_since(conn, tid, recorded))
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


def _unread_items(conn: sqlite3.Connection, tid: str, stop: dict, writer: tuple, board: Optional[str],
                  verb: str = "release", extra: Sequence[dict] = ()) -> list[dict]:
    """What an agent caller must have read: human comments after the stop, a budget decision recorded after it,
    and other sessions' recent comments (promote's rule), plus ``extra`` items the caller names. Each ``{id, who,
    time, body}``; ``id`` None is a comment whose text has not synced here, which nothing can acknowledge."""
    from datetime import UTC, datetime
    from hermes_cli import kanban_release_comments as unread
    from hermes_cli import kanban_spend_override_fence as fence
    # Keyed by kind and id: an ``extra`` item may be a budget event (``kind: event``), whose id is a task_events
    # id and never stands for, or collides with, a comment id.
    items: dict = {(item.get("kind", "comment"), item["id"]): dict(item) for item in extra}
    for cid in _unread_human(conn, tid, stop, None):
        row = conn.execute("SELECT author, created_at, body FROM task_comments WHERE id=?", (cid,)).fetchone()
        if row is not None:
            items[("comment", cid)] = {"id": cid, "who": f"{row[0]} (human)", "body": str(row[2] or "")[:600],
                          "time": datetime.fromtimestamp(int(row[1] or 0), UTC).isoformat()}
    checks = (lambda: fence.check(conn, tid, seen=None, board=board),
              lambda: unread.check(conn, tid, session=writer[1], seen=None, verb=verb, board=board))
    for check in checks:
        try:
            check()
        except unread.UnreadComments as exc:
            for c in exc.comments:
                items.setdefault(("comment", c["id"]), {"id": c["id"], "who": c["writer_session"], "time": c["time"],
                                           "body": c["body"]})
    return sorted(items.values(), key=lambda item: (item["id"] is None, item["id"] or 0))


def _shown(conn: sqlite3.Connection, tid: str, session: Optional[str], verb: str, ids: Sequence[int],
           events: Sequence[int] = ()) -> bool:
    """Every id in ``ids`` (what this verb's caller must read now) was printed to agent ``session`` by a read-first
    refusal of the same ``verb`` on this board: ``ids`` is a subset of the union of ``comment_ids`` over this
    board's own ``release_unread_shown`` events for that session and verb. Each verb prints its own required set
    (``spend-budget`` prints the limit's own budget comment, ``release`` the comments after the stop), so one
    verb's refusal never stands in for the other's. A row another gateway pushed proves nothing about what this
    caller was shown (:func:`own_events`); a caller with no session has no identity to match."""
    if not session or not (ids or events):
        return False
    printed: set = set()
    printed_events: set = set()
    for e in own_events(conn, tid, (SHOWN,)):
        p = e["payload"]
        if p.get("session") == session and p.get("verb", "release") == verb:
            printed.update(c for c in (p.get("comment_ids") or []) if isinstance(c, int))
            printed_events.update(c for c in (p.get("event_ids") or []) if isinstance(c, int))
    return set(ids) <= printed and set(events) <= printed_events


def _record_shown(conn: sqlite3.Connection, tid: str, writer: tuple, ids: Sequence[int], verb: str,
                  events: Sequence[dict] = ()) -> None:
    """Record what a read-first refusal printed: the agent session, the comment ids, the budget events (local id
    and uid) printed in place of a note that is not readable here, the time."""
    import time
    from hermes_cli import kanban_db as kb
    if not ids and not events:
        return
    record = {"version": 1, "verb": verb, "actor_kind": writer[0], "session": writer[1] or None,
              "comment_ids": sorted(set(ids)), "time": int(time.time())}
    if events:
        record["event_ids"] = sorted({e["id"] for e in events})
        record["event_uids"] = sorted({str(e["uid"]) for e in events if e.get("uid")})
    with kb.write_txn(conn):
        kb._append_event(conn, tid, SHOWN, record)


def read_first(conn: sqlite3.Connection, tid: str, anchor: Optional[dict], writer: tuple, seen: Optional[int],
               board: Optional[str], *, verb: str = "release", extra: Sequence[dict] = (),
               rerun: str = "") -> Optional[int]:
    """Read before acting. Returns the ``seen`` id the budget fence gets, or :class:`_Refusal`.

    An agent authority does not decide over a person's words or a budget decision it has not been shown: the
    refusal prints them (human comments after ``anchor``, the budget fence's decision, other sessions' recent
    comments and ``extra``) and records a ``release_unread_shown`` event naming the caller's session and the ids
    it printed. ``--seen`` is accepted only as the id of the newest unread item AND only when this board's own
    such events show every item this verb requires was printed to the same session by this verb's refusal: a
    correct first guess, another session's or another verb's refusal, a larger number, or ``--seen`` when nothing
    needs reading is refused. A human proceeds and acknowledges what
    is on the card now, read before the mandate is written."""
    if writer[0] == "human":
        return conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    items = _unread_items(conn, tid, anchor, writer, board, verb, extra)
    if not items:
        if seen is not None:
            raise _Refusal("seen_not_needed", f"nothing on {tid} needs --seen; rerun without it")
        return None
    events = [item for item in items if item.get("kind") == "event"]
    comments = [item["id"] for item in items if item.get("kind") != "event" and item["id"] is not None]
    ids = comments + [item["id"] for item in events]
    newest = max(ids) if len(ids) == len(items) else None
    if (newest is not None and seen == newest
            and _shown(conn, tid, writer[1], verb, comments, [item["id"] for item in events])):
        return max(comments, default=None)  # the comment watermark: an event id never acknowledges a comment
    shown = "\n".join(f"{'Budget event' if item.get('kind') == 'event' else 'Comment'} {item['id']} | "
                      f"{item['who']} | {item['time']}\n{item['body']}" for item in items)
    command = rerun or f"hermes kanban {verb} {tid}"
    how = (f"After reading: `{command} --seen {newest}`" if newest is not None else
           "A comment's text has not synced here; wait for the sync and rerun")
    if newest is not None and not writer[1]:
        how = ("This caller has no session id (HERMES_SESSION_ID), so no refusal can be recorded as shown to it; "
               "a human operator, the owning chat or a delegate chat must do this")
    wrong = ""
    if seen is not None:
        wrong = (f" (--seen {seen} is not the newest of them)" if seen != newest else
                 f" (not everything here was printed to this session by an earlier {verb} refusal; it is now)")
    _record_shown(conn, tid, writer, comments, verb, events)
    doing = {"release": "releasing", "spend-budget": "changing the spend budget of"}.get(verb, verb)
    raise _Refusal("budget_decision_unread", f"read these before {doing} {tid}{wrong}:\n{shown}\n{how}",
                   unread=comments, **({"unread_events": [item["id"] for item in events]} if events else {}))


def _read_first(conn: sqlite3.Connection, tid: str, stop: dict, writer: tuple, seen: Optional[int],
                board: Optional[str]) -> Optional[int]:
    return read_first(conn, tid, stop, writer, seen, board)


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


def _budget_plan(conn: sqlite3.Connection, s) -> tuple[Optional[int], Optional[int], int, str]:
    """``(runs limit, cache-read limit, runs spent, text)`` this release leaves on the card; a None run limit
    leaves the override untouched.

    Everyone: a card with no run limit (run limit 0; exempt when the cache-read limit is 0 too) keeps that, and
    otherwise the run limit is the runs already counted plus N. A human lifts the cache-read limit (0). An
    agent authority keeps the cache-read limit of any override it cannot attribute to an agent
    (``kanban_spend_budget.cache_attribution``; older events record nothing and count as possibly a person's):
    it adds N runs and raises nothing else. ``s.keeps_limit`` records that, so the event this release writes carries
    the limit's own attribution forward and a later agent release keeps it too.

    A cache-read limit that is already reached in the window the card runs in would stop it again at once.
    A human (or an agent replacing an agent's limit) lifts it; an agent that must keep it is refused before
    anything is written (``budget_limit_in_force``). That holds for a card with no run limit as well."""
    from hermes_cli import kanban_spend_budget as ksb
    prior = ksb.card_override(conn, s.tid) or {}
    spent = ksb.spend(conn, s.tid)
    s.keeps_limit = (s.writer[0] != "human" and ksb.override_set(prior) and ksb.cache_attribution(prior) != "agent")
    s.prior = prior
    cache = ksb._limit_value(prior.get("cache_read_tokens"))
    limit = ksb.limits().cache_read_tokens if cache is None else cache
    used = spent.tokens["cache_read_tokens"]
    reached = bool(limit) and used >= limit and ksb.limit_window(conn, s.tid) == "current"
    if reached and s.keeps_limit:
        raise _Refusal("budget_limit_in_force", f"{s.tid} has reached its cache-read limit ({used} of {limit}) and an "
                       "agent does not raise a limit a person may have set; a human runs "
                       f"`hermes kanban release {s.tid}` or `hermes kanban spend-budget {s.tid}`")
    unlimited = ksb._limit_value(prior.get("runs")) == 0
    if unlimited and not reached:
        return None, None, spent.runs, "unchanged (no run limit on this card)"
    runs = 0 if unlimited else spent.runs + s.runs
    said = "no run limit" if unlimited else f"{runs} runs ({s.runs} more than the {spent.runs} counted)"
    if not s.keeps_limit:
        return runs, 0, spent.runs, f"{said}, no cache-read limit"
    return runs, cache, spent.runs, (f"{said}, cache-read limit kept at "
                                     f"{'the board limit' if cache is None else limit or 'no limit'}")


def _mandate_and_budget(conn: sqlite3.Connection, out: dict, s) -> None:
    """The mandate comment (reused on a rerun), the scope renewal and the budget; fills ``out`` and ``s``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release_comments as unread
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    from hermes_cli import kanban_spend_budget as ksb
    kind, session = s.writer[0], s.writer[1]
    body = mandate_text(s.runs, s.note, agent=kind != "human", budget=s.budget_text)
    done = renewal.covered(conn, s.tid)
    evidence = ([e for e in renewal.scope_evidence(conn, s.tid, s.episode) if renewal.key(e) not in done]
                if s.scope else [])
    comment = _existing_mandate(conn, s.tid, s.stop, body, kind, session, evidence)
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
    runs, cache, spent, _ = _budget_plan(conn, s)  # again: a human's mandate comment may have opened a new window
    if runs is None:
        s.budget = 0  # an exempt card (or one with no run limit) stays that way: a release never makes it finite
    else:
        s.budget = runs
        try:
            who = kind if kind in ("human", "agent") else None
            # The cache-read limit's own attribution, always: a kept one keeps what it had (a person's run-only
            # override defers to the board limit, and that deference is kept too); a lifted one is this writer's.
            kept = ksb.carried(s.prior).get("cache_read_writer_kind", "unknown") if s.keeps_limit else who
            # An agent raises the run count (that is the release) but a person's run limit stays a person's: a
            # later agent `spend-budget --runs 0` must not read it as agent-set and lift it unread.
            person_runs = ksb.runs_attribution(s.prior) or "unknown"
            runs_kind = (person_runs if kind != "human" and ksb.override_set(s.prior) and person_runs != "agent"
                         else None)
            ksb.set_override(conn, s.tid, runs=runs, cache_read_tokens=cache, by=s.by, reason=s.reason,
                             seen=s.budget_seen, board=s.board, writer_kind=who, cache_read_writer_kind=kept,
                             runs_writer_kind=runs_kind)
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


def _current_decision_card(conn: sqlite3.Connection, named: str, tid: str, episode: str) -> None:
    """A decision card stands for the stop it was filed for, never for a later stop of the same card."""
    if named == tid:
        return
    filed = _decision(conn, named).get("root_episode")
    if filed != episode:
        raise _Refusal("stale_decision_card", f"{named} was filed for stop {filed} of {tid}, which has since stopped "
                       f"again (current stop {episode}); release the card itself: `hermes kanban release {tid}`")


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
        _read_first(conn, tid, _newest_stop(conn, tid), s.writer, s.seen, s.board)  # never skipped, resume too
        return _resume(conn, out, tid, s.by, recorded, s.parents)
    s.stop = ksd.pending_episode(conn, tid)
    s.episode = renewal.key(s.stop)
    _current_decision_card(conn, out["task_id"], tid, s.episode)
    s.scope = ksd.execution_restricted(conn, tid)
    if s.scope and (s.stop["kind"] != renewal.STOP or s.status != "triage"):
        raise _Refusal("not_stopped", f"{tid} carries a scope restriction without a renewable stop in triage")
    if s.parents and s.status != "triage":
        raise _Refusal("scheduled_open_parent", f"{tid} is {s.status} with open parent(s) {', '.join(s.parents)}; "
                       "it cannot wait on them from there. Release it once they finish")
    s.budget_seen = _read_first(conn, tid, s.stop, s.writer, s.seen, s.board)
    if s.precondition and (why := s.precondition(conn)):
        raise _Refusal("ask_changed", str(why))
    s.budget_text = _budget_plan(conn, s)[3]  # also the up-front budget_limit_in_force refusal
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
