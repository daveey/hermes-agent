"""Human-only release offers: a Release button that works when only a person can release. [daveey]

A spend- or scope-stopped card whose Board reviewer allowance is exhausted (or still held under a newer generic
budget) can be released by a person only: ``kanban_release._board_allowance`` refuses every agent authority with
``allowance_exhausted`` / ``allowance_in_force``. A live owner chat then owns a stop it cannot act on, so the
stranded-park sweep's live-owner exemption must not suppress the stamped offer (``kanban_stranded_park``).

:func:`file` writes that stamped ask on this home: the ``release_target`` stamp plus this home's own
``stranded_park_escalated`` record with the ask's digest, exactly what ``kanban_release.offered`` requires, with
Release / Don't choices. It binds the ask to the card's linked chats and the requesting chat. It never releases:
only a recorded human answer (``kanban_card_answer.apply``: Release, or the default Unblock) or a human operator's
``hermes kanban release`` does, as the human act it is. No agent identity is ever upgraded to a human one.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

RELEASE, DONT, UNBLOCK = "release", "dont", "unblock"  # UNBLOCK: the Desktop's default button, same as Release
CHOICES = ({"label": "Release", "value": RELEASE}, {"label": "Don't", "value": DONT})
_WHY = {"allowance_exhausted": "its ten-run reviewer allowance is used up and an agent release cannot replace it",
        "allowance_in_force": "the Board still holds its reviewer allowance and an agent release cannot replace it"}


class OfferRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_offer(ask: Any) -> bool:
    """``ask`` carries this module's Release / Don't choices (the digest-checked filing proves who wrote them)."""
    return isinstance(ask, dict) and ask.get("choices") == [dict(c) for c in CHOICES]


def candidate(conn: sqlite3.Connection, task_id: str) -> bool:
    """Local reads only: a release could be offered and the Board may hold an allowance, so :func:`human_only`
    is worth its one Board read."""
    from hermes_cli import kanban_release
    from hermes_cli import kanban_spend_allowance as allowance
    target = kanban_release.held(conn, task_id)
    return target is not None and allowance.history(conn, target)


def human_only(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """``allowance_exhausted`` / ``allowance_in_force`` when an agent release of the card ``task_id`` stands for
    would be refused for that reason, else None. One bounded authenticated Board read, never under a transaction;
    an unknown read is None (an agent release then refuses ``allowance_unreadable``, a transient wall)."""
    from hermes_cli import kanban_allowance_observation as observation
    from hermes_cli import kanban_release
    if conn.in_transaction or not candidate(conn, task_id):
        return None
    target = kanban_release.held(conn, task_id)
    return kanban_release.agent_allowance_refusal(conn, target, observation.board_state(conn, target))


def _board(conn: sqlite3.Connection) -> str:
    from hermes_cli.kanban_db_connect import _main_db_file
    from hermes_cli.kanban_db_sync import board_slug_for_path
    path = _main_db_file(conn)
    return (board_slug_for_path(path) if path else None) or "default"


def request(conn: sqlite3.Connection, row: dict, code: Optional[str], *, gateway: str,
            note: Optional[str] = None) -> dict:
    """The stamped ask, in the unblock-card shape: the card as a link, what it is, why it stopped, the cost, and
    the Release / Don't buttons. Caller checked that ``kanban_release.offer`` names a release."""
    from hermes_cli import kanban_release
    from hermes_cli import kanban_stranded_brief as brief
    from hermes_cli.kanban_human_request import validate_human_request
    from hermes_cli.kanban_stranded_park import park_entry
    offer = kanban_release.offer(conn, row["id"])
    stopped = offer["task"]
    source = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (stopped,)).fetchone())
    what, why, _ = brief.context(conn, source, park_entry(conn, stopped))
    runs = kanban_release.default_runs()
    link = f"@task:{_board(conn)}@{gateway}/{stopped}" if gateway else f"@task:{_board(conn)}/{stopped}"
    only = _WHY.get(code or "", "the owning chat or a delegate can release it too")
    action = (f"Release {link} ({brief._text(source.get('title') or what, 120)})? Release (same as Unblock) = it "
              f"resumes to finish its acceptance with {runs} more runs under a goal-level scope; safety holds still "
              "apply. Don't = it stays stopped with no further spend; nothing is archived. (A person may also run "
              f"`hermes kanban release {stopped}`.)")
    ask = validate_human_request({
        "action": action,
        "why_human": f"Tried: an agent release; only a person can release it now: {only}.",
        "interaction": "approval",
        "scope": (f"Card: {link}. What it is: {what}\nWhy it stopped: {why}\nCost: up to {runs} more runs (each "
                  "a normal agent run).\nOptions:\nA) Release: finish the acceptance; it can be stopped again.\n"
                  "B) Don't: it stays stopped; archive it separately to drop it.\nRecommend: A - a scope or budget "
                  "stop is a pause, not a finding against the work."),
        "evidence": (f"{note}. " if note else "")
        + f"Pending stop {offer['episode']}; agent release refusal: {code or 'none read'}.",
        "choices": [dict(c) for c in CHOICES],
    }, kind=row.get("block_kind") or "needs_input")
    ask[kanban_release.STAMP] = offer  # after validation, which drops a supplied stamp: only the home sets it
    return ask


def linked_chats(conn: sqlite3.Connection, task_id: str, *, own: str) -> list:
    """``(origin, local)`` for every unreleased interactive chat linked to the card."""
    from hermes_cli.kanban_creator_chats import _chat_links
    from hermes_cli.kanban_stranded_park import _desk
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return []
    out = []
    for link in _chat_links(conn, task_id):
        chat = _desk(f"{link['chat_gateway']}/{link['chat_profile'] or 'default'}/{link['chat_session']}", own)
        if chat is not None:
            out.append(chat)
    return out


def requesting_chat(ref: Optional[str], *, own: str):
    """``(origin, local)`` for ``--for-chat``: a session ref, or a bare session id on this gateway's profile."""
    if not ref:
        return None
    from hermes_cli.kanban_stranded_park import _desk
    from hermes_cli.profiles import get_active_profile_name
    text = str(ref).strip()
    if "/" not in text and ":" not in text:
        text = f"{own}/{get_active_profile_name() or 'default'}/{text}"
    chat = _desk(text, own)
    if chat is None:
        raise OfferRefused("chat_invalid", f"--for-chat {ref!r} is not an interactive chat session")
    return chat


def _refusal(conn: sqlite3.Connection, task_id: str) -> Optional[OfferRefused]:
    from hermes_cli import kanban_release
    from hermes_cli.kanban_containment_guard import pending as contained
    from hermes_cli.kanban_home_transfer import frozen
    from hermes_cli.kanban_refusal_receipt import restricted
    from hermes_cli.kanban_spend_decision import execution_refusal
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        return OfferRefused("no_such_task", f"no card {task_id} on this board; offer it on its home gateway")
    stopped = kanban_release.target(conn, task_id)
    if frozen(conn, task_id):
        return OfferRefused("home_transfer", "home transfer owns this card; no offer filed")
    from hermes_cli.kanban_containment_connections import held as held_contained
    if any(check(conn, card) for check in (contained, held_contained) for card in (task_id, stopped)):
        return OfferRefused("containment_hold", kanban_release.CONTAINMENT)
    if restricted(conn, stopped):
        return OfferRefused("safety_restricted", execution_refusal(conn, stopped) or "Provider safety restriction")
    if kanban_release.offer(conn, task_id) is None:
        return OfferRefused("not_held", f"{task_id} waits on no spend or scope stop a release could end")
    from hermes_cli.kanban_release_reserved import reconcile
    try:
        if reconcile(conn, task_id, for_filing=True):
            return OfferRefused("application_pending", "An approval is reserved; recover its application, do not ask again")
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError):
        return OfferRefused("application_unreadable", "Cannot establish reserved application custody; no offer filed")
    return None


def _bind_chats(conn: sqlite3.Connection, task_id: str, chats, gateway: str, now: int) -> None:
    """An offer recipient shepherds this card, even if an earlier comment subscribed it only to notices.

    Preserve routing metadata, delivery mode and cursor; promote only notice-only provenance. Binding before
    the filing event also keeps a new subscriber's cursor behind the event it must receive.
    """
    from hermes_cli import kanban_db_notify as notify
    from hermes_cli import kanban_stranded_park as stranded
    linked = [origin for origin, _ in linked_chats(conn, task_id, own=gateway)]
    for chat in chats:
        stranded._subscribe_desk(conn, task_id, chat)
        origin, local = chat
        key = (task_id, "tui" if local else "hub",
               origin.session if local else f"{origin.gateway}/{origin.profile}/{origin.session}")
        row = conn.execute("SELECT delivery_metadata FROM kanban_notify_subs "
                           "WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=''", key).fetchone()
        metadata = notify._decode_notify_delivery_metadata(row[0])
        if metadata.get("reason") in notify.UNCOUNTED_SUB_REASONS:
            metadata["reason"] = stranded.SHEPHERD_KIND
            conn.execute("UPDATE kanban_notify_subs SET delivery_metadata=? "
                         "WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=''",
                         (json.dumps(metadata), *key))
        if origin not in linked:
            stranded._link_desk(conn, task_id, chat, now)


def file(conn: sqlite3.Connection, task_id: str, **kwargs) -> dict:
    """Commit reserved-approval cleanup before reporting a filing refusal."""
    result = _file(conn, task_id, **kwargs)
    if isinstance(result, OfferRefused):
        raise result
    return result


def _file(conn: sqlite3.Connection, task_id: str, *, code: Optional[str], chats=(), gateway: str,
          episode_entry: Optional[dict] = None, sweep: bool = False, now: Optional[int] = None,
          note: Optional[str] = None) -> dict | OfferRefused:
    """File (or confirm) the stamped human-only offer on ``task_id`` and bind ``chats``. Releases nothing.

    ``sweep``: the stranded-park sweep's once-per-park-episode filing, rechecked under the write lock
    (``_escalated``). An explicit request re-files only when the stored ask is not already this live offer.
    Returns ``{outcome: offered|already_offered, task, target, episode}`` or raises :class:`OfferRefused`."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_release
    from hermes_cli import kanban_stranded_park as stranded
    from hermes_cli.kanban_ask_resolved import store_request
    from hermes_cli.kanban_human_request import NONHUMAN_KINDS
    now = int(now if now is not None else time.time())
    with kb.write_txn(conn):
        refused = _refusal(conn, task_id)
        if refused is not None:
            return refused  # let the transaction commit suppression before the public refusal
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if row["status"] not in stranded.PARKED:
            raise OfferRefused("not_held", f"{task_id} is {row['status']}, not parked on a stop")
        if sweep and stranded._escalated(conn, task_id, episode_entry):
            return {"outcome": "already_offered", "task": task_id}
        offer = kanban_release.offer(conn, task_id)
        try:
            ask = json.loads(row.get("human_request") or "null")
        except (TypeError, ValueError):
            ask = None
        _bind_chats(conn, task_id, chats, gateway, now)
        outcome = "already_offered"
        if not (is_offer(ask) and kanban_release.offered(conn, task_id, ask) == offer["task"]):
            outcome, kind = "offered", row.get("block_kind")
            if kind in NONHUMAN_KINDS or not kind:
                kind = "needs_input"
                conn.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (kind, task_id))
            filed = request(conn, {**row, "block_kind": kind}, code, gateway=gateway, note=note)
            store_request(conn, task_id, filed)
            entry = stranded.park_entry(conn, task_id)
            kb._append_event(conn, task_id, stranded.EVENT, {
                "episode_start": entry["created_at"] if entry else int(row.get("created_at") or 0),
                "reason": stranded._reason(entry), "desk_chat": None, "request": True, "human_only": code,
                "chats": [f"{o.gateway}/{o.profile}/{o.session}" for o, _ in chats], **stranded._record(filed)})
    return {"outcome": outcome, "task": task_id, "target": offer["task"], "episode": offer["episode"]}


def offer_release(conn: sqlite3.Connection, task_id: str, *, for_chat: Optional[str] = None) -> dict:
    """The ``offer-release`` operation on this home (CLI and tool entry)."""
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_wake_check import foreign_ids
    own = gateway_names()[0]
    if task_id in foreign_ids(conn):
        raise OfferRefused("not_home", f"{task_id} is a copy of another gateway's card; its home files the offer")
    chats = linked_chats(conn, task_id, own=own)
    asked = requesting_chat(for_chat, own=own)
    if asked is not None and asked[0] not in [o for o, _ in chats]:
        chats.append(asked)
    return file(conn, task_id, code=human_only(conn, task_id), chats=chats, gateway=own)


def command(args) -> int:
    from hermes_cli import kanban_db_connect as kbc
    try:
        with kbc.connect_closing() as conn:
            out = {"ok": True, **offer_release(conn, args.task_id, for_chat=getattr(args, "for_chat", None))}
    except OfferRefused as exc:
        out = {"ok": False, "outcome": "refused", "refusal": exc.code, "message": str(exc), "task": args.task_id}
    if getattr(args, "json", False):
        print(json.dumps(out))
    elif out["ok"]:
        print(f"{out['outcome'].replace('_', ' ')}: {out['task']} shows a Release / Don't ask for stop "
              f"{out['episode']}; only a person's answer releases {out['target']}")
    else:
        import sys
        print(f"cannot offer a release on {out['task']} ({out['refusal']}): {out['message']}", file=sys.stderr)
    return 0 if out["ok"] else 1
