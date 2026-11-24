"""``hermes kanban delegate``: chat sessions a human granted scope-renewal rights. [daveey]

``renew-scope`` has four authorities: a human operator, the card's owning chat, its running creator task and a
``delegate``. A delegate is a chat session that owns none of the cards it is asked to unblock (a triage or desk
chat); without a grant it could only relay "unblock this" to the owning chats. A grant names one
(gateway, profile, session), or session ``*`` for every chat session of that gateway and profile, and expires.

* ``grant`` and ``revoke`` are a human operator's alone, classified exactly as ``renew-scope`` classifies a human
  (``kanban_comment_writer.cli_comment_writer``); an agent caller is refused ``not_human``. No flag asserts it.
* On a synced board the grant is canonical Board state (ops ``delegate_scope_grant`` / ``delegate_scope_revoke``,
  read back from ``GET /v2/boards/{board}/scope-delegates``); the Board advertises ``scope_delegate: 1`` next to
  ``scope_renewal: 1`` and nothing is sent without it (``board_unsupported``). A board that never synced keeps
  grants in the local ``scope_delegates`` table.
* A grant renews nothing by itself. :func:`live_grant` is what ``resolve_actor`` asks after every older rule has
  run: the stopped card's own runs, callers carrying a Kanban task marker and ambiguous identities never reach it.
  The delegate still writes its own mandate comment after the stop, and a safety restriction has no renewal.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from typing import Mapping, Optional

from hermes_cli.kanban_scope_renewal import MAX_REASON, RenewalRefused

ANY = "*"
DEFAULT_HOURS = 168.0
MIN_TTL_S, MAX_TTL_S = 60, 366 * 86400  # the Board's bounds
CAPABILITY = "scope_delegate"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_GATEWAY = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_LOCAL_SQL = """
CREATE TABLE IF NOT EXISTS scope_delegates (
    uid TEXT PRIMARY KEY, gateway TEXT NOT NULL, profile TEXT NOT NULL, session TEXT NOT NULL,
    granted_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, granted_by_gateway TEXT NOT NULL,
    granted_by_profile TEXT NOT NULL, reason TEXT NOT NULL,
    revoked_at INTEGER, revoked_by_gateway TEXT, revoke_reason TEXT
)"""
_FIELDS = ("uid", "gateway", "profile", "session", "granted_at", "expires_at", "granted_by_gateway",
           "granted_by_profile", "reason", "revoked_at", "revoked_by_gateway", "revoke_reason")


def _here() -> tuple[str, str]:
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name
    return gateway_names()[0], get_active_profile_name() or "default"


def _store(conn: sqlite3.Connection, board: Optional[str]) -> Optional[str]:
    """The canonical Board id, or None for a board that never synced (grants are local rows).

    A synced board whose sync is not active in this process has no usable store: nothing is read or written."""
    from hermes_cli import kanban_db_sync as sync
    state, mode = sync.sync_state(conn), sync.board_mode(conn)
    if mode == "off" and not state:
        return None
    if mode == "off":
        raise RenewalRefused("board_unavailable", "this board is synced but sync is not active in this process")
    return board or state["board_id"]


def _has_local(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scope_delegates'").fetchone() is not None


def _view(row: Mapping, now: int, mine: bool) -> dict:
    grant = {k: row[k] if k in row.keys() else None for k in _FIELDS}
    return {**grant, "live": grant["revoked_at"] is None and int(grant["expires_at"] or 0) > now, "mine": mine}


def _board_grants(hub, board: str) -> Optional[list[dict]]:
    """Every grant the Board holds as it reads them now, or None when it does not support delegates."""
    capability = hub.decision_retain_capability(board)
    if not isinstance(capability, dict) or capability.get(CAPABILITY) != 1:
        return None
    reply = hub.scope_delegates(board)
    rows = reply.get("grants") if isinstance(reply, dict) and reply.get("version") == 1 else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r.get("uid") for r in rows):
        raise RenewalRefused("board_unavailable", "the Board's delegate grants could not be read")
    now = int(reply.get("now") or time.time())
    # ``live`` and ``mine`` are the Board's reading (its clock, its record of this gateway's former names).
    return [{**_view(r, now, r.get("mine") is True), "live": r.get("live") is True} for r in rows]


def _with_hub(fn):
    """Run ``fn(hub, client)``; a Board that cannot be reached changes and proves nothing."""
    from hermes_cli import kanban_sync_client as client
    hub = client.Hub(client.load_identity())
    try:
        return fn(hub, client)
    except (client.HubError, client.SyncUnavailable, OSError, TimeoutError) as exc:
        raise RenewalRefused("board_unavailable", f"the Board could not be reached ({exc}); nothing was changed") from exc
    finally:
        hub.close()


def grants(conn: sqlite3.Connection, board: Optional[str] = None) -> list[dict]:
    """Live and ended grants, newest first. ``board_unsupported`` on a synced board without the capability."""
    target = _store(conn, board)
    if target is None:
        if not _has_local(conn):
            return []
        now, here = int(time.time()), _here()[0]
        return [_view(r, now, r["gateway"] == here) for r in
                conn.execute("SELECT * FROM scope_delegates ORDER BY granted_at DESC, uid DESC")]
    found = _with_hub(lambda hub, _client: _board_grants(hub, target))
    if found is None:
        raise RenewalRefused("board_unsupported", "the Board does not support delegated scope renewal yet; "
                             "nothing was changed. Retry after the Board deployment")
    return found


def live_grant(conn: sqlite3.Connection, board: Optional[str], *, gateway: str, profile: str,
               session: str) -> Optional[str]:
    """Uid of the live grant covering this chat session (its own before ``*``), else None.

    A Board without the capability holds no grant, and a synced board whose sync is not active here cannot be
    asked: both answer None, so the caller's refusal stays what it was before delegates existed."""
    from hermes_cli import kanban_db_sync as sync
    if sync.board_mode(conn) == "off" and sync.sync_state(conn):
        return None
    target = _store(conn, board)
    if target is None:
        if not _has_local(conn):
            return None
        now = int(time.time())
        found = [_view(r, now, True) for r in conn.execute(
            "SELECT * FROM scope_delegates WHERE gateway=? AND profile=? AND session IN (?,?)",
            (gateway, profile, session, ANY))]
    else:
        found = [g for g in (_with_hub(lambda hub, _client: _board_grants(hub, target)) or [])
                 if g["mine"] and (g["profile"] or "default") == profile and g["session"] in (session, ANY)]
    found = sorted((g for g in found if g["live"]), key=lambda g: (g["session"] == ANY, -int(g["granted_at"] or 0)))
    return str(found[0]["uid"]) if found else None


def _board_gateway(gateway: str) -> str:
    """The delegate gateway for a Board op: the one named or this gateway's hub name, else the name the Board
    authenticates this gateway as (a host with a Board identity but no hermes-hub mail registry)."""
    gateway = gateway or _with_hub(lambda hub, _client: str(getattr(hub.ident, "gateway", "") or ""))
    if not _GATEWAY.fullmatch(gateway):
        raise RenewalRefused("invalid_gateway", f"{gateway!r} is not a gateway name")
    return gateway


def _human(writer: Optional[tuple], env: Optional[Mapping[str, str]], verb: str) -> None:
    from hermes_cli.kanban_comment_writer import cli_comment_writer
    kind, _session, evidence = writer or cli_comment_writer(env=os.environ if env is None else env)
    if kind != "human":
        raise RenewalRefused("not_human", f"only a human operator may {verb} delegated scope-renewal authority "
                             f"(this caller is an agent: {evidence}); run it from a human terminal")


def _key(session: str, gateway: Optional[str], profile: Optional[str], reason: str) -> tuple[str, str, str, str, str, str]:
    here, active = _here()
    gateway, profile, session, reason = gateway or here, profile or active, (session or "").strip(), (reason or "").strip()
    if session != ANY and not _NAME.fullmatch(session):
        raise RenewalRefused("invalid_session", "--session is a chat session id, or '*' for every chat session "
                             "of the gateway and profile")
    if not _NAME.fullmatch(profile):
        raise RenewalRefused("invalid_profile", f"{profile!r} is not a profile name")
    if not reason or len(reason) > MAX_REASON:
        raise RenewalRefused("invalid_reason", f"--reason is required (at most {MAX_REASON} characters)")
    return session, gateway, profile, reason, here, active


def _send(board: str, wire: dict, uid: Optional[str]) -> dict:
    """One Board op, then the Board's own record of it. Returns ``{"detail", "grant"}``."""
    def ask(hub, _client):
        if _board_grants(hub, board) is None:
            raise RenewalRefused("board_unsupported", "the Board does not support delegated scope renewal yet; "
                                 "nothing was changed. Retry after the Board deployment")
        results = hub.ops(board, [wire]).get("results", [])
        if len(results) != 1 or results[0].get("client_op_id") != wire["client_op_id"] or results[0].get("status") != "applied":
            reply = results[0] if len(results) == 1 else {}
            raise RenewalRefused(str(reply.get("reason") or "board_rejected"),
                                 f"the Board refused: {reply.get('reason') or 'no reply'}"
                                 f"{': ' + str(reply['detail']) if reply.get('detail') else ''}")
        after = _board_grants(hub, board) or []
        grant = next((g for g in after if g["uid"] == uid), None)
        if uid and grant is None:
            raise RenewalRefused("board_unconfirmed", "the Board applied the grant but it could not be read back; "
                                 "rerun `hermes kanban delegate list`")
        return {"detail": results[0].get("detail"), "grant": grant}
    return _with_hub(ask)


def grant(conn: sqlite3.Connection, *, session: str, reason: str, gateway: Optional[str] = None,
          profile: Optional[str] = None, hours: float = DEFAULT_HOURS, board: Optional[str] = None,
          writer: Optional[tuple] = None, env: Optional[Mapping[str, str]] = None) -> dict:
    """Grant ``session`` (or ``*``) of ``gateway``/``profile`` renewal rights for ``hours``. Human only.

    ``writer``/``env`` are test seams, as on ``renew``. One grant is live per key: a new one supersedes the old uid."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    _human(writer, env, "grant")
    session, gateway, profile, reason, here, active = _key(session, gateway, profile, reason)
    try:
        ttl = int(float(hours) * 3600)
    except (TypeError, ValueError, OverflowError):
        ttl = 0
    if not MIN_TTL_S <= ttl <= MAX_TTL_S:
        raise RenewalRefused("invalid_hours", "--hours must be between one minute and 366 days")
    target, uid = _store(conn, board), sync.uuid7()
    if target is not None:
        gateway = _board_gateway(gateway)
        wire = {"client_op_id": uid, "op": "delegate_scope_grant", "grant_uid": uid, "gateway": gateway,
                "profile": profile, "session": session, "ttl_s": ttl,
                "payload": {"version": 1, "actor_kind": "human", "actor_profile": active, "reason": reason}}
        return {**_send(target, wire, uid)["grant"], "store": "board"}
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(_LOCAL_SQL)
        conn.execute("UPDATE scope_delegates SET revoked_at=?, revoked_by_gateway=?, revoke_reason=? WHERE gateway=? "
                     "AND profile=? AND session=? AND revoked_at IS NULL AND expires_at>?",
                     (now, here, f"superseded by {uid}", gateway, profile, session, now))
        conn.execute("INSERT INTO scope_delegates (uid,gateway,profile,session,granted_at,expires_at,"
                     "granted_by_gateway,granted_by_profile,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                     (uid, gateway, profile, session, now, now + ttl, here, active, reason))
        row = conn.execute("SELECT * FROM scope_delegates WHERE uid=?", (uid,)).fetchone()
    return {**_view(row, now, gateway == here), "store": "local"}


def revoke(conn: sqlite3.Connection, *, session: str, reason: str, gateway: Optional[str] = None,
           profile: Optional[str] = None, board: Optional[str] = None, writer: Optional[tuple] = None,
           env: Optional[Mapping[str, str]] = None) -> list[str]:
    """End the live grant of exactly this key; its uid(s). Human only. ``no_live_grant`` when there is none."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    _human(writer, env, "revoke")
    session, gateway, profile, reason, here, active = _key(session, gateway, profile, reason)
    target = _store(conn, board)
    if target is not None:
        gateway = _board_gateway(gateway)
        wire = {"client_op_id": sync.uuid7(), "op": "delegate_scope_revoke", "gateway": gateway, "profile": profile,
                "session": session,
                "payload": {"version": 1, "actor_kind": "human", "actor_profile": active, "reason": reason}}
        return [u for u in str(_send(target, wire, None)["detail"] or "").split(",") if u]
    now = int(time.time())
    with kb.write_txn(conn):
        live = [r[0] for r in conn.execute(
            "SELECT uid FROM scope_delegates WHERE gateway=? AND profile=? AND session=? AND revoked_at IS NULL "
            "AND expires_at>?", (gateway, profile, session, now))] if _has_local(conn) else []
        if not live:
            raise RenewalRefused("no_live_grant", "no live delegate grant names this gateway, profile and session")
        for uid in live:
            conn.execute("UPDATE scope_delegates SET revoked_at=?, revoked_by_gateway=?, revoke_reason=? WHERE uid=?",
                         (now, here, reason, uid))
    return live


def _state(grant: dict) -> str:
    return "live" if grant["live"] else "revoked" if grant["revoked_at"] is not None else "expired"


def command(args: argparse.Namespace) -> int:
    from hermes_cli import kanban_db_connect as kbc
    action, as_json = getattr(args, "delegate_action", None), getattr(args, "json", False)
    if action not in ("grant", "revoke", "list"):
        print("usage: hermes kanban delegate {grant,revoke,list}")
        return 2
    try:
        with kbc.connect_closing() as conn:
            if action == "list":
                found = grants(conn)
                if as_json:
                    print(json.dumps({"ok": True, "grants": found}))
                else:
                    for g in found:
                        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(g["expires_at"])))
                        print(f"{_state(g):8} {g['uid']}  {g['session']} @ {g['profile']}@{g['gateway']}  "
                              f"until {when}  ({g['reason']})")
                    if not found:
                        print("No delegate grants.")
                return 0
            if action == "grant":
                made = grant(conn, session=args.session, gateway=args.gateway, profile=args.profile,
                             hours=args.hours, reason=args.reason)
                out = {"ok": True, "grant": made}
                text = (f"Granted {made['session']} @ {made['profile']}@{made['gateway']} scope-renewal rights "
                        f"until {time.strftime('%Y-%m-%d %H:%M', time.localtime(int(made['expires_at'])))} "
                        f"(grant {made['uid']}).")
            else:
                ended = revoke(conn, session=args.session, gateway=args.gateway, profile=args.profile,
                               reason=args.reason)
                out, text = {"ok": True, "revoked": ended}, f"Revoked delegate grant {', '.join(ended)}."
    except RenewalRefused as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": exc.code, "message": str(exc)}))
        else:
            print(f"delegate {action} refused ({exc.code}): {exc}")
        return 1
    print(json.dumps(out) if as_json else text)
    return 0
