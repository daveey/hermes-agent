"""Session handover: the Board owner record, fencing, and ``hermes sessions move`` end to end. [daveey]

Entry paths, one test each: the owner client against the fake Board (cache, fail-open, CAS outcomes), the fence in
``tui_gateway.prompt_turn._admit_prompt_turn``, ``hermes --resume`` and ``hermes -z --resume``, ``hermes sessions
move`` through the CLI with a local transport into a second HERMES_HOME (copied, revealed, archived here, fenced
here), a refusal mid-turn and the ``--after-turn`` wait, a CAS failure removing the hidden copy, and a card created
by the moved chat whose done notice reaches the new gateway's copy, not the archived one here.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import session_handover as sh
from hermes_cli import session_owner as so
from hermes_state import SessionDB
from tests.hermes_cli.kanban_sync_fake_board import FakeService

SRC, DST, OTHER, BOARD = "macbook", "hub", "linux-2", "default"
MACHINES = {name: {"name": name, "state": "active", "labels": [], "hold": False} for name in (SRC, DST, OTHER)}
REPO = Path(__file__).resolve().parents[2]
SID = "20261028_120000_aaaaaa"


def _write_config(home: Path, dst_home: Path, dst_root: Path) -> None:
    cfg = {
        "kanban": {"sync": {"enabled": True}},
        "sessions": {"move": {
            "hosts": {DST: "ubuntu@hub-host"},
            # the target is a second HERMES_HOME on this machine (a pytest child: the live-DB guard needs the bypass)
            "transport": ["env", f"HERMES_HOME={dst_home}", f"HOME={dst_root}", f"PYTHONPATH={REPO}",
                          "HERMES_STATE_DB_GUARD_BYPASS=1", "{args}"],
            "remote_hermes": [sys.executable, "-m", "hermes_cli.main"],
            "profile": "default",
        }},
    }
    (home / "config.yaml").write_text(json.dumps(cfg))  # JSON is YAML


@pytest.fixture
def gw(tmp_path, monkeypatch):
    """This gateway (``macbook``, primary ``default`` board on a fake Board) and an empty target home (``hub``)."""
    src_root, dst_root = tmp_path / "src", tmp_path / "dst"
    home, dst_home = src_root / ".hermes", dst_root / ".hermes"
    home.mkdir(parents=True)
    dst_home.mkdir(parents=True)
    (src_root / "code" / "proj").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: src_root)
    import hermes_state
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")  # what a bare SessionDB() opens
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps({"gateway": SRC}))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(mail))
    _write_config(home, dst_home, dst_root)
    sync.consume_dispatcher_wake()
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, SRC)
    sync.set_mode(conn, "primary")
    svc = FakeService(machines=MACHINES)
    so.set_hub_factory(lambda: svc.hub(SRC))
    db = SessionDB(home / "state.db")
    db.create_session(SID, "desktop", cwd=str(src_root / "code" / "proj"))
    db.append_message(SID, "user", "please build it")
    db.append_message(SID, "assistant", "on it")
    try:
        yield SimpleNamespace(home=home, dst_home=dst_home, dst_root=dst_root, src_root=src_root, conn=conn, svc=svc,
                              db=db)
    finally:
        so.set_hub_factory(None)
        db.close()
        conn.close()


def _dst_db(gw) -> SessionDB:
    return SessionDB(gw.dst_home / "state.db")


def _owner_on_board(gw, sid=SID):
    return gw.svc.session_owners.get(BOARD, {}).get(sid)


# --- the owner client against the fake Board --------------------------------------------------------------------

def test_owner_client_cas_outcomes_and_30s_cache(gw, monkeypatch):
    assert so.owner(SID) is None  # no record
    calls = len(gw.svc.calls)
    assert so.owner(SID) is None and len(gw.svc.calls) == calls  # cached

    assert so.move(SID, DST, 1, "x") == {"ok": False, "reason": "epoch_mismatch", "owner": None}
    first = so.move(SID, DST, 0, "needs the hub")
    assert first["ok"] and first["owner"]["gateway"] == DST and first["owner"]["epoch"] == 1
    assert so.move(SID, OTHER, 1, "again")["reason"] == "not_owner"  # macbook no longer owns it
    assert so.move("other-sid", "ghost", 0, "x")["reason"] == "unknown_target"
    assert so.move("other-sid", SRC, 0, "x")["reason"] == "same_gateway"
    assert gw.svc.session_audit == [{"session_id": SID, "actor": SRC, "from_gateway": None, "to_gateway": DST,
                                     "epoch": 1, "reason": "needs the hub"}]

    # a stale cache entry is refetched after CACHE_TTL_S
    clock = [1000.0]
    monkeypatch.setattr(so.time, "monotonic", lambda: clock[0])
    so.reset_cache()
    assert so.owner(SID)["gateway"] == DST
    gw.svc.session_owners[BOARD][SID]["gateway"] = OTHER  # changed on the Board behind our back
    clock[0] += so.CACHE_TTL_S - 1
    assert so.owner(SID)["gateway"] == DST
    clock[0] += 2
    assert so.owner(SID)["gateway"] == OTHER


def test_owner_client_unreachable_board_uses_last_answer_else_fails_open(gw, monkeypatch, caplog):
    gw.svc.session_owners[BOARD] = {SID: {"gateway": DST, "epoch": 1, "moved_at": 1, "moved_by": SRC}}
    clock = [1000.0]
    monkeypatch.setattr(so.time, "monotonic", lambda: clock[0])
    assert so.owner(SID)["gateway"] == DST
    gw.svc.offline = True
    clock[0] += 3600  # far past the TTL: still the last answer, never "no record"
    assert so.owner(SID)["gateway"] == DST
    assert so.refusal(SID) is not None
    with caplog.at_level(logging.WARNING, logger="hermes_cli.session_owner"):
        assert so.owner("never-asked") is None
    assert "fail-open" in caplog.text
    with pytest.raises(so.OwnerUnavailable):
        so.move(SID, OTHER, 1, "x")


def test_no_record_and_no_fencing_when_sync_is_off(gw):
    gw.svc.session_owners[BOARD] = {SID: {"gateway": DST, "epoch": 1, "moved_at": 1, "moved_by": SRC}}
    (gw.home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: false\n")
    so.reset_cache()
    assert so.owner(SID) is None and so.refusal(SID) is None
    with pytest.raises(so.OwnerUnavailable):
        so.move(SID, DST, 0, "x")


def test_ksc_hub_sessions_returns_a_409_refusal_body(monkeypatch):
    hub = ksc.Hub(ksc.Identity(url="https://hub.test", key="k", gateway=SRC))

    def refuse(method, path, body=None):
        assert path == "/v2/boards/default/sessions" and body["action"] == "move"
        raise ksc.HubError(409, json.dumps({"reason": "not_owner", "owner": {"gateway": DST, "epoch": 2}}))

    monkeypatch.setattr(hub, "_call", refuse)
    assert hub.sessions("default", {"action": "move"}) == {"reason": "not_owner", "owner": {"gateway": DST, "epoch": 2}}

    def boom(method, path, body=None):
        raise ksc.HubError(500, "internal error")

    monkeypatch.setattr(hub, "_call", boom)
    with pytest.raises(ksc.HubError):
        hub.sessions("default", {"action": "get", "session_ids": []})


# --- fencing ------------------------------------------------------------------------------------------------------

def _moved_away(gw):
    gw.svc.session_owners[BOARD] = {SID: {"gateway": DST, "epoch": 1, "moved_at": 1, "moved_by": SRC}}
    so.reset_cache()


def test_admit_prompt_turn_refuses_a_moved_session(gw, monkeypatch):
    from tui_gateway import server
    frames = []
    monkeypatch.setattr(server, "_emit_terminal_turn_error",
                        lambda sid, session, error, error_surface=None, **k: frames.append((str(error), error_surface)))
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(gw.db))
    claimed = []
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session, **k: claimed.append(sid) or None)
    session = {"agent": None, "session_key": SID, "history": [], "history_lock": threading.Lock(),
               "history_version": 0, "running": True, "attached_images": []}
    monkeypatch.setattr("tui_gateway.turn_admission.owns", lambda session: True)

    _moved_away(gw)
    assert server._admit_prompt_turn("live-sid", session, "hello", None, None, None, None) is None
    assert frames == [(f"This chat moved to {DST}; open it there.",
                       {"layer": "gateway", "code": "SESSION_MOVED", "retryable": False})]
    assert session["_turn_owner_refusal"] == "SESSION_MOVED" and session["running"] is False
    assert claimed == []  # refused before the slot claim

    # owned here again (no record): the gate passes on to the slot claim
    gw.svc.session_owners[BOARD] = {}
    so.reset_cache()
    session["running"] = True
    server._admit_prompt_turn("live-sid", session, "hello", None, None, None, None)
    assert claimed == ["live-sid"]


def _resume_args(**kw):
    return SimpleNamespace(resume=SID, continue_last=None, no_restore_cwd=True, worktree=False, in_dir=None, **kw)


def test_cli_resume_of_a_moved_session_exits_with_session_moved(gw, monkeypatch, capsys):
    from hermes_cli import main as hm
    monkeypatch.setattr(hm, "_apply_in_dir", lambda args: None)
    # Fork: upstream moved the foreign-resume import out of main into main_resume_args.
    monkeypatch.setattr("hermes_cli.main_resume_args.import_foreign_resume", lambda args: None)
    monkeypatch.setattr(hm, "_resolve_session_by_name_or_id", lambda value: value)
    hm._resolve_chat_session_args(_resume_args(), use_tui=False)  # not moved: resumes
    _moved_away(gw)
    with pytest.raises(SystemExit) as exc:
        hm._resolve_chat_session_args(_resume_args(), use_tui=False)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "hermes-refusal-reason: SESSION_MOVED" in err and f"moved to {DST}" in err


def test_oneshot_resume_of_a_moved_session_exits_before_the_turn(gw, monkeypatch, capsys):
    from hermes_cli import main as hm
    monkeypatch.setattr(hm, "_apply_in_dir", lambda args: None)
    # Fork: upstream moved the foreign-resume import out of main into main_resume_args.
    monkeypatch.setattr("hermes_cli.main_resume_args.import_foreign_resume", lambda args: None)
    monkeypatch.setattr(hm, "_resolve_session_by_name_or_id", lambda value: value)
    monkeypatch.setattr(hm, "_confirm_startup_expensive_model_override", lambda args: None)
    ran = []
    # Upstream (936f3465f82, EP-11a) deleted the in-process ``_run_and_exit_oneshot`` runner: -z only runs
    # through the gateway chat launcher, so that is the one turn path the moved fence must stop.
    monkeypatch.setattr("hermes_cli.gateway_chat_startup.launch_gateway_chat", lambda args: ran.append(args) or 0)
    _moved_away(gw)
    with pytest.raises(SystemExit) as exc:
        hm._run_oneshot_from_args(_resume_args(oneshot="hi"))
    assert exc.value.code == 1 and ran == []
    assert "SESSION_MOVED" in capsys.readouterr().err


# --- hermes sessions move, end to end ----------------------------------------------------------------------------

def _cli(*argv) -> tuple[int, dict]:
    """``hermes sessions <argv>`` through the real sessions parser and dispatcher."""
    import argparse
    import io
    from hermes_cli.sessions_cmd import cmd_sessions
    from hermes_cli.subcommands.sessions import build_sessions_parser
    parser = argparse.ArgumentParser(prog="hermes")
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions)
    args = parser.parse_args(["sessions", *argv])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cmd_sessions(args)
    return rc, json.loads(out.getvalue().strip().splitlines()[-1])


def _card(gw, title="build it"):
    tid = kb.create_task(gw.conn, title=title, assignee="default", session_id=SID)
    kbn.add_notify_sub(gw.conn, task_id=tid, platform="tui", chat_id=SID)
    return tid


def test_sessions_move_end_to_end_copies_reveals_archives_fences_and_repoints(gw):
    tid = _card(gw)
    rc, res = _cli("move", SID, "--to", DST, "--reason", "needs the hub's GPU")
    assert rc == 0 and res["moved"] and res["revealed"], res
    assert res["segments"] == {SID: 2}
    assert res["owner"]["gateway"] == DST and res["owner"]["epoch"] == 1
    assert _owner_on_board(gw)["gateway"] == DST

    with contextlib.closing(_dst_db(gw)) as dst:  # copied and revealed there, cwd remapped
        row = dst.get_session(SID)
        assert row and not row["hidden"] and not row["archived"]
        assert row["cwd"] == str(gw.dst_root / "code" / "proj")
        assert [m["content"] for m in dst.get_messages(SID)] == ["please build it", "on it"]
    row = gw.db.get_session(SID)  # archived here
    assert row["archived"] and row["end_reason"] == f"moved_to_gateway:{DST}"
    assert str(so.refusal(SID)) == f"This chat moved to {DST}; open it there."  # fenced here
    so.reset_cache()
    assert so.refusal(SID).reason == "SESSION_MOVED"

    assert res["links"] == [{"board": BOARD, "task": tid, "chats": [SID], "to": f"{DST}/default"}]
    subs = {(s["platform"], s["chat_id"]) for s in kbn.list_notify_subs(gw.conn, tid)}
    assert subs == {("hub", f"{DST}/default/{SID}")}  # no tui sub: the archived copy here is never notified


def _published(conn, tid: str, owner_gw: str) -> dict:
    """The card as this gateway's hermes-hub client publishes it (client/hermes_hub.py: a ``platform='hub'``
    subscription ``<gw>/<profile>/<session>`` becomes a ``rel: shepherd`` session link, tui subs ``subscribed``)."""
    from hermes_cli.kanban_origin_ledger import observe_owner_task
    t = kb.get_task(conn, tid)
    sessions = [{"session": t.session_id, "profile": "default", "rel": "created"}] if t.session_id else []
    for platform, chat in conn.execute("SELECT platform, chat_id FROM kanban_notify_subs WHERE task_id = ?", (tid,)):
        if platform == "tui":
            sessions.append({"session": chat, "profile": None, "rel": "subscribed"})
        elif platform == "hub":
            g, p, s = chat.split("/")
            sessions.append({"session": s, "profile": p, "gw": g, "rel": "shepherd"})
    task = {"id": tid, "status": t.status, "title": t.title, "board": BOARD, "gateway": owner_gw,
            "profile": "default", "assignee": t.assignee, "origin_bindings": [], "sessions": sessions}
    conn.execute("BEGIN")
    try:
        return observe_owner_task(conn, task, now=0)
    finally:
        conn.rollback()


def test_a_card_done_after_the_move_notifies_the_copy_on_the_new_gateway(gw, tmp_path):
    from tui_gateway.kanban_remote_notices import sweep_remote_notices
    tid = _card(gw, "run the bench")
    rc, res = _cli("move", SID, "--to", DST, "--reason", "bench box")
    assert rc == 0 and res["moved"], res
    kb.complete_task(gw.conn, tid, summary="bench: 42 it/s")

    # the local tui notifier has nothing to deliver to the archived copy here
    assert kbn.list_notify_subs(gw.conn, tid, platform="tui") == []
    replica = tmp_path / "hub-replica.db"
    with contextlib.closing(sqlite3.connect(replica)) as rc_conn:
        rc_conn.execute("CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT)")
        rc_conn.execute("INSERT INTO replica VALUES (?,?,?,?)",
                        (SRC, "tasks", f"default/default/{tid}", json.dumps(_published(gw.conn, tid, SRC))))
        rc_conn.commit()
    with contextlib.closing(_dst_db(gw)) as dst:
        @contextlib.contextmanager
        def opener(profile, sid):
            assert profile == "default"
            yield dst, dst.get_compression_tip(sid) or sid

        out = sweep_remote_notices(replica_path=replica, gateway=DST, session_db=opener,
                                   ledger=tmp_path / "dst-ledger.db")
        assert len(out) == 1
        notices = [m for m in dst.get_messages(SID) if m.get("display_kind") == "kanban_notice"]
        assert len(notices) == 1 and tid in notices[0]["content"] and "42 it/s" in notices[0]["content"]
    # this gateway never announces its own card to a remote chat of its own, nor to the archived copy
    assert sweep_remote_notices(replica_path=replica, gateway=SRC, session_db=None) == []
    assert not [m for m in gw.db.get_messages(SID) if m.get("display_kind") == "kanban_notice"]


def test_move_refused_mid_turn_and_after_turn_waits_for_the_turn_to_end(gw):
    holder = f"pid={__import__('os').getpid()}:test-turn"
    assert gw.db.try_acquire_session_turn_lease(SID, holder)
    res = sh.move_session(SID, DST, "x", db=gw.db)
    assert res["moved"] is False and res["code"] == "turn_running"
    assert _owner_on_board(gw) is None

    res = sh.move_session(SID, DST, "x", after_turn=True, db=gw.db, wait_s=0.0, poll_s=0.0)
    assert res["code"] == "turn_timeout" and _owner_on_board(gw) is None

    slept = []

    def sleep(s):
        slept.append(s)
        gw.db.release_session_turn_lease(SID, holder)  # the turn ends

    res = sh.move_session(SID, DST, "after this turn", after_turn=True, db=gw.db, poll_s=0.01, sleep=sleep)
    assert res["moved"] and slept == [0.01], res
    assert _owner_on_board(gw)["gateway"] == DST


def test_a_cas_failure_removes_the_hidden_copy(gw, monkeypatch):
    gw.svc.session_owners[BOARD] = {SID: {"gateway": SRC, "epoch": 3, "moved_at": 1, "moved_by": OTHER}}
    so.reset_cache()
    real_move = so.move

    def racing_move(sid, to, expect, reason):  # another move won between our read and the CAS
        gw.svc.session_owners[BOARD][SID]["epoch"] = 4
        return real_move(sid, to, expect, reason)

    monkeypatch.setattr(so, "move", racing_move)
    res = sh.move_session(SID, DST, "x", db=gw.db)
    assert res["moved"] is False and res["code"] == "cas_failed" and res["cas"]["reason"] == "epoch_mismatch", res
    assert res["discarded"]["ok"] and res["discarded"]["deleted"] == [SID]
    with contextlib.closing(_dst_db(gw)) as dst:
        assert dst.get_session(SID) is None
    row = gw.db.get_session(SID)
    assert not row["archived"] and row["end_reason"] is None  # still live here


def test_move_needs_a_configured_host_and_a_reason(gw):
    assert sh.move_session(SID, OTHER, "x", db=gw.db)["code"] == "unknown_host"
    assert sh.move_session(SID, DST, " ", db=gw.db)["code"] == "reason_required"
    assert sh.move_session(SID, SRC, "x", db=gw.db, cfg={"sessions": {"move": {"hosts": {SRC: "me"}}}})["code"] \
        == "same_gateway"
    gw.svc.session_owners[BOARD] = {SID: {"gateway": OTHER, "epoch": 1, "moved_at": 1, "moved_by": SRC}}
    so.reset_cache()
    assert sh.move_session(SID, DST, "x", db=gw.db)["code"] == "not_owner"


def test_import_lineage_replaces_a_copy_this_gateway_handed_away(tmp_path):
    """A chat moved away and back: the archived ``moved_to_gateway:`` copy here is replaced by the newer one."""
    with contextlib.closing(SessionDB(tmp_path / "a.db")) as a, contextlib.closing(SessionDB(tmp_path / "b.db")) as b:
        a.create_session(SID, "desktop", cwd="/home/u/code/x")
        a.append_message(SID, "user", "one")
        b.import_sessions([a.export_session_lineage(SID, include_inactive=True)["segments"][0]])
        b.end_session(SID, "moved_to_gateway:hub")
        b.set_session_archived(SID, True)
        a.append_message(SID, "assistant", "two")
        payload = {**a.export_session_lineage(SID, include_inactive=True), "source_home": "/home/u"}
        res = sh.import_lineage(payload, hidden=True, db=b, home="/Users/u")
        assert res["ok"] and res["replaced"] == [SID] and res["counts"] == {SID: 2}, res
        row = b.get_session(SID)
        assert row["hidden"] and not row["archived"] and row["cwd"] == "/Users/u/code/x"
        assert sh.discard_hidden([SID], db=b)["deleted"] == [SID]
        b.import_sessions([dict(payload["segments"][0])])
        assert sh.discard_hidden([SID], db=b) == {"ok": False, "deleted": [], "kept_visible": [SID]}


def test_transport_argv_templates():
    cfg = {"sessions": {"move": {}}}
    assert sh.transport_argv("u@h", ["sessions", "reveal", "s1"], cfg) == [
        "ssh", "-o", "BatchMode=yes", "u@h", "hermes sessions reveal s1"]
    cfg = {"sessions": {"move": {"transport": ["env", "X=1", "{args}"], "remote_hermes": "py -m hermes_cli.main"}}}
    assert sh.transport_argv("u@h", ["sessions", "reveal", "s 1"], cfg) == [
        "env", "X=1", "py", "-m", "hermes_cli.main", "sessions", "reveal", "s 1"]


def test_open_cards_on_other_gateways_refuse_the_move_unless_forced(gw, monkeypatch):
    """Cards another gateway owns cannot be re-pointed from here: their notices would reach the archived copy."""
    from hermes_cli import kanban_chat_links as kcl
    rows = [{"id": "t_remote", "gateway": OTHER, "status": "running"}, {"id": "t_old", "gateway": OTHER, "status": "done"}]
    monkeypatch.setattr(kcl, "replica_cards", lambda ids, me, statuses=None, **kw: rows)
    res = sh.move_session(SID, DST, "needs the hub", db=gw.db)
    assert not res["moved"] and res["code"] == "remote_cards" and f"{OTHER}/t_remote" in res["error"], res
    assert res["cards"] == [{"id": "t_remote", "gateway": OTHER, "status": "running"}]  # a done card is no obligation
    assert not gw.db.get_session(SID)["archived"]
    res = sh.move_session(SID, DST, "needs the hub", db=gw.db, force=True)
    assert res["moved"], res
