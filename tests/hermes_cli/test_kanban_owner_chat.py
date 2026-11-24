"""Every card has an owning chat (``kanban_owner_chat``): system producers get one standing ``Kanban owner`` chat
each, headless agent/CLI creates get one only for a declared producer, and a stop on such a card routes to that
chat instead of to nobody."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_create_authority as kca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_owner_chat as owner
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(owner, "_ENSURED", {})


def _links(conn, tid):
    """Owning chats: ``task_chat_links`` on a synced board, else the creating chat's ``tui`` subscription."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return [dict(r) for r in conn.execute(
            "SELECT chat_session, kind, released FROM task_chat_links WHERE task_id=?", (tid,))]
    return [{"chat_session": r[0], "kind": "created", "released": 0} for r in conn.execute(
        "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='tui'", (tid,))]


def _session(home, sid):
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        return db.get_session(sid), db.get_session_title(sid)
    finally:
        db.close()


def test_a_system_producer_card_gets_its_producers_owner_chat(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Repair sync drift", assignee="default", created_by="kanban-drift")
        sid = owner.session_id("kanban-drift")
        assert _links(conn, tid) == [{"chat_session": sid, "kind": "created", "released": 0}]
        assert conn.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND platform='tui' AND chat_id=?",
                            (tid, sid)).fetchone()
        assert kca.has_authority(conn, tid, None, durable=True)
        event = [e for e in kb.list_events(conn, tid) if e.kind == owner.EVENT]
        assert event and event[0].payload["producer"] == "kanban-drift"
    row, title = _session(kanban_home, sid)
    assert row["source"] == "desktop" and title == "Kanban owner: kanban-drift"


def test_one_owner_chat_per_producer_is_reused(kanban_home):
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="default", created_by="ci-main-watch")
        b = kb.create_task(conn, title="b", assignee="default", created_by="ci-main-watch")
        c = kb.create_task(conn, title="c", assignee="default", created_by="self-heal")
        sessions = {_links(conn, t)[0]["chat_session"] for t in (a, b)}
        assert sessions == {owner.session_id("ci-main-watch")}
        assert _links(conn, c)[0]["chat_session"] == owner.session_id("self-heal")


def test_a_card_from_a_registered_chat_keeps_that_chat_and_gets_no_owner_chat(kanban_home, monkeypatch):
    from hermes_state import SessionDB
    db = SessionDB(db_path=kanban_home / "state.db")
    try:
        db.create_session("20261124_120000_human", source="desktop")
    finally:
        db.close()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="From a chat", assignee="default", created_by="default",
                             session_id="20261124_120000_human", require_scope_authority=True)
        assert [link["chat_session"] for link in _links(conn, tid)] == ["20261124_120000_human"]
        assert not any(e.kind == owner.EVENT for e in kb.list_events(conn, tid))


def test_headless_agent_create_without_a_producer_is_still_refused(kanban_home):
    with kbc.connect_closing() as conn:
        with pytest.raises(kca.CreateAuthorityRefused):
            kb.create_task(conn, title="Orphan", assignee="default", created_by="default",
                           require_scope_authority=True)
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title='Orphan'").fetchone()[0] == 0


def test_headless_agent_create_with_a_declared_producer_gets_its_owner_chat(kanban_home, monkeypatch):
    monkeypatch.setenv(owner.ENV, "kanban-drift")
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Drift", assignee="default", created_by="default",
                             require_scope_authority=True, owner_producer=owner.declared_producer())
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("kanban-drift")


def test_declared_producer_is_the_running_cron_job_else_the_env_outside_agent_turns(monkeypatch):
    monkeypatch.delenv(owner.ENV, raising=False)
    assert owner.declared_producer(None) is None
    assert owner.declared_producer("session-tool-task") is None
    assert owner.declared_producer("cron:a9ff61f12e19:exec1") == "cron:a9ff61f12e19"
    monkeypatch.setenv(owner.ENV, "kanban-drift")
    assert owner.declared_producer(None) == "kanban-drift"
    assert owner.declared_producer(None, allow_env=False) is None
    assert owner.declared_producer("cron:a9ff61f12e19:exec1") == "cron:a9ff61f12e19"


def test_the_kanban_create_tool_refuses_a_headless_caller_and_owns_a_declared_one(kanban_home, monkeypatch):
    from tools import kanban_tools
    refused = json.loads(kanban_tools._handle_create({"title": "Headless", "assignee": "default"}))
    assert refused.get("error") or refused.get("success") is False
    monkeypatch.setenv(owner.ENV, "kanban-drift")
    made = json.loads(kanban_tools._handle_create({"title": "Monitor card", "assignee": "default"}))
    tid = made.get("task_id") or (made.get("task") or {}).get("id")
    assert tid, made
    with kbc.connect_closing() as conn:
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("kanban-drift")


def test_the_kanban_create_tool_in_a_cron_job_owns_the_card_by_that_job(kanban_home, monkeypatch):
    from tools import kanban_tools
    monkeypatch.delenv(owner.ENV, raising=False)
    made = json.loads(kanban_tools._handle_create({"title": "Cron card", "assignee": "default"},
                                                  task_id="cron:job123:exec9"))
    tid = made.get("task_id") or (made.get("task") or {}).get("id")
    assert tid, made
    with kbc.connect_closing() as conn:
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("cron:job123")


def test_a_cron_script_runs_with_its_jobs_producer(kanban_home, tmp_path, monkeypatch):
    from cron import scheduler_script
    scripts = kanban_home / "scripts"
    scripts.mkdir()
    script = scripts / "show_producer.py"
    script.write_text(f"import os\nprint(os.environ.get({owner.ENV!r}, 'none'))\n")
    monkeypatch.delenv(owner.ENV, raising=False)
    ok, out = scheduler_script._run_job_script(str(script), job_id="job123")
    assert ok, out
    assert out.strip() == "cron:job123"


def test_a_stop_on_a_producer_card_routes_to_its_owner_chat(kanban_home):
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli import kanban_spend_decision as decision
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Watcher card", assignee="default", created_by="pickup-stall-detector")
        helpers._runs(conn, tid, ksb.DEFAULT_RUNS)
        assert ksb.hold(conn, tid, lim=helpers.LIMITS) == "triage"
        with kb.write_txn(conn):
            route = decision.producer(conn, tid, "test")
        assert route["kind"] == "chats", route
        subs = {r[0] for r in conn.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id=?", (tid,))}
        assert owner.session_id("pickup-stall-detector") in subs


def test_switched_off_restores_the_old_behaviour(kanban_home, monkeypatch):
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Legacy", assignee="default", created_by="self-heal")
        assert _links(conn, tid) == []
        with pytest.raises(kca.CreateAuthorityRefused):
            kb.create_task(conn, title="Legacy agent", assignee="default", created_by="default",
                           require_scope_authority=True, owner_producer="kanban-drift")


def test_backfill_links_ownerless_open_cards_once(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(owner, "enabled", lambda: False)
        old = kb.create_task(conn, title="Old ownerless", assignee="default", created_by="worker-claim-watch")
        finished = kb.create_task(conn, title="Done ownerless", assignee="default", created_by="worker-claim-watch")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (finished,))
        monkeypatch.setattr(owner, "enabled", lambda: True)
        dry = owner.backfill(conn, apply=False)
        assert dry == {"ownerless": 1, "linked": 0, "by_producer": {"worker-claim-watch": 1}, "failed": []}
        assert _links(conn, old) == []
        applied = owner.backfill(conn, apply=True)
        assert applied["linked"] == 1
        assert _links(conn, old)[0]["chat_session"] == owner.session_id("worker-claim-watch")
        assert _links(conn, finished) == []
        assert owner.backfill(conn, apply=True)["ownerless"] == 0


def test_on_a_synced_board_the_owner_chat_is_a_created_chat_link_and_renews(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", "hub")
    ksa._reset_cache_for_tests()
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone()
        tid = kb.create_task(conn, title="Drift", assignee="default", created_by="kanban-drift")
        sid = owner.session_id("kanban-drift")
        rows = conn.execute("SELECT chat_gateway, chat_session, kind, released FROM task_chat_links "
                            "WHERE task_id=?", (tid,)).fetchall()
        assert [tuple(r) for r in rows] == [("hub", sid, "created", 0)]
        # the renewal reader itself accepts the owner chat as this card's owner
        assert kca._accepts(conn, tid, sid, "hub", "default")
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """A synced board (``task_chat_links``) on gateway ``hub``."""
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import kanban_sync_client as ksc
    from tests.hermes_cli import kanban_sync_support as support
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", "hub")
    ksa._reset_cache_for_tests()
    try:
        yield conn
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


def test_link_reconcile_keeps_the_owner_chat_as_owner(synced):
    from hermes_cli import kanban_chat_links as links
    tid = kb.create_task(synced, title="Drift", assignee="default", created_by="kanban-drift")
    links.reconcile(synced, "default", "hub", ledger=False)
    rows = [tuple(r) for r in synced.execute(
        "SELECT kind, released, chat_session FROM task_chat_links WHERE task_id=? AND kind='created'", (tid,))]
    assert rows == [("created", 0, owner.session_id("kanban-drift"))]
    assert kca.has_authority(synced, tid, None, durable=True)


def test_on_a_synced_board_a_stop_routes_to_the_owner_chat(synced):
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli import kanban_spend_decision as decision
    tid = kb.create_task(synced, title="Watcher card", assignee="default", created_by="pickup-stall-detector")
    sid = owner.session_id("pickup-stall-detector")
    assert not synced.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id=?", (tid,)).fetchone(), \
        "no subscription before a decision: the owner chat is not woken by every completion"
    helpers._runs(synced, tid, ksb.DEFAULT_RUNS)
    assert ksb.hold(synced, tid, lim=helpers.LIMITS) == "triage"
    with kb.write_txn(synced):
        route = decision.producer(synced, tid, "test")
    assert route["kind"] == "chats", route
    bound = [r[0] for r in synced.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id=?", (tid,))]
    assert len(bound) == 1 and bound[0].rsplit("/", 1)[-1] == sid, bound


def test_the_owner_chat_is_pinned_seeded_and_never_an_empty_draft(kanban_home):
    from hermes_state import SessionDB
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a", assignee="default", created_by="ci-main-watch")
    sid = owner.session_id("ci-main-watch")
    db = SessionDB(db_path=kanban_home / "state.db")
    try:
        row = db.get_session(sid)
        assert row["pinned"] and row["message_count"] == 1
        assert sid not in db.empty_draft_ids(older_than_s=-10)
    finally:
        db.close()


def test_an_archived_owner_chat_is_restored_and_keeps_its_cards(kanban_home, monkeypatch):
    from hermes_state import SessionDB
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a", assignee="default", created_by="ci-main-watch")
    sid = owner.session_id("ci-main-watch")
    db = SessionDB(db_path=kanban_home / "state.db")
    try:
        db.set_session_archived(sid, True)
    finally:
        db.close()
    monkeypatch.setattr(owner, "_ENSURED", {})  # past RECHECK_S, or a new process
    with kbc.connect_closing() as conn:
        first = conn.execute("SELECT id FROM tasks WHERE title='a'").fetchone()[0]
        assert owner.backfill(conn, apply=True)["linked"] == 1  # the hourly pass finds the archived owner
        assert _links(conn, first) == [{"chat_session": sid, "kind": "created", "released": 0}]
        kb.create_task(conn, title="b", assignee="default", created_by="ci-main-watch")
    db = SessionDB(db_path=kanban_home / "state.db")
    try:
        assert not db.get_session(sid)["archived"]
    finally:
        db.close()


def test_a_deleted_owner_chat_is_replaced_and_creates_keep_working(kanban_home, monkeypatch):
    from hermes_state import SessionDB
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="a", assignee="default", created_by="ci-main-watch")
    db = SessionDB(db_path=kanban_home / "state.db")
    try:
        db.delete_session(owner.session_id("ci-main-watch"))
    finally:
        db.close()
    monkeypatch.setattr(owner, "_ENSURED", {})  # a new process
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="b", assignee="default", created_by="ci-main-watch")
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("ci-main-watch", 2)


def test_state_db_trouble_leaves_a_system_card_ownerless_but_refuses_an_agent(kanban_home, monkeypatch):
    def broken(producer):
        raise OSError("database is locked")
    monkeypatch.setattr(owner, "ensure", broken)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Monitor card", assignee="default", created_by="ci-main-watch")
        assert _links(conn, tid) == []
        assert any(e.kind == owner.UNAVAILABLE for e in kb.list_events(conn, tid))
        assert owner.backfill(conn, apply=False)["ownerless"] == 1
        with pytest.raises(kca.CreateAuthorityRefused):
            kb.create_task(conn, title="Agent card", assignee="default", created_by="default",
                           require_scope_authority=True, owner_producer="kanban-drift")


def test_inside_an_agent_turn_the_producer_env_var_does_not_buy_ownership(kanban_home, monkeypatch):
    from tools import kanban_tools
    monkeypatch.setenv(owner.ENV, "kanban-drift")
    refused = json.loads(kanban_tools._handle_create({"title": "Agent card", "assignee": "default"},
                                                     task_id="20261124_120000_agentturn"))
    assert refused.get("error") or refused.get("success") is False, refused
    assert owner.declared_producer("20261124_120000_agentturn", allow_env=False) is None


def test_a_human_terminal_create_is_owned_by_the_terminal_owner_chat(kanban_home, monkeypatch):
    import argparse

    from hermes_cli import kanban as kc
    monkeypatch.delenv(owner.ENV, raising=False)
    monkeypatch.setattr(kc, "_agent_invoked", lambda: False)
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", "Typed by a human", "--assignee", "default"])) == 0
    with kbc.connect_closing() as conn:
        tid = conn.execute("SELECT id FROM tasks WHERE title='Typed by a human'").fetchone()[0]
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id(owner.TERMINAL)


def test_backfill_leaves_a_card_whose_chat_released_it(synced, monkeypatch):
    monkeypatch.setattr(owner, "enabled", lambda: False)
    tid = kb.create_task(synced, title="Released", assignee="default", created_by="ci-main-watch")
    with kb.write_txn(synced):
        synced.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,"
                       "released,created_at) VALUES ('u1',?,'hub',NULL,'20261124_120000_gone','created',1,0)", (tid,))
    monkeypatch.setattr(owner, "enabled", lambda: True)
    assert owner.backfill(synced, apply=True) == {"ownerless": 0, "linked": 0, "by_producer": {}, "failed": []}


def test_owner_chats_cli_lists_then_links(kanban_home, monkeypatch, capsys):
    import argparse

    from hermes_cli import kanban as kc
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Old", assignee="default", created_by="self-heal")
    monkeypatch.setattr(owner, "enabled", lambda: True)
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc.kanban_command(root.parse_args(["kanban", "owner-chats", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["ownerless"] == 1
    assert kc.kanban_command(root.parse_args(["kanban", "owner-chats", "--apply"])) == 0
    assert "linked 1" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("self-heal")


def test_a_producer_pays_for_state_db_once_per_process_not_per_card(kanban_home, monkeypatch):
    opened = []
    real = owner._open_store
    monkeypatch.setattr(owner, "_open_store", lambda path: opened.append(path) or real(path))
    with kbc.connect_closing() as conn:
        for n in range(3):
            kb.create_task(conn, title=f"card {n}", assignee="default", created_by="ci-main-watch")
    assert len(opened) == 1


def test_backfill_moves_cards_off_a_deleted_owner_chat(synced, monkeypatch):
    from hermes_state import SessionDB
    from hermes_constants import get_hermes_home
    tid = kb.create_task(synced, title="Watcher card", assignee="default", created_by="ci-main-watch")
    old = owner.session_id("ci-main-watch")
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        db.delete_session(old)
    finally:
        db.close()
    monkeypatch.setattr(owner, "_ENSURED", {})
    assert owner.backfill(synced, apply=True)["linked"] == 1
    rows = {tuple(r) for r in synced.execute(
        "SELECT chat_session, released FROM task_chat_links WHERE task_id=? AND kind='created'", (tid,))}
    # the dead link stays (creating-chat links are never released on their own gateway); the live one owns it
    assert rows == {(old, 0), (owner.session_id("ci-main-watch", 2), 0)}
    assert kca.has_authority(synced, tid, None, durable=True)
    assert owner.backfill(synced, apply=True)["ownerless"] == 0


def test_dispatcher_maintenance_owns_ownerless_cards_hourly(kanban_home, monkeypatch, owner_backfill):
    from hermes_cli import kanban_dispatch_maintenance as maintenance
    monkeypatch.setattr(owner, "_NEXT_BACKFILL", {})
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Made while off", assignee="default", created_by="self-heal")
        monkeypatch.setattr(owner, "enabled", lambda: True)
        clock = iter([1000.0, 1000.0 + 60, 1000.0 + owner.BACKFILL_EVERY_S + 1])
        monkeypatch.setattr(maintenance, "time", __import__("types").SimpleNamespace(monotonic=lambda: next(clock)),
                            raising=False)
        assert owner.maybe_backfill(conn, now=1000.0)["linked"] == 1
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("self-heal")
        assert owner.maybe_backfill(conn, now=1060.0) is None
        assert owner.maybe_backfill(conn, now=1000.0 + owner.BACKFILL_EVERY_S + 1)["ownerless"] == 0


def test_a_create_that_opts_out_gets_no_owner_chat(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Self-work intake", assignee="default", created_by="gw:hub",
                             settle_owner_chat=False)
        assert _links(conn, tid) == []


def test_decomposed_children_of_a_producer_card_are_owned(kanban_home):
    from hermes_cli.kanban_db_graph import decompose_triage_task
    with kbc.connect_closing() as conn:
        root = kb.create_task(conn, title="Root", assignee="default", created_by="ci-main-watch", triage=True)
        kids = decompose_triage_task(conn, root, root_assignee="default",
                                     children=[{"title": "a", "assignee": "default"},
                                               {"title": "b", "assignee": "default"}])
        assert kids and all(kca.has_authority(conn, k, None, durable=True) for k in kids)


def test_every_card_of_a_terminal_swarm_is_owned(kanban_home, monkeypatch):
    from hermes_cli import kanban_swarm
    monkeypatch.setattr(kanban_swarm, "_agent_invoked", lambda: False, raising=False)
    with kbc.connect_closing() as conn:
        out = kanban_swarm.create_swarm(conn, goal="Find the flake", workers=[kanban_swarm.parse_worker_arg("default:look")],
                                        verifier_assignee="default", synthesizer_assignee="default")
        ids = [r[0] for r in conn.execute("SELECT id FROM tasks")]
        assert out and len(ids) >= 3
        assert all(kca.has_authority(conn, t, None, durable=True) for t in ids)


def test_backfill_restores_an_archived_owner_chat_even_while_it_is_cached(synced):
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    tid = kb.create_task(synced, title="Watcher card", assignee="default", created_by="ci-main-watch")
    sid = owner.session_id("ci-main-watch")
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        db.set_session_archived(sid, True)
    finally:
        db.close()
    assert owner.backfill(synced, apply=True)["linked"] == 1  # the cache is fresh: the backfill must still recheck
    db = SessionDB(db_path=get_hermes_home() / "state.db")
    try:
        assert not db.get_session(sid)["archived"]
    finally:
        db.close()
    assert owner.backfill(synced, apply=True) == {"ownerless": 0, "linked": 0, "by_producer": {}, "failed": []}
    assert sum(e.kind == owner.EVENT for e in kb.list_events(synced, tid)) == 1


def test_an_owner_chat_of_another_gateway_is_not_judged_dead_here(synced, monkeypatch):
    monkeypatch.setattr(owner, "enabled", lambda: False)
    tid = kb.create_task(synced, title="Inherited", assignee="default", created_by="ci-main-watch")
    with kb.write_txn(synced):
        synced.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,"
                       "released,created_at) VALUES ('u2',?,'worker-1',NULL,?,'created',0,0)",
                       (tid, owner.session_id("ci-main-watch")))
    monkeypatch.setattr(owner, "enabled", lambda: True)
    assert owner._owner_refs(synced, tid, True) == ([], True)


def test_the_dispatcher_backfill_links_in_batches_and_catches_up_sooner(kanban_home, monkeypatch, owner_backfill):
    monkeypatch.setattr(owner, "_NEXT_BACKFILL", {})
    monkeypatch.setattr(owner, "BACKFILL_BATCH", 2)
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        for n in range(3):
            kb.create_task(conn, title=f"old {n}", assignee="default", created_by="self-heal")
        monkeypatch.setattr(owner, "enabled", lambda: True)
        assert owner.maybe_backfill(conn, now=0.0)["linked"] == 2
        assert owner.maybe_backfill(conn, now=owner.BACKFILL_CATCHUP_S - 1) is None
        assert owner.maybe_backfill(conn, now=owner.BACKFILL_CATCHUP_S)["linked"] == 1
        assert owner.maybe_backfill(conn, now=owner.BACKFILL_CATCHUP_S + owner.BACKFILL_CATCHUP_S) is None


def test_dispatcher_maintenance_hook_runs_the_backfill(kanban_home, monkeypatch, owner_backfill):
    from hermes_cli import kanban_dispatch_maintenance as maintenance
    monkeypatch.setattr(owner, "_NEXT_BACKFILL", {})
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Before owner chats", assignee="default", created_by="oom-detector")
        monkeypatch.setattr(owner, "enabled", lambda: True)
        maintenance._own_ownerless_cards(conn)
        assert _links(conn, tid)[0]["chat_session"] == owner.session_id("oom-detector")


def test_one_failing_producer_neither_stops_the_pass_nor_reruns_every_tick(kanban_home, monkeypatch, owner_backfill):
    monkeypatch.setattr(owner, "_NEXT_BACKFILL", {})
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        bad = kb.create_task(conn, title="bad", assignee="default", created_by="broken-producer")
        good = kb.create_task(conn, title="good", assignee="default", created_by="self-heal")
        monkeypatch.setattr(owner, "enabled", lambda: True)
        real, calls = owner.ensure, []

        def ensure(producer, *, force=False):
            calls.append(producer)
            if producer == "broken-producer":
                raise OSError("database is locked")
            return real(producer, force=force)
        monkeypatch.setattr(owner, "ensure", ensure)
        result = owner.maybe_backfill(conn, now=0.0)
        assert result["failed"] == ["broken-producer"] and result["linked"] == 1
        assert _links(conn, good) and not _links(conn, bad)
        assert owner.maybe_backfill(conn, now=1.0) is None  # not every tick
        assert owner.maybe_backfill(conn, now=owner.BACKFILL_CATCHUP_S)["failed"] == ["broken-producer"]
        assert calls.count("broken-producer") == 2


def test_a_card_that_stays_ownerless_after_linking_does_not_hold_a_batch_slot(kanban_home, monkeypatch):
    monkeypatch.setattr(owner, "_UNLINKABLE", set())
    monkeypatch.setattr(owner, "enabled", lambda: False)
    with kbc.connect_closing() as conn:
        stuck = kb.create_task(conn, title="stuck", assignee="default", created_by="self-heal")
        other = kb.create_task(conn, title="other", assignee="default", created_by="self-heal")
        monkeypatch.setattr(owner, "enabled", lambda: True)
        real = owner.link
        monkeypatch.setattr(owner, "link", lambda c, t, p, s: False if t == stuck else real(c, t, p, s))
        assert owner.backfill(conn, apply=True, limit=1)["linked"] == 0  # the oldest card cannot be owned
        assert stuck in owner._UNLINKABLE
        assert owner.backfill(conn, apply=True, limit=1)["linked"] == 1  # the next pass moves on
        assert _links(conn, other)
