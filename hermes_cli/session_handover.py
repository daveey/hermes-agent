"""Cooperative session handover: ``hermes sessions move`` and its target-side verbs. [daveey]

A chat session moves to another gateway while this one (its current owner) is alive and signs the move:

1. refuse while a turn runs (``--after-turn`` waits for it to end, polling up to 10 min);
2. export the whole compression lineage (every message row, ``include_inactive``) plus ``source_home``;
3. push it HIDDEN to the target through the transport and verify the per-segment message counts;
4. CAS the owner record on the Board (:mod:`hermes_cli.session_owner`; epoch from the current record, 0 if none);
5. reveal the copy on the target;
6. archive the copy here (``end_reason='moved_to_gateway:<gw>'``);
7. re-point this gateway's open cards that notify the chat to the new gateway's copy (:func:`repoint_links`).

Open cards owned by OTHER gateways that notify the chat (``kanban_chat_links.replica_cards``) cannot be re-pointed
from here: their notices would reach the archived copy. Such a move is refused (``remote_cards``, naming them)
unless ``--force``; wait for those cards to finish, or force and read their results on their own gateway.

If the CAS fails after the push, the hidden target copy is deleted through the transport and the move reports it.

Transport (config ``sessions.move``)::

    sessions:
      move:
        hosts: {hub: ubuntu@hub-host}        # gateway -> ssh target; a move needs its target listed here
        transport: "ssh -o BatchMode=yes {target} {command}"   # default; {args} expands to separate argv words
        remote_hermes: hermes                # the hermes executable on the target
        profile: default                     # the chat's profile on the target (default: this profile)

The target side is ``hermes sessions import-lineage <file|-> [--hidden]`` (prints a JSON count per segment),
``hermes sessions reveal <id>`` (unhide) and ``hermes sessions discard-hidden <id>`` (delete a copy that is still
hidden: the undo of a push whose CAS failed; a visible session is never deleted).

Turn running = the session's durable turn lease (state.db ``session_turn_leases``, held from admission to flush of
every turn, by any surface). The active-session registry slot is NOT used for this: Desktop holds it for the life
of an open chat, so it would read "a turn runs" between turns and ``--after-turn`` could never proceed.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

_log = logging.getLogger(__name__)

END_REASON_PREFIX = "moved_to_gateway:"
AFTER_TURN_WAIT_S = 600.0
POLL_S = 2.0
DEFAULT_TRANSPORT = "ssh -o BatchMode=yes {target} {command}"
OPEN_STATUSES = ("todo", "ready", "running", "scheduled", "blocked", "review", "triage")
TRANSPORT_TIMEOUT_S = 600.0


class MoveError(RuntimeError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.extra = code, extra


# --- pure helpers ------------------------------------------------------------------------------------------------

def remap_cwd(cwd: Optional[str], src_home: str, dst_home: str) -> tuple[Optional[str], Optional[str]]:
    """``(new cwd, None)`` or ``(None, reason)``. No cwd stays no cwd (hermes-session-move's rule)."""
    if not cwd:
        return cwd, None
    src = str(src_home).rstrip("/")
    if cwd == src:
        return dst_home, None
    if cwd.startswith(src + "/"):
        return str(dst_home).rstrip("/") + cwd[len(src):], None
    return None, f"cwd {cwd} is outside {src}"


def move_config(cfg: Optional[dict] = None) -> dict[str, Any]:
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = load_config_readonly() or {}
        except Exception:  # health: allow BLE001 -- unreadable config: no hosts, every move is refused
            cfg = {}
    sessions = cfg.get("sessions") if isinstance(cfg, dict) else None
    move = (sessions or {}).get("move") if isinstance(sessions, dict) else None
    return dict(move) if isinstance(move, dict) else {}


def move_hosts(cfg: Optional[dict] = None) -> dict[str, str]:
    hosts = move_config(cfg).get("hosts")
    return {str(k): str(v) for k, v in hosts.items() if k and v} if isinstance(hosts, dict) else {}


def transport_argv(target: str, hermes_args: list[str], cfg: Optional[dict] = None) -> list[str]:
    """The argv that runs ``<remote_hermes> <hermes_args>`` on ``target``: the ``sessions.move.transport`` template
    (a string, shlex-split, or a list) with ``{target}``, ``{command}`` (the whole remote command as one word, for
    ssh) and ``{args}`` (the remote command as separate words, for a local transport)."""
    mc = move_config(cfg)
    remote = mc.get("remote_hermes") or "hermes"
    remote_words = shlex.split(remote) if isinstance(remote, str) else [str(w) for w in remote]
    words = [*remote_words, *hermes_args]
    template = mc.get("transport") or DEFAULT_TRANSPORT
    tokens = shlex.split(template) if isinstance(template, str) else [str(t) for t in template]
    out: list[str] = []
    for tok in tokens:
        if tok == "{args}":
            out.extend(words)
        else:
            out.append(tok.replace("{target}", target).replace("{command}", shlex.join(words)))
    return out


def _last_json(text: str) -> Optional[dict]:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def run_transport(target: str, hermes_args: list[str], *, payload: Optional[str] = None,
                  cfg: Optional[dict] = None) -> dict[str, Any]:
    """Run a target-side verb; its last JSON line, plus ``rc`` (``ok`` False with ``error`` when none came back)."""
    argv = transport_argv(target, hermes_args, cfg)
    try:
        p = subprocess.run(argv, input=payload, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TRANSPORT_TIMEOUT_S,
                           check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "rc": None, "error": f"{type(exc).__name__}: {exc}"}
    res = _last_json(p.stdout)
    if res is None:
        return {"ok": False, "rc": p.returncode, "error": ((p.stderr or "") + (p.stdout or ""))[-600:]}
    res.setdefault("ok", p.returncode == 0)
    res["rc"] = p.returncode
    return res


# --- target side ---------------------------------------------------------------------------------------------------

def _open_db():
    from hermes_state import SessionDB
    return SessionDB()


def _count(db, sid: str) -> int:
    return len(db.get_messages(sid, include_inactive=True))


def _handed_away(db, sid: str) -> bool:
    """A copy this gateway once handed to another one (archived ``moved_to_gateway:``): a move back replaces it."""
    row = db.get_session(sid)
    return bool(row and row.get("archived") and str(row.get("end_reason") or "").startswith(END_REASON_PREFIX))


def _lift_deletion_fence(db, session_ids: list) -> None:
    """[daveey] A handed-away copy replaced by the chat coming back, or an unrevealed push discarded after a
    failed move, is not deleted by the user. Upstream's
    ``delete_session`` fences every deleted id for good (``hermes_state_mutation_retirement``) and
    ``import_sessions`` then refuses that id, so a move back would drop the chat on both gateways. Clear only
    the id marker; the delete's receipt/media retirement stands, and the import recreates the row."""
    from hermes_state_mutation_retirement import RETIRED_PREFIX

    def _do(conn):
        conn.executemany("DELETE FROM state_meta WHERE key = ?", [(RETIRED_PREFIX + sid,) for sid in session_ids])

    db._execute_write(_do)


def import_lineage(payload: dict, *, hidden: bool = False, db=None, home: Optional[str] = None) -> dict[str, Any]:
    """Import an ``export_session_lineage(include_inactive=True)`` payload (``source_home`` remapped to this home)."""
    own = db is None
    db = db or _open_db()
    try:
        segments = [dict(s) for s in (payload.get("segments") or [payload])]
        src = payload.get("source_home")
        dst = home or str(Path.home())
        for seg in segments:
            seg.pop("segments", None)
            if src:
                new, why = remap_cwd(seg.get("cwd"), src, dst)
                seg["cwd"] = None if why else new
        replaced = [s["id"] for s in segments if s.get("id") and _handed_away(db, s["id"])]
        for sid in reversed(replaced):  # the tip first: a compression child never outlives its parent here
            db.delete_session(sid)
        if replaced:
            _lift_deletion_fence(db, replaced)
        res = db.import_sessions(segments)
        ids = [s["id"] for s in segments if s.get("id")]
        if res.get("ok") and hidden:
            for sid in ids:
                db.set_session_hidden(sid, True)
        counts = {sid: _count(db, sid) for sid in ids if db.get_session(sid)}
        return {"ok": bool(res.get("ok")), "imported": res.get("imported"), "skipped": res.get("skipped"),
                "replaced": replaced, "errors": res.get("errors"), "hidden": bool(hidden), "counts": counts}
    finally:
        if own:
            db.close()


def reveal(session_id: str, *, db=None) -> dict[str, Any]:
    own = db is None
    db = db or _open_db()
    try:
        lineage = db.get_compression_lineage(session_id) or ([session_id] if db.get_session(session_id) else [])
        if not lineage:
            return {"ok": False, "error": f"no session {session_id}"}
        for sid in lineage:
            db.set_session_hidden(sid, False)
        return {"ok": True, "revealed": lineage}
    finally:
        if own:
            db.close()


def discard_hidden(session_ids: Iterable[str], *, db=None) -> dict[str, Any]:
    """Delete sessions that are still hidden (an unrevealed push). A visible one is left and reported."""
    own = db is None
    db = db or _open_db()
    try:
        deleted, kept = [], []
        for sid in reversed(list(session_ids)):
            row = db.get_session(sid)
            if row is None:
                continue
            if not row.get("hidden"):
                kept.append(sid)
                continue
            if db.delete_session(sid):
                deleted.append(sid)
        if deleted:  # an unrevealed push, not a user delete: a later move may bring the chat here again
            _lift_deletion_fence(db, deleted)
        return {"ok": not kept, "deleted": deleted, "kept_visible": kept}
    finally:
        if own:
            db.close()


# --- source side ---------------------------------------------------------------------------------------------------

def turn_running(db, session_id: str) -> bool:
    """A live durable turn lease on the session's lineage (any surface, any process)."""
    try:
        owner = db.session_turn_lease_owner(session_id)
    except Exception:  # health: allow BLE001 -- no lease table yet: nothing ever ran a turn
        return False
    return bool(owner) and float(owner.get("expires_at") or 0) > time.time()


def wait_for_turn_end(db, session_id: str, *, wait_s: float = AFTER_TURN_WAIT_S, poll_s: float = POLL_S,
                      sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + wait_s
    while turn_running(db, session_id):
        if time.monotonic() >= deadline:
            return False
        sleep(poll_s)
    return True


def _profile(cfg: Optional[dict]) -> str:
    configured = move_config(cfg).get("profile")
    if configured:
        return str(configured)
    try:
        from hermes_cli.profiles import current_profile_name
        name = current_profile_name("default") or "default"
    except Exception:  # health: allow BLE001 -- unknown profile: the default one
        name = "default"
    return "default" if name == "custom" else name


def _board_conns():
    """``(slug, path)`` for every kanban board file on this host (the default board and named boards)."""
    from hermes_cli import kanban_db
    root = kanban_db.kanban_home()
    found = [("default", root / "kanban.db")] if (root / "kanban.db").exists() else []
    boards = kanban_db.boards_root()
    if boards.is_dir():
        found += [(p.parent.name, p) for p in sorted(boards.glob("*/kanban.db")) if p.is_file()]
    return found


def repoint_links(lineage: list[str], to: str, *, profile: str) -> list[dict[str, Any]]:
    """Open cards on this gateway's boards created by the lineage (``tasks.session_id``) or shepherded by it
    (``tui`` subscriptions) notify ``(to, profile, same session id)`` from now on: a ``platform='hub'`` subscription
    (kanban_origin.bind, the cross-gateway notice path hermes-hub publishes as a ``shepherd`` session link and the
    target's ``kanban_remote_notices`` delivers), and the local ``tui`` subscription is removed so the archived copy
    here is never notified."""
    from hermes_cli import kanban_db_notify as kbn
    from hermes_cli import kanban_origin as ko
    from hermes_cli.kanban_db_connect import connect_closing
    ids = [str(i) for i in lineage if i]
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    st = ",".join("?" * len(OPEN_STATUSES))
    out: list[dict[str, Any]] = []
    for slug, path in _board_conns():
        with connect_closing(path) as conn:
            cards: dict[str, set[str]] = {}
            for tid, sid in conn.execute(
                    f"SELECT id, session_id FROM tasks WHERE status IN ({st}) AND session_id IN ({q})",
                    (*OPEN_STATUSES, *ids)):
                cards.setdefault(tid, set()).add(sid)
            subs: dict[str, set[str]] = {}
            try:
                for tid, chat in conn.execute(
                        f"SELECT s.task_id, s.chat_id FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                        f"WHERE s.platform = 'tui' AND s.chat_id IN ({q}) AND t.status IN ({st})",
                        (*ids, *OPEN_STATUSES)):
                    subs.setdefault(tid, set()).add(chat)
                    cards.setdefault(tid, set())
            except Exception:  # health: allow BLE001 -- a board without notify subscriptions
                pass
            for tid in sorted(cards):
                chats = sorted(subs.get(tid) or cards[tid])
                for chat in chats:
                    ko.bind(conn, tid, ko.Origin(to, profile, chat), board=slug)
                for chat in sorted(subs.get(tid) or ()):
                    kbn.remove_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
                out.append({"board": slug, "task": tid, "chats": chats, "to": f"{to}/{profile}"})
    return out


def _segments_and_counts(db, session_id: str, home: str) -> tuple[dict, dict[str, int]]:
    payload = db.export_session_lineage(session_id, include_inactive=True)
    if not payload:
        raise MoveError("not_found", f"no session {session_id} here")
    payload = {**payload, "source_home": home}
    segments = payload.get("segments") or [payload]
    return payload, {s["id"]: len(s.get("messages") or []) for s in segments}


def move_session(session_id: str, to: str, reason: str, *, after_turn: bool = False, force: bool = False, db=None,
                 cfg: Optional[dict] = None, wait_s: float = AFTER_TURN_WAIT_S, poll_s: float = POLL_S,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Run the move; returns the report (``moved`` True/False, plus ``code`` and ``error`` when refused)."""
    from hermes_cli import session_owner as so
    reason = (reason or "").strip()
    try:
        if not reason:
            raise MoveError("reason_required", "--reason is required (why this chat must move)")
        hosts = move_hosts(cfg)
        if to not in hosts:
            raise MoveError("unknown_host", f"{to!r} is not in sessions.move.hosts ({', '.join(sorted(hosts)) or 'none'})")
        me = so.self_gateway()
        if not me:
            raise MoveError("no_gateway", "this machine has no hermes-hub gateway name")
        if to == me:
            raise MoveError("same_gateway", f"this chat is already on {me}")
        own = db is None
        db = db or _open_db()
        try:
            return _move(db, session_id, to, reason, me=me, target=hosts[to], after_turn=after_turn, cfg=cfg,
                         wait_s=wait_s, poll_s=poll_s, sleep=sleep, force=force)
        finally:
            if own:
                db.close()
    except MoveError as exc:
        return {"session_id": session_id, "to": to, "moved": False, "code": exc.code, "error": str(exc), **exc.extra}


def remote_open_cards(lineage: list[str], me: str) -> list[dict[str, Any]]:
    """Open cards created on other gateways that this lineage's chats are linked to (from the board replicas)."""
    from hermes_cli import kanban_chat_links as kcl
    try:
        rows = kcl.replica_cards(lineage, me, statuses=OPEN_STATUSES)
    except Exception:  # health: allow BLE001 -- no replica (sync off): nothing known on other gateways
        return []
    return [{"id": r.get("id"), "gateway": r.get("gateway") or r.get("owner_gateway") or "?", "status": r.get("status")}
            for r in rows if r.get("status") in OPEN_STATUSES]


def _move(db, session_id: str, to: str, reason: str, *, me: str, target: str, after_turn: bool, cfg,
          wait_s: float, poll_s: float, sleep, force: bool = False) -> dict[str, Any]:
    from hermes_cli import session_owner as so
    resolved = db.resolve_session_id(session_id) if hasattr(db, "resolve_session_id") else session_id
    if not resolved or not db.get_session(resolved):
        raise MoveError("not_found", f"no session {session_id} here")
    lineage = db.get_compression_lineage(resolved) or [resolved]
    root = lineage[0]
    if so._context() is None:
        raise MoveError("not_synced", "session moves need the default board syncing to the Board service "
                                      "(kanban.sync enabled, mode primary)")
    rec = so.owner(root)
    if rec and rec.get("gateway") != me:
        raise MoveError("not_owner", f"this chat is owned by {rec.get('gateway')}, not {me}", owner=rec)
    remote = remote_open_cards(lineage, me)
    if remote and not force:
        raise MoveError("remote_cards", f"{len(remote)} open card(s) on other gateways notify this chat and cannot be "
                        "re-pointed from here: " + ", ".join(f"{c['gateway']}/{c['id']}" for c in remote[:10])
                        + "; wait for them to finish or pass --force", cards=remote)
    if turn_running(db, root):
        if not after_turn:
            raise MoveError("turn_running", "a turn is running in this chat; retry when it ends (or --after-turn)")
        if not wait_for_turn_end(db, root, wait_s=wait_s, poll_s=poll_s, sleep=sleep):
            raise MoveError("turn_timeout", f"the turn did not end within {int(wait_s)} s; nothing moved")
    home = str(Path.home())
    payload, counts = _segments_and_counts(db, root, home)
    ids = list(counts)
    pushed = run_transport(target, ["sessions", "import-lineage", "-", "--hidden"], payload=json.dumps(payload),
                           cfg=cfg)
    got = pushed.get("counts") if isinstance(pushed.get("counts"), dict) else {}
    if not pushed.get("ok") or any(int(got.get(k, -1)) < v for k, v in counts.items()):
        undo = run_transport(target, ["sessions", "discard-hidden", *ids], cfg=cfg) if pushed.get("ok") else None
        raise MoveError("push_incomplete", f"the copy on {to} is incomplete", push=pushed, segments=counts,
                        discarded=undo)
    grown = [k for k, v in counts.items() if _count(db, k) > v]
    if grown:
        undo = run_transport(target, ["sessions", "discard-hidden", *ids], cfg=cfg)
        raise MoveError("grew_during_move", f"a turn landed here during the move ({grown}); nothing moved, "
                                            "the hidden copy was removed", discarded=undo)
    epoch = int((rec or {}).get("epoch") or 0)
    try:
        cas = so.move(root, to, epoch, reason)
    except so.OwnerUnavailable as exc:
        cas = {"ok": False, "reason": "board_unavailable", "error": str(exc)}
    if not cas.get("ok"):
        undo = run_transport(target, ["sessions", "discard-hidden", *ids], cfg=cfg)
        raise MoveError("cas_failed", f"the Board refused the move ({cas.get('reason')}); the hidden copy on {to} "
                        + ("was removed" if undo.get("ok") else "could NOT be removed: " + str(undo.get("error")
                                                                                              or undo)),
                        cas=cas, discarded=undo)
    revealed = run_transport(target, ["sessions", "reveal", root], cfg=cfg)
    end_reason = END_REASON_PREFIX + to
    for sid in ids:
        db.reopen_session(sid)
        db.end_session(sid, end_reason)
        db.set_session_archived(sid, True)
    links = repoint_links(ids, to, profile=_profile(cfg))
    report = {"session_id": root, "to": to, "moved": True, "owner": cas.get("owner"), "segments": counts,
              "revealed": bool(revealed.get("ok")), "links": links}
    if not revealed.get("ok"):
        report["reveal_error"] = revealed.get("error") or revealed
    return report


# --- CLI ----------------------------------------------------------------------------------------------------------

def _print(obj: Any) -> None:
    print(json.dumps(obj, default=str))


def cmd_import_lineage(args) -> int:
    src = args.file
    text = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8-sig")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        _print({"ok": False, "error": f"not JSON: {exc}"})
        return 1
    if not isinstance(payload, dict):
        _print({"ok": False, "error": "payload must be an export_session_lineage object"})
        return 1
    res = import_lineage(payload, hidden=bool(args.hidden))
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_reveal(args) -> int:
    res = reveal(args.session_id)
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_discard_hidden(args) -> int:
    res = discard_hidden(args.session_ids)
    _print(res)
    return 0 if res.get("ok") else 1


def cmd_move(args) -> int:
    res = move_session(args.session_id, args.to, args.reason or "", after_turn=bool(args.after_turn),
                       force=bool(getattr(args, "force", False)))
    _print(res)
    return 0 if res.get("moved") and res.get("revealed", True) else 1


def spawn_after_turn_move(session_id: str, to: str, reason: str) -> int:
    """Start ``hermes sessions move <id> --to <to> --reason ... --after-turn`` detached (the agent tool's path);
    returns its pid. Output goes to ``<hermes home>/logs/session-move-<id>.log``."""
    from hermes_constants import get_hermes_home
    from tools.environments.local import build_subprocess_env
    log_dir = Path(get_hermes_home()) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"session-move-{session_id}.log", "ab")  # handed to the child
    try:
        from hermes_cli._startup_fast import project_root_str
        repo = project_root_str()
        p = subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "sessions", "move", session_id, "--to", to,
             "--reason", reason, "--after-turn"],
            # detached-spawn: ok — the after-turn move must outlive the turn (and agent) that requested it [daveey]
            cwd=repo, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            env=build_subprocess_env(scrub_secrets=False, inherit_profile_home=False))
    finally:
        log.close()
    return p.pid
