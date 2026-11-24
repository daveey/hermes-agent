"""The chat that filed a card on another gateway is the one subscribed to it, and it hears the card finish.

Hub evidence 2026-10-28 (card t_01a182faed899158): placement-created hub cards bound to chat 5ab65c had no
subscription for it (``create_with_origin`` stored only the binding), and no code turned a hub-side link into a
done notice on the origin gateway. One test per entry path: the kanban_create tool with auto placement (origin
side, then owner side), the deferred-placement forward, the CLI ``--origin-session``, plus the origin-side
notice delivery.
"""
import argparse
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_state import SessionDB
from tools import kanban_tools
from tools.registry import registry

CHAT = "20261027_134353_5ab65c"
STALE = "20261026_170127_f07c32"


def _subs(task_id, platform=None):
    with kbc.connect_closing() as conn:
        return sorted((s["platform"], s["chat_id"]) for s in kn.list_notify_subs(conn, task_id)
                      if platform is None or s["platform"] == platform)


# --- owner side: the courier (origin intent) path the hub runs for every placed card --------------------------

@pytest.fixture
def owner(tmp_path, monkeypatch):
    return _owner(tmp_path, monkeypatch)


@pytest.fixture
def synced_owner(tmp_path, monkeypatch):
    """The owner with its Board sync on (``task_chat_links`` exists only on a synced board)."""
    (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    return _owner(tmp_path, monkeypatch)


def _owner(tmp_path, monkeypatch):
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_ORIGIN_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    # A process-global session left over from some other chat: must never be subscribed.
    monkeypatch.setenv("HERMES_SESSION_KEY", STALE)
    monkeypatch.setenv("HERMES_SESSION_ID", STALE)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("kanban-placement-macbook", "cli")
    db.close()
    kb.init_db()
    return {"request_id": "placement:macbook:0123456789abcdef",
            "owner": {"gateway": "hub", "profile": "default", "board": "default"},
            "origin": {"gateway": "macbook", "profile": "default", "session": CHAT}}


def test_courier_create_subscribes_the_bound_origin_not_the_ambient_session(synced_owner):
    owner = synced_owner
    args = {"title": "Build", "assignee": "default", "session_id": "kanban-placement-macbook"}
    res = json.loads(registry.dispatch("kanban_create", args, origin_intent=owner))
    assert res.get("created") is True, res
    tid = res["task_id"]
    assert _subs(tid) == [("hub", f"macbook/default/{CHAT}")]
    again = json.loads(registry.dispatch("kanban_create", args, origin_intent=owner))
    assert again["task_id"] == tid and _subs(tid) == [("hub", f"macbook/default/{CHAT}")]


def test_courier_adoption_subscribes_the_bound_origin(owner):
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Existing", assignee="default")
        intent = {"request_id": "adopt:0123456789", "origin": owner["origin"],
                  "owner": {**owner["owner"], "task_id": tid}}
        assert adopt_with_origin(conn, intent, board="default") == tid
    assert ("hub", f"macbook/default/{CHAT}") in _subs(tid)


def test_courier_adoption_writes_the_origin_chats_created_link(synced_owner):
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Existing", assignee="default")
        intent = {"request_id": "adopt:0123456789", "origin": synced_owner["origin"],
                  "owner": {**synced_owner["owner"], "task_id": tid}}
        assert adopt_with_origin(conn, intent, board="default") == tid
    assert _links(tid) == [("macbook", "default", CHAT, "created", 0)]


def _links(task_id):
    with kbc.connect_closing() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT chat_gateway, chat_profile, chat_session, kind, released FROM task_chat_links WHERE task_id = ?",
            (task_id,))]


def test_courier_create_writes_the_origin_chats_created_link_once(synced_owner):
    """[daveey] 2026-11-13 01:09 PT: placed cards had no ``task_chat_links`` row for the chat that placed them, so
    the link waited on the origin's own Board sync (given up). The owner writes it with the subscription."""
    from hermes_cli.kanban_chat_links import link_uid
    args = {"title": "Build", "assignee": "default", "session_id": "kanban-placement-macbook"}
    tid = json.loads(str(registry.dispatch("kanban_create", args, origin_intent=synced_owner)))["task_id"]
    assert _links(tid) == [("macbook", "default", CHAT, "created", 0)]
    with kbc.connect_closing() as conn:  # the uid the origin's ledger reconcile writes: one Board row, not two
        assert conn.execute("SELECT uid FROM task_chat_links WHERE task_id = ?", (tid,)).fetchone()[0] == link_uid(
            tid, "macbook", "default", CHAT, "created")
    registry.dispatch("kanban_create", args, origin_intent=synced_owner)  # an exact retry adds nothing
    assert _links(tid) == [("macbook", "default", CHAT, "created", 0)]


@pytest.mark.parametrize("origin", [
    {"gateway": "hub", "profile": "default", "session": CHAT},  # a chat on the owner: its own reconcile links it
    # [daveey] PR #2079 review: another profile's chat on the owner's gateway gets none either. The owner's reconcile
    # is that chat's gateway and releases any created row of it it does not derive (a courier card has no
    # creator_chats_resolved event), measured: written, then {'released': 1} on the next pass. The hub sub reaches it.
    {"gateway": "hub", "profile": "other", "session": CHAT},
    {"gateway": "macbook", "profile": "default", "session": "kanban-placement-macbook"},  # attribution, no chat
    # a receipt-shaped hash: maybe a worker's own $HERMES_SESSION_ID; only macbook can prove it is a Desktop chat
    {"gateway": "macbook", "profile": "default", "session": "local-" + "9c" * 32},
])
def test_courier_create_writes_no_link_for_a_chat_on_the_owner_or_a_placeholder(synced_owner, origin):
    args = {"title": "Build", "assignee": "default"}
    res = json.loads(str(registry.dispatch("kanban_create", args, origin_intent={**synced_owner, "origin": origin})))
    if origin['session'].startswith(('kanban-placement-', 'local-')):
        assert not res.get('created') and 'no renewable scope authority' in str(res), res
        with kbc.connect_closing() as conn:
            assert not conn.execute('SELECT 1 FROM tasks').fetchone()
        return
    assert res.get("created") is True, res
    assert _links(res["task_id"]) == []
    assert "origin_chat_link" not in res  # the origin is told the link still waits for its own reconcile


def test_courier_create_tells_the_origin_it_wrote_the_link(synced_owner):
    """[daveey] PR #2079 review: the origin's ``chat_link: owner_written`` note read the chat id's shape; the owner's
    answer now carries what it did, so a mixed fleet (older owner, sync off) never gets that promise wrongly."""
    args = {"title": "Build", "assignee": "default"}
    res = json.loads(str(registry.dispatch("kanban_create", args, origin_intent=synced_owner)))
    assert res.get("created") is True and res.get("origin_chat_link") == "owner_written", res
    again = json.loads(str(registry.dispatch("kanban_create", args, origin_intent=synced_owner)))
    assert again["task_id"] == res["task_id"] and again.get("origin_chat_link") == "owner_written"


def test_courier_create_on_an_owner_whose_sync_is_stopped_does_not_claim_the_link(synced_owner, monkeypatch):
    from hermes_cli import kanban_sync_runstate as runstate
    monkeypatch.setattr(runstate, "down", lambda: {"running": False, "reason": "gave up the sync role"})
    res = json.loads(str(registry.dispatch("kanban_create", {"title": "Build", "assignee": "default"},
                                           origin_intent=synced_owner)))
    assert res.get("created") is True and "origin_chat_link" not in res, res


def test_courier_create_on_an_owner_without_board_sync_does_not_claim_the_link(owner):
    res = json.loads(str(registry.dispatch("kanban_create", {"title": "Build", "assignee": "default"},
                                           origin_intent=owner)))
    assert not res.get('created') and 'no renewable scope authority' in str(res), res
    with kbc.connect_closing() as conn:
        assert not conn.execute('SELECT 1 FROM tasks').fetchone()


def test_courier_adoption_tells_the_origin_it_wrote_the_link(synced_owner):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Existing", assignee="default")
    intent = {"request_id": "adopt:0123456789", "origin": synced_owner["origin"],
              "owner": {**synced_owner["owner"], "task_id": tid}}
    res = json.loads(str(registry.dispatch("kanban_origin_adopt", {"task_id": tid, "board": "default"},
                                           origin_intent=intent)))
    assert res.get("adopted") is True and res.get("origin_chat_link") == "owner_written", res


# --- origin side: kanban_create auto placement and the deferred forward ----------------------------------------

@pytest.fixture
def mac(tmp_path, monkeypatch):
    from tests.hermes_cli.test_kanban_placement import HUB_SCHEMA, ROUTE
    from hermes_cli import kanban_placement as kp
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in ("HERMES_KANBAN_TASK", "HERMES_ORIGIN_SESSION", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", STALE)
    monkeypatch.setenv("HERMES_SESSION_KEY", STALE)
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps({"gateway": "macbook"}))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(mail))
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    hub = sqlite3.connect(state / "hub.db")
    hub.executescript(HUB_SCHEMA)
    hub.execute("INSERT INTO meta VALUES ('gateway', 'macbook')")
    hub.execute("INSERT INTO meta VALUES ('last_ok', ?)", (str(time.time()),))
    rec = {"schema": 1, "gateway": "hub", "refreshed_at": time.time(), "capabilities": []}
    hub.execute("INSERT INTO replica VALUES ('hub', 'capabilities', 'info', 1, 1, ?, ?)",
                (json.dumps(rec), int(time.time())))
    hub.commit()
    sdb = SessionDB(db_path=home / "state.db")
    for sid in (CHAT, STALE):
        sdb.create_session(sid, source="desktop")
    sdb.close()
    calls = []

    def fake_hub(argv):
        if "--help" in argv:
            return 0, "usage: hermes-hub create-task [--id ID]", ""
        calls.append(argv)
        rid = argv[argv.index("--id") + 1]
        hub.execute("INSERT OR IGNORE INTO sent (id, body, state, created_at) VALUES (?, ?, 'sent', ?)",
                    (rid, json.dumps({"id": rid}), time.time()))
        hub.commit()
        return 0, rid, ""

    monkeypatch.setattr(kp, "hub_runner", fake_hub)
    monkeypatch.setattr(kp, "hub_spawn", lambda argv: None, raising=False)
    monkeypatch.setattr(kp, "_help_cache", {})
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 0})
    kb.init_db()
    yield {"calls": calls}
    hub.close()


def _as_chat(fn):
    """Run ``fn`` as chat CHAT's turn: the request-scoped ContextVars name it, os.environ names another chat.
    Runs in a copied context so the binding never leaks into later tests."""
    import contextvars
    from gateway.session_context import set_session_vars

    def run():
        set_session_vars(session_key=CHAT, session_id=CHAT)
        return fn()

    return contextvars.copy_context().run(run)


def test_auto_placement_sends_the_filing_chat_as_origin(mac):
    from tools import kanban_tools as kt
    out = json.loads(_as_chat(lambda: kt._handle_create({"title": "Fix the flaky lint", "body": "Push it."})))
    assert out["ok"] and out["placed"]["gateway"] == "hub", out
    argv = mac["calls"][0]
    assert argv[argv.index("--origin-session") + 1] == CHAT


def test_deferred_forward_sends_the_filing_chat_as_origin(mac, monkeypatch):
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrate", assignee="default")
    out = json.loads(_as_chat(lambda: kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]})))
    assert out["ok"] and out["placement"]["defer"] == "hub", out
    tid = out["task_id"]
    # The local placeholder notifies its chat (the bound ContextVar key), never the process-global one.
    assert _subs(tid, "tui") == [("tui", CHAT)]
    with kbc.connect_closing() as conn:
        kb.complete_task(conn, parent, summary="done", force=True)
        assert kpf.state(conn, tid)["origin_session"] == CHAT
        claimed = kb.claim_task(conn, tid)
        f = kpf.Forwarder(conn, tid, claimed.current_run_id)
        f.live = lambda t: None
        f.await_id = lambda rid, **kw: (None, None)
        f.run()  # forwarder runs in a worker process: the env names no chat of its own
    argv = mac["calls"][-1]
    assert argv[argv.index("--origin-session") + 1] == CHAT


def test_local_create_never_subscribes_a_stale_process_session(mac, monkeypatch):
    from tools import kanban_tools as kt
    out = json.loads(_as_chat(lambda: kt._handle_create({"title": "Read the Chrome tab", "body": "local"})))
    assert out["ok"] and "placed" not in out, out
    assert _subs(out["task_id"]) == [("tui", CHAT)]


def test_tui_key_ignores_a_process_key_naming_another_chat(monkeypatch):
    import contextvars
    from gateway.session_context import scoped_current_session_id
    from tools import kanban_tools as kt
    monkeypatch.setenv("HERMES_SESSION_KEY", STALE)

    def key_as(sid):
        with scoped_current_session_id(sid):
            return kt._tui_session_key()

    assert contextvars.Context().run(key_as, CHAT) == ""
    assert contextvars.Context().run(key_as, STALE) == STALE  # same chat: an inherited key is fine
    assert contextvars.Context().run(kt._tui_session_key) == STALE  # nothing bound: CLI subprocess


# --- CLI --origin-session ---------------------------------------------------------------------------------------

def test_cli_origin_session_flag_subscribes_that_chat(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban as kc
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    for name in ("HERMES_KANBAN_TASK", "HERMES_ORIGIN_SESSION", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", STALE)
    (tmp_path / 'config.yaml').write_text('kanban:\n  sync:\n    enabled: true\n')
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    conn = sqlite3.connect(state / "hub.db")
    conn.executescript("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
                       "CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,"
                       " updated_at INTEGER, PRIMARY KEY (gw, coll, key));")
    conn.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    conn.execute("INSERT INTO replica VALUES ('macbook', 'gateway', 'info', 1, 1, '{}', 0)")
    conn.commit()
    conn.close()
    kb.init_db()
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "via cli", "--json", "--origin-session", f"macbook:{CHAT}"])
    assert kc._cmd_create(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert _subs(out["id"], "hub") == [("hub", f"macbook/default/{CHAT}")]


# --- origin side delivery: a linked hub card's done reaches the chat --------------------------------------------

def _replica(tmp_path, cards):
    path = tmp_path / "hub.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);"
                       "CREATE TABLE IF NOT EXISTS replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, "
                       "seq INTEGER, data TEXT, updated_at INTEGER, PRIMARY KEY (gw, coll, key));")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('gateway', 'macbook')")
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM replica").fetchone()[0]
    for gw, key, task in cards:
        seq += 1
        conn.execute("INSERT OR REPLACE INTO replica VALUES (?, 'tasks', ?, 1, ?, ?, 0)",
                     (gw, key, seq, json.dumps(task)))
    conn.commit()
    conn.close()
    return path


def _card(tid, status, session=CHAT, *, at=None, rel="shepherd", gw="macbook"):
    at = time.time() - 30 if at is None else at
    return {"id": tid, "title": f"card {tid}", "status": status, "assignee": "default", "completed_at": int(at),
            "run": {"summary": f"{tid} handoff", "ended_at": int(at)},
            "sessions": [{"gw": gw, "profile": "default", "rel": rel, "session": session}]}


def test_remote_done_notice_is_delivered_once_and_released_on_refusal(tmp_path):
    from tui_gateway import kanban_remote_notices as krn
    krn._reset_for_tests()
    hub = _replica(tmp_path, [
        ("hub", "default/default/t_done", _card("t_done", "done")),
        ("hub", "default/default/t_run", _card("t_run", "running")),
        ("hub", "default/default/t_other", _card("t_other", "done", session=STALE)),
        ("hub", "default/default/t_old", _card("t_old", "done", at=time.time() - 3 * krn.FRESH_S)),
        ("macbook", "default/default/t_mine", _card("t_mine", "done")),  # local: tui subs cover it
    ])
    ledger = tmp_path / "ledger.db"
    got = krn.collect(CHAT, "default", hub_db=hub, ledger=ledger)
    assert [g["ref"] for g in got] == ["hub/default/default/t_done"]
    assert got[0]["text"].startswith("✔ [hub:default] @default Kanban t_done done — card t_done\nt_done handoff")
    assert krn.collect(CHAT, "default", hub_db=hub, ledger=ledger) == []  # claimed once
    assert krn.release(got[0], ledger=ledger)
    assert [g["ref"] for g in krn.collect(CHAT, "default", hub_db=hub, ledger=ledger)] == ["hub/default/default/t_done"]
    # a blocked state is its own notice; a link kept on a compaction parent still reaches the tip
    _replica(tmp_path, [("hub", "default/default/t_run", _card("t_run", "blocked", session="parent_sid"))])
    got = krn.collect(CHAT, "default", hub_db=hub, ledger=ledger, lineage=("parent_sid",))
    assert [g["ref"] for g in got] == ["hub/default/default/t_run"] and "blocked" in got[0]["text"]
    assert krn.collect("someone_else", "default", hub_db=hub, ledger=ledger) == []


def test_remote_notice_skips_a_card_a_local_forward_placeholder_reports(tmp_path):
    from tui_gateway import kanban_remote_notices as krn
    krn._reset_for_tests()
    hub = _replica(tmp_path, [("hub", "default/default/t_fwd", _card("t_fwd", "done")),
                              ("hub", "default/default/t_own", _card("t_own", "done"))])
    ledger = tmp_path / "ledger.db"
    asked = []
    got = krn.collect(CHAT, "default", hub_db=hub, ledger=ledger, skip=lambda t: asked.append(t) or t == "t_fwd")
    assert [g["ref"] for g in got] == ["hub/default/default/t_own"]
    assert krn.collect(CHAT, "default", hub_db=hub, ledger=ledger, skip=lambda t: asked.append(t) or True) == []
    assert sorted(asked) == ["t_fwd", "t_own"]  # asked once per new state, never on later polls


def test_session_poller_delivers_remote_notice_through_collect(tmp_path, monkeypatch):
    import threading
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway import server as srv
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    krn._reset_for_tests()
    kb.init_db()
    _replica(tmp_path, [("hub", "default/default/t_done", _card("t_done", "done")),
                        ("hub", "default/default/t_fwd", _card("t_fwd", "done"))])
    from hermes_cli.kanban_placement_forward import FORWARDED
    with kbc.connect_closing() as conn:
        local = kb.create_task(conn, title="placeholder", assignee="default")
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                     (local, FORWARDED, json.dumps({"request_id": "r", "remote_task_id": "t_fwd"}), int(time.time())))
        conn.commit()
    claims: list = []
    texts = srv._collect_kanban_notifications({"session_key": CHAT}, claims)
    assert [str(t) for t in texts if "t_done" in t], texts
    assert not [t for t in texts if "t_fwd" in t]  # the placeholder's own tui sub reports it
    assert any(c.get("remote") for c in claims)
    session = {"_kanban_claims": claims, "history_lock": threading.Lock()}
    srv._kb_rewind_refused(session, texts)  # a refused turn hands the notice back
    again = srv._collect_kanban_notifications({"session_key": CHAT}, [])
    assert [str(t) for t in again if "t_done" in t]


def test_owner_answer_means_enabled_and_not_recorded_stopped(synced_owner):
    """[daveey] PR #2157 review 5890952298: ``origin_link_fields`` reads "sync runs" as enabled and not recorded
    stopped. No positive "running" marker exists (a completed round deletes the record), so an enabled sync with no
    record still answers ``owner_written``: the link row sits in this owner's own outbox and goes out with its first
    round. A recorded stop (refusal to start, crash, graceful stop) withholds the answer."""
    from hermes_cli import kanban_origin_creation as oc
    from hermes_cli import kanban_sync_runstate as runstate
    res = json.loads(str(registry.dispatch("kanban_create", {"title": "Build", "assignee": "default"},
                                           origin_intent=synced_owner)))
    assert not runstate.path().exists()  # never started: no record either way
    with kbc.connect_closing() as conn:
        assert oc.origin_link_fields(conn, res["task_id"], synced_owner["origin"]) == {
            "origin_chat_link": "owner_written"}
        runstate.record_down("not starting: board default: test")
        assert oc.origin_link_fields(conn, res["task_id"], synced_owner["origin"]) == {}


def test_owner_answer_survives_the_hubs_done_row_to_the_origin(synced_owner, monkeypatch):
    """[daveey] PR #2157 review 5890952298: the origin reads ``origin_chat_link`` from its replica's ``done`` row. The
    owner's hermes-hub (``advance_origin_create``) finishes the request with ``{**creation, "ok": True, "code":
    "origin_bound", "handoff_complete": True, "origin_receipt": receipt}``, where ``creation`` is this courier
    create's own result, and the origin's ``fetch_outcomes`` stores that result verbatim in ``sent.result``. Real hub
    row placement:macbook:24a806162e8729d0b6f2bf21 (11-13) keeps every creation key that way (``origin_creation``,
    ``placement``, ``gated`` ...), so the field rides through; this drives the real create output through that
    merge."""
    from hermes_cli import kanban_placement as kp
    creation = json.loads(str(registry.dispatch("kanban_create", {"title": "Build", "assignee": "default"},
                                                origin_intent=synced_owner)))
    receipt = {"status": "accepted", "request_id": creation["origin_creation"]["child_request_id"]}
    done = {**creation, "ok": True, "code": "origin_bound", "handoff_complete": True, "origin_receipt": receipt}
    monkeypatch.setattr(kp, "_sent_row", lambda rid: ("done", json.dumps(done)))
    seen: dict = {}
    tid, _ = kp.await_task_id(synced_owner["request_id"], hub_cli="hermes-hub", budget=0, result_out=seen)
    assert tid == creation["task_id"]
    assert kp.owner_chat_link(seen["result"]) == {"chat_link": "owner_written"}
