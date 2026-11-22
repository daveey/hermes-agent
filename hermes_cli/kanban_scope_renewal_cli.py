"""``hermes kanban renew-scope``: who is asking, and where the renewal is decided. [daveey]

The caller is identified exactly as ``hermes kanban comment`` identifies a comment's writer
(``kanban_comment_writer.cli_comment_writer``); no flag names a role or session. On a synced board the canonical
Board decides and writes the ``scope_renewed`` event (one ``renew_scope`` op, so a card whose home is another
gateway needs no courier); a board that never synced validates and appends in one local transaction.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from typing import Mapping, Optional

from hermes_cli import kanban_scope_renewal as renewal
from hermes_cli.kanban_scope_renewal import Actor, RenewalRefused

_TASK_MARKERS = ("HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_TASK")
_MARKED = re.compile(r"\d+:[A-Z][A-Z0-9_]*")  # an agent-marker ancestor; not a runner, a guess or a handle


def _sessions(conn: sqlite3.Connection, task_id: str) -> set:
    return renewal._creator_sessions(conn, task_id)


def owning_link(conn: sqlite3.Connection, task_id: str, *, gateway: str, profile: str, session: str) -> Optional[dict]:
    """This chat's unreleased ``created`` link (or the legacy owning subscription ``_chat_links`` projects).

    ``shepherd``/``touch`` rows are observer subscriptions any chat can add to any card: never authority.
    """
    from hermes_cli.kanban_creator_chats import _chat_links
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return _owning_sub(conn, task_id, session)
    for link in _chat_links(conn, task_id):
        if (link.get("kind", "created") == "created" and link["chat_session"] == session
                and link["chat_gateway"] == gateway and (link.get("chat_profile") or "default") == profile):
            return link
    return None


def _owning_sub(conn: sqlite3.Connection, task_id: str, session: str) -> Optional[dict]:
    """A board that never synced has no chat links: the chat whose subscription was stamped at creation owns."""
    from hermes_cli.kanban_db_notify import OBSERVER_SUB_REASONS
    for row in conn.execute("SELECT chat_id, delivery_metadata FROM kanban_notify_subs WHERE task_id=? AND chat_id=?",
                            (task_id, session)):
        try:
            reason = json.loads(row[1] or "{}").get("reason")
        except (TypeError, ValueError, AttributeError):
            continue
        if reason and reason not in OBSERVER_SUB_REASONS:
            return {"chat_session": session}
    return None


def resolve_actor(conn: sqlite3.Connection, task_id: str, *, writer: tuple, env: Mapping[str, str],
                  gateway: str, profile: str) -> Actor:
    """The renewing authority, or :class:`RenewalRefused`. The stopped card's own runs are refused first."""
    from hermes_cli.kanban_archive_scope import _creator_task_id
    kind, session, evidence = writer
    if kind == "human":
        return Actor("human", None, "human", None, gateway, profile)
    marked = {str(env.get(name) or "").strip() for name in _TASK_MARKERS} - {""}
    if task_id in marked or (session and session in _sessions(conn, task_id)):
        raise RenewalRefused("own_run", f"a run of {task_id} cannot renew its own stopped scope")
    if kind != "agent" or not session or not _MARKED.fullmatch(str(evidence)):
        raise RenewalRefused("identity_unavailable", "the caller's identity is missing or ambiguous "
                             f"({evidence}); renew from the owning chat, the creator task or a human terminal")
    if len(marked) > 1:
        raise RenewalRefused("identity_unavailable", "the caller carries two different task identities")
    creator = _creator_task_id(conn, task_id)
    if marked:
        own = next(iter(marked))
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (own,)).fetchone()
        if own != creator or row is None or row[0] != "running" or session not in _sessions(conn, own):
            raise RenewalRefused("not_scope_authority", f"task {own} is not {task_id}'s running creator task")
        return Actor("agent", session, "creator_task", own, gateway, profile)
    if owning_link(conn, task_id, gateway=gateway, profile=profile, session=session) is None:
        raise RenewalRefused("not_scope_authority", f"this session is not {task_id}'s owning chat (an unreleased "
                             "created chat link); shepherd/touch subscriptions and unrelated agents cannot renew")
    return Actor("agent", session, "chat", f"@session:{profile}@{gateway}/{session}", gateway, profile)


def _canonical(conn: sqlite3.Connection, board: str, task_id: str, payload: dict, mode: str) -> dict:
    """Ask the canonical Board to revalidate and write the event; project its exact row into this replica."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    hub = client.Hub(client.load_identity())
    try:
        capability = hub.decision_retain_capability(board)
        if not isinstance(capability, dict) or capability.get("scope_renewal") != 1:
            raise RenewalRefused("board_unsupported", "the Board does not support scope renewal v1 yet; "
                                 "nothing was changed. Retry after the Board deployment")
        if mode == "primary":
            client.push(conn, hub, board, hub.ident.gateway, primary=True)  # the mandate comment must be canonical
        body = conn.execute("SELECT body FROM task_comments WHERE id=?", (payload["mandate_comment_id"],)).fetchone()[0]
        event_uid = sync.uuid7()
        audit = {k: payload[k] for k in ("actor_kind", "actor_session", "actor_profile", "authority_kind",
                                         "authority_ref", "reason")}
        wire = {"client_op_id": event_uid, "op": "renew_scope", "task": task_id, "event_uid": event_uid,
                "episode": payload["episode"], "mandate_comment_uid": payload["mandate_comment_uid"],
                "mandate_body": body, "payload": {"version": 1, **audit}}
        results = hub.ops(board, [wire]).get("results", [])
        if len(results) != 1 or results[0].get("client_op_id") != event_uid or results[0].get("status") != "applied":
            reply = results[0] if len(results) == 1 else {}
            raise RenewalRefused(str(reply.get("reason") or "board_rejected"),
                                 f"the Board refused the renewal: {reply.get('reason') or 'no reply'}"
                                 f"{': ' + str(reply['detail']) if reply.get('detail') else ''}")
        proof = hub.row_evidence(board, "task_events", event_uid)
        row = proof.get("row") if proof.get("state") == "live" else None
        if not isinstance(row, dict) or row.get("kind") != renewal.EVENT or row.get("task_id") != task_id:
            raise RenewalRefused("board_unconfirmed", "the Board applied the renewal but its event could not be "
                                 "read back; rerun `hermes kanban show` after the next sync")
        sync.apply_remote_changes(conn, [client._row_change("task_events", row)],
                                  new_cursor=sync.sync_state(conn)["cursor"], board=board)
        return json.loads(row["payload"])
    finally:
        hub.close()


def renew(conn: sqlite3.Connection, task_id: str, *, episode: str, comment_id: int, reason: str,
          board: Optional[str] = None, writer: Optional[tuple] = None,
          env: Optional[Mapping[str, str]] = None) -> dict:
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_comment_writer import cli_comment_writer
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    env = os.environ if env is None else env
    actor = resolve_actor(conn, task_id, writer=writer or cli_comment_writer(env=env), env=env,
                          gateway=gateway_names()[0], profile=get_active_profile_name() or "default")
    mode = sync.board_mode(conn)
    if mode == "off" and not sync.sync_state(conn):
        return renewal.renew_local(conn, task_id, episode=episode, comment_id=comment_id, reason=reason, actor=actor)
    if mode == "off":
        raise RenewalRefused("board_unavailable", "this board is synced but sync is not active in this process")
    payload = renewal.prepare(conn, task_id, episode=episode, comment_id=comment_id, reason=reason, actor=actor)
    return _canonical(conn, board or sync.sync_state(conn)["board_id"], task_id, payload, mode)


def command(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_db as kb
    try:
        with kb.connect_closing() as conn:
            payload = renew(conn, args.task_id, episode=args.episode, comment_id=args.mandate_comment,
                            reason=args.reason)
    except RenewalRefused as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": exc.code, "message": str(exc)}))
        else:
            print(f"renew-scope refused ({exc.code}): {exc}")
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "task_id": args.task_id, **payload}))
    else:
        print(f"Scope renewed on {args.task_id} for stop {payload['episode']} (mandate comment "
              f"{payload['mandate_comment_uid']}). The card is still in triage: "
              f"`hermes kanban promote {args.task_id} <what changed>` releases it.")
    return 0
