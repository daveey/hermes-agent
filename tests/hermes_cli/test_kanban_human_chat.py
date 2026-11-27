"""Every card blocked on a human has a human chat (``kanban_human_chat``), and that chat's outcome is red.

Fixtures are copied from the three hub cards behind t_01a21f5e47abf118 (2026-11-27):

* t_01a21bb1169deea2 (unforged CI, PR #13): created by placement session ``kanban-placement-macbook``, no creator
  card, its only creating chat ``worker-a1/default/kanban_owner_kanban_dispatcher``. No human chat: it gets the
  standing Needs-you chat, which then derives ``decision`` (red).
* t_01a211fcd5927efb (iPhone qualification): created by hub chat 20261122_233430_28d354. Already human: untouched.
"""
from __future__ import annotations

import json

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_human_chat as khc
from hermes_cli import kanban_owner_chat as owner
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as sync
from tests.hermes_cli import kanban_sync_support as support

UNFORGED_REQUEST = {
    "action": "Allow a production cache-publication fix to finish making Unforged CI green. Unblock = A (fix and "
              "verify); archive = B (stop without merging PR #13).",
    "why_human": "Tried: the fixture fixes passed hosted Linux and namespace tests; independent feasibility review "
                 "ruled out a fixture-only repair for the macOS permission failure. Only you can: change the explicit "
                 "tests/fixtures-only scope limit to permit production cache-publication changes; no safety "
                 "assertions will be weakened.",
    "interaction": "approval",
    "scope": "Situation: The two reported test failures are fixed, but restoring the retired Mac runner exposed a "
             "production bug publishing read-only cache directories. CI remains red and the PR is not merged.\n"
             "Options:\nA) Fix production publication: preserve read-only publication, add regressions, review and "
             "rerun CI; additional agent work, reversible PR changes.\nB) Stop without merging: leave the fixes in "
             "the open PR and macOS CI red; no further work, resume later if desired.\nRecommend: A - fixes the "
             "real Mac bug without hiding failures or relaxing sandbox protections.",
    "evidence": "called `Result::unwrap()` on an `Err` value: \"failed to publish materialized tree "
                "`b7039bd4341145c8e824525f4eec1c8abc47b273a5123f9e8de7c7806995b456`: Permission denied (os error 13)\"",
    "choices": [{"label": "A: Fix production publication (recommended)", "value": "A"},
                {"label": "B: Stop without merging", "value": "B"}],
}
IPHONE_REQUEST = {
    "action": "Unlock your iPhone and open the updated Baby Bear app; leave it open. Unblock = resume physical "
              "testing; archive = stop without default cutover.",
    "why_human": "Tried: installed and independently verified signed872, then attempted both supported no-kill launch "
                 "routes and inspected a fresh device screenshot showing the closed-padlock Lock Screen. Only you can: "
                 "unlock your iPhone with Face ID or passcode.",
    "interaction": "physical",
    "scope": "Situation: Baby Bear872 is installed, but the phone now shows its Lock Screen and the updated app has "
             "not been verified running.\nOptions:\nA) Unlock and open Baby Bear: leave it open for testing; about one "
             "minute of your attention; no setting change.\nB) Archive: stop qualification; native remains the code "
             "default; no cutover.\nRecommend: A - the app update is delivered; unlocking lets the remaining physical "
             "checks continue.",
    "evidence": "{\"bundle\": \"in.dbloom.babybear\", \"installedBuild\": \"872\"}\ninstalledMatchesVerifiedRelease True",
}
HUMAN_CHAT = "20261122_233430_28d354"
DISPATCHER_OWNER = "kanban_owner_kanban_dispatcher"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(khc, "home_gateway", lambda: "hub")
    monkeypatch.setattr(owner, "_ENSURED", {})
    monkeypatch.setattr(owner, "enabled", lambda: False)  # create-time owner chats are not under test here
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(HUMAN_CHAT, source="desktop")
    finally:
        db.close()
    return home


@pytest.fixture
def conn(home):
    with kbc.connect_closing(home / "kanban.db") as conn:
        sync.ensure_identity(conn, "default", "hub")
        yield conn


def _unforged(conn) -> str:
    """t_01a21bb1169deea2's shape: placement-session creator, no creator card, a worker's owner chat only."""
    tid = kb.create_task(conn, title="unforged main CI red: v1_1_exit_gates phase_2 + caos_worker1 exec_counter",
                         assignee="default", session_id="kanban-placement-macbook")
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,"
                     "created_at) VALUES (?,?,?,?,?,'created',0,1)",
                     (links.link_uid(tid, "worker-a1", "default", DISPATCHER_OWNER, "created"), tid, "worker-a1",
                      "default", DISPATCHER_OWNER))
    assert kb.block_task(conn, tid, kind="needs_input", human_request=UNFORGED_REQUEST)
    return tid


def _iphone(conn) -> str:
    tid = kb.create_task(conn, title="iPhone qualification: Baby Bear 872", assignee="default", session_id=HUMAN_CHAT)
    links.reconcile(conn, "default", "hub", ledger=False)
    assert kb.block_task(conn, tid, kind="needs_input", human_request=IPHONE_REQUEST)
    return tid


def _created(conn, tid):
    return sorted((r[0], r[1]) for r in conn.execute(
        "SELECT chat_gateway, chat_session FROM task_chat_links WHERE task_id=? AND kind='created' AND released=0",
        (tid,)))


def test_the_unforged_card_gets_the_needs_you_chat_and_the_iphone_card_is_left_alone(conn, home):
    unforged, iphone = _unforged(conn), _iphone(conn)
    before = _created(conn, iphone)
    assert khc.missing(conn) == [unforged]  # the dispatcher owner chat is no human chat; the iPhone chat is
    result = khc.sweep(conn)
    sid = khc.needs_you_session()
    assert result["linked"] == [{"task": unforged, "source": "needs_you", "session": sid}]
    assert _created(conn, unforged) == sorted([("hub", sid), ("worker-a1", DISPATCHER_OWNER)])
    assert _created(conn, iphone) == before
    assert khc.missing(conn) == []
    assert any(e.kind == khc.EVENT for e in kb.list_events(conn, unforged))
    assert not any(e.kind == khc.EVENT for e in kb.list_events(conn, iphone))
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        row = db.get_session(sid)
        assert row["source"] == "desktop" and row["pinned"] and not row["archived"]
        assert db.get_session_title(sid) == "Needs you"
    finally:
        db.close()
    assert khc.sweep(conn)["linked"] == []  # idempotent


def test_a_worker_card_gets_its_creator_chains_human_chat_not_needs_you(conn):
    parent = kb.create_task(conn, title="chat card", assignee="default", session_id=HUMAN_CHAT)
    links.reconcile(conn, "default", "hub", ledger=False)
    child = kb.create_task(conn, title="worker card", assignee="default", session_id="kanban-placement-x")
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                     (json.dumps({"creator_task_id": parent}), child))
        conn.execute("DELETE FROM task_chat_links WHERE task_id=?", (child,))
    assert kb.block_task(conn, child, kind="needs_input", human_request=UNFORGED_REQUEST)
    result = khc.sweep(conn)
    assert result["linked"] == [{"task": child, "source": "creator_chain", "session": HUMAN_CHAT}]
    assert ("hub", khc.needs_you_session()) not in _created(conn, child)


def test_a_non_home_gateway_links_nothing(conn, monkeypatch):
    unforged = _unforged(conn)
    monkeypatch.setattr(khc, "home_gateway", lambda: "somewhere-else")
    result = khc.sweep(conn)
    assert result["missing"] == [unforged] and result["linked"] == [] and result["home"] is False
    monkeypatch.setattr(owner, "enabled", lambda: True)
    monkeypatch.setattr(khc, "_NEXT", {})
    assert khc.maybe_sweep(conn, now=1e12) is None
    monkeypatch.setattr(khc, "home_gateway", lambda: "hub")
    assert khc.maybe_sweep(conn, now=1e12) is None  # throttled: the first call set the next run
    monkeypatch.setattr(khc, "_NEXT", {})
    assert [i["task"] for i in khc.maybe_sweep(conn, now=1e12)["linked"]] == [unforged]


def test_the_needs_you_chat_is_red_while_another_of_its_cards_runs(conn, home, monkeypatch):
    """Point 2: a linked human ask makes the outcome ``decision`` (the red stripe) even beside running work."""
    unforged = _unforged(conn)
    khc.sweep(conn)
    sid = khc.needs_you_session()
    running = kb.create_task(conn, title="other work", assignee="default", session_id="kanban-placement-y")
    owner.link(conn, running, khc.NEEDS_YOU, sid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [home / "kanban.db"])
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        out = so.refresh_outcome(sid, "card_change", db=db)
    finally:
        db.close()
        ksa._reset_cache_for_tests()
    assert out["kind"] == "decision"
    assert [i["card_id"] for i in out["items"]] == [unforged]
