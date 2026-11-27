"""Phantom-ref checks consult this host's Board replica (``kanban/sync-shadow/<board>.db``) before calling an id
hallucinated.

worker-1 completing t_01a1ceb5fa5abe36 (run 13081): its live ``kanban.db`` held 227 of the board's ~9.7k cards, its
Board replica held all of them, and a fresh hub replica answered authoritatively without the 2-minute-old hub card.
The completion gate refused ``created_cards`` naming t_01a1cedb2faf7341 (created by an earlier run of the same card
on worker-3, linked as its child), and the summary scan flagged t_01a1cf4d49ca68bb (a hub-created card).
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

OTHER_NODE_CHILD = "t_01a1cedb2faf7341"  # created on worker-3 by an earlier run of the completing card
HUB_CARD = "t_01a1cf4d49ca68bb"  # created on the hub two minutes before the summary named it
FOREIGN_CARD = "t_01a1cf4eda4b0000"  # real Board card, another profile made it, not linked to the completer
MADE_UP = "t_deadbeef00ff"


def _fresh_hub_without_cards(state_dir):
    """A hub replica that answers (fresh, online) but does not hold the Board cards."""
    state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_dir / "hub.db")
    conn.execute("CREATE TABLE replica (coll TEXT, gw TEXT, key TEXT, data TEXT, updated_at INTEGER)")
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    # The real courier store also has an empty outbound request journal.
    conn.execute("CREATE TABLE sent(id TEXT PRIMARY KEY, state TEXT, body TEXT)")
    conn.execute("INSERT INTO meta VALUES ('gateway', 'worker-1')")
    conn.execute("INSERT INTO meta VALUES ('last_ok', ?)", (str(time.time() - 2),))
    conn.commit()
    conn.close()


def _seed_replica(rows, links=(), board="default"):
    replica = kbc.connect(ksc.shadow_path(board))
    try:
        now = int(time.time())
        with kbc.write_txn(replica):
            for tid, created_by, created_on in rows:
                replica.execute("INSERT INTO tasks (id, title, status, created_at, created_by, created_on) "
                                "VALUES (?, ?, 'todo', ?, ?, ?)", (tid, tid, now, created_by, created_on))
            for parent, child in links:
                replica.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
    finally:
        replica.close()


@pytest.fixture
def worker_node(tmp_path, monkeypatch):
    """worker-1's shape: Board sync on, a fresh hub replica, and a Board replica holding cards the live file lacks."""
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hubstate"))
    support.write_sync_config(home, True)
    _fresh_hub_without_cards(tmp_path / "hubstate")
    leases = support.lease_client()
    next(leases)
    conn = kbc.connect(home / "kanban.db")
    tid = kb.create_task(conn, title="workers fall behind core", assignee="default")
    kb.claim_task(conn, tid)
    _seed_replica([(OTHER_NODE_CHILD, "default", "worker-3"), (HUB_CARD, "default", "hub"),
                   (FOREIGN_CARD, "someone-else", "worker-2")], links=[(tid, OTHER_NODE_CHILD)])
    yield conn, tid
    conn.close()
    leases.close()


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind))]


def _diag_refs(conn, tid):
    diags = kd.compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid), [], now=int(time.time()))
    return [d.data["phantom_refs"] for d in diags if d.kind == "prose_phantom_refs"]


def test_created_card_from_another_worker_node_is_verified_from_the_board_replica(worker_node):
    """(1) The child exists only in the Board replica (created on worker-3, linked there): verified, not phantom."""
    conn, tid = worker_node
    assert kb.get_task(conn, OTHER_NODE_CHILD) is None  # not in this node's partial live file
    assert kb.complete_task(conn, tid, summary="filed the stall detector", created_cards=[OTHER_NODE_CHILD])
    assert _events(conn, tid, "completion_blocked_hallucination") == []
    (done,) = _events(conn, tid, "completed")
    assert done["verified_cards"] == [OTHER_NODE_CHILD]


def test_summary_naming_a_hub_card_only_the_board_replica_holds_is_not_flagged(worker_node):
    """(2) A fresh hub replica lacking the card no longer wins: the Board replica has it."""
    conn, tid = worker_node
    assert kb.complete_task(conn, tid, summary=f"worker-6 claimed {HUB_CARD} at 10:28:50Z")
    assert _events(conn, tid, "suspected_hallucinated_references") == []
    assert _diag_refs(conn, tid) == []


def test_short_prefix_of_a_board_replica_card_is_not_flagged_but_an_orphan_prefix_is(worker_node):
    """A summary citing a replica-only card by its 8-hex prefix resolves through the Board replica."""
    conn, tid = worker_node
    assert kb.complete_task(conn, tid, summary=f"follow-up of {HUB_CARD[:10]}; also t_feedf00d")
    (flag,) = _events(conn, tid, "suspected_hallucinated_references")
    assert flag["phantom_refs"] == ["t_feedf00d"]
    assert _diag_refs(conn, tid) == [["t_feedf00d"]]


def test_id_found_nowhere_is_still_flagged_and_still_refused(worker_node):
    conn, tid = worker_node
    with pytest.raises(kb.HallucinatedCardsError) as err:
        kb.complete_task(conn, tid, summary="done", created_cards=[OTHER_NODE_CHILD, MADE_UP])
    assert err.value.phantom == [MADE_UP]
    (blocked,) = _events(conn, tid, "completion_blocked_hallucination")
    assert blocked["phantom_cards"] == [MADE_UP] and blocked["verified_cards"] == [OTHER_NODE_CHILD]
    assert kb.get_task(conn, tid).status == "running"
    assert kb.complete_task(conn, tid, summary=f"saw {HUB_CARD} and {MADE_UP}")
    (flag,) = _events(conn, tid, "suspected_hallucinated_references")
    assert flag["phantom_refs"] == [MADE_UP]
    assert _diag_refs(conn, tid) == [[MADE_UP]]


def test_replica_card_another_profile_made_is_still_phantom_as_created_card(worker_node):
    """Existence in the replica is not ownership: the trust rule (creator or linked child) still applies."""
    conn, tid = worker_node
    with pytest.raises(kb.HallucinatedCardsError) as err:
        kb.complete_task(conn, tid, summary="done", created_cards=[FOREIGN_CARD])
    assert err.value.phantom == [FOREIGN_CARD]
    assert kb.complete_task(conn, tid, summary=f"see {FOREIGN_CARD}")  # merely naming a real card is fine
    assert _events(conn, tid, "suspected_hallucinated_references") == []


def test_replica_card_created_by_the_completing_task_id_is_verified(worker_node):
    conn, tid = worker_node
    _seed_replica([("t_01a1cf000000beef", tid, "worker-3")])
    assert kb.complete_task(conn, tid, summary="done", created_cards=["t_01a1cf000000beef"])


def test_replica_link_verifies_a_child_another_profile_created(worker_node):
    """The replica's parent link counts like a local one (a planner profile made the child, the worker linked it)."""
    conn, tid = worker_node
    _seed_replica([("t_01a1cf00000c0de0", "planner", "worker-3")], links=[(tid, "t_01a1cf00000c0de0")])
    assert kb.complete_task(conn, tid, summary="done", created_cards=["t_01a1cf00000c0de0"])


def test_unreadable_replica_is_skipped_and_the_readable_one_still_answers(worker_node):
    conn, tid = worker_node
    ksc.shadow_path("aaa-broken").write_bytes(b"not a sqlite file" * 64)  # sorts before default.db
    assert kb.complete_task(conn, tid, summary=f"see {HUB_CARD}", created_cards=[OTHER_NODE_CHILD])
    assert _events(conn, tid, "suspected_hallucinated_references") == []


def test_ids_spread_over_two_board_replicas_all_resolve(worker_node):
    """A summary naming cards on two boards: the second replica is still asked for what the first lacked."""
    conn, tid = worker_node
    _seed_replica([("t_01a1cf00000bb000", "default", "hub")], board="bb")  # bb.db sorts before default.db
    assert kb.complete_task(conn, tid, summary=f"see t_01a1cf00000bb000 and {HUB_CARD}")
    assert _events(conn, tid, "suspected_hallucinated_references") == []


def test_nothing_to_look_up_opens_no_replica(worker_node, monkeypatch):
    from hermes_cli import kanban_board_replica_refs as refs
    from hermes_cli import kanban_generation

    opened = []
    monkeypatch.setattr(kanban_generation, "open_ro", lambda p, *a, **k: opened.append(p))
    assert refs.replica_tasks([], "t_x") == ({}, set())
    assert opened == []


def test_missing_board_replica_is_not_created_and_changes_nothing(tmp_path, monkeypatch):
    """No Board replica file: the lookup never creates one, and the gate behaves exactly as before."""
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hubstate"))
    conn = kbc.connect(home / "kanban.db")
    try:
        tid = kb.create_task(conn, title="t", assignee="default")
        kb.claim_task(conn, tid)
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(conn, tid, summary="done", created_cards=[OTHER_NODE_CHILD])
        assert not ksc.shadow_path("default").exists()
    finally:
        conn.close()
