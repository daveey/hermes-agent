"""A contained card whose authority finished is described as stranded, woken once, and escalated when no chat owns it. [daveey]

Replays the 11-19/20 hub strands: cards that contained themselves naming an authority that then completed (or was
archived) without releasing, so the held card sat in triage with no owner.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_triage_sweep as sweep
from hermes_cli import kanban_wait_info as kwi
from hermes_cli.kanban_session_wait_rows import _local_wait_info
from tests.hermes_cli import test_kanban_containment_lifecycle as helpers

board = helpers.board
NOW = 1_800_000_000.0
MIN = 60
KEY = "idempotency_key LIKE 'containment-strand:%'"


def _contained(conn, *, producer=False):
    relay, _consumer, authority, target, spec = helpers.setup(conn)
    prod = None
    if producer:
        prod = kb.create_task(conn, title="Producer fixture", assignee="default")
        spec = {**spec, "producer": {**target, "task_id": prod}}
    helpers.contain(conn, relay, target, spec)
    return relay, authority, prod


def _finish(conn, task, status="done", at=NOW - 45 * MIN):
    conn.execute("UPDATE tasks SET status=?, completed_at=?, current_run_id=NULL WHERE id=?", (status, at, task))


def _wait(conn, task, now=NOW) -> dict:
    wait = _local_wait_info(conn, [task], now=now).get(task)
    assert wait is not None
    return wait


def test_authority_done_over_30_min_is_agent_owned_and_stalled_at_once(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 31 * MIN)
    wait = _wait(board, relay)
    assert wait["agent_owned"] is True and wait["agent_owned_certain"] is True
    assert wait["wait_state"] == "stalled" and wait["waiting_since"] == NOW - 31 * MIN
    assert wait["premise"].startswith("contained:")
    ep = board.execute("SELECT containment_episode FROM tasks WHERE id=?", (relay,)).fetchone()[0]
    assert wait["contained"] == {"episode": ep, "authority": {"id": authority, "status": "done"}, "producer": None}
    assert authority in wait["waiting_on"]


def test_authority_done_under_30_min_does_not_wake(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 10 * MIN)
    wait = _wait(board, relay)
    assert wait["agent_owned"] is False and wait["premise"] is None and wait["wait_state"] == "waiting"


def test_open_authority_is_not_agent_owned(board):
    relay, authority, _ = _contained(board)
    wait = _wait(board, relay)
    assert wait["agent_owned"] is False and wait["premise"] is None
    assert wait["waiting_on"] == f"release by card {authority} (running)"
    assert wait["contained"]["authority"] == {"id": authority, "status": "running"}


def test_open_producer_keeps_waiting_on_the_producer(board):
    relay, authority, prod = _contained(board, producer=True)
    _finish(board, authority, "archived")
    wait = _wait(board, relay)
    assert wait["agent_owned"] is False and prod in wait["waiting_on"]
    assert wait["contained"]["producer"]["id"] == prod


def test_producer_with_a_newer_completed_run_leaves_the_card_stranded(board):
    relay, authority, prod = _contained(board, producer=True)
    _finish(board, authority, "archived")
    board.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
                  "VALUES (?, 'done', 'completed', ?, ?)", (prod, NOW - 50 * MIN, NOW - 40 * MIN))
    assert _wait(board, relay)["agent_owned"] is True


def test_premise_is_stable_per_episode_and_changes_when_the_authority_completes_again(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 60 * MIN)
    first = _wait(board, relay)["premise"]
    assert _wait(board, relay, now=NOW + 3600)["premise"] == first  # second tick: same premise, no new wake
    _finish(board, authority, at=NOW - 40 * MIN)  # reopened, release attempt failed, completed again
    assert _wait(board, relay)["premise"] != first


def test_replica_row_with_contained_facts_is_described_the_same_way():
    card = {"status": "triage", "contained": {
        "episode": "019a98b4-0000-7000-8000-000000000001", "contained_at": NOW - 3600,
        "authority": {"id": "t_1123456789abcdef", "status": "archived", "at": NOW - 40 * MIN}, "producer": None}}
    wait = kwi.describe_replica(card, now=NOW)
    assert wait["agent_owned"] is True and wait["contained"]["authority"]["status"] == "archived"
    assert "no supported exit exists" in wait["waiting_on"]


def test_no_chat_gets_one_supervisor_card_per_episode_and_no_duplicate(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 60 * MIN)
    created = sweep.escalate_stranded_contained(board, now=NOW)
    assert len(created) == 1
    cards = board.execute(f"SELECT id, body, idempotency_key FROM tasks WHERE {KEY}").fetchall()
    assert [c["id"] for c in cards] == created
    ep = board.execute("SELECT containment_episode FROM tasks WHERE id=?", (relay,)).fetchone()[0]
    assert cards[0]["idempotency_key"] == f"containment-strand:{relay}:{ep}"
    assert relay in cards[0]["body"] and authority in cards[0]["body"] and "kanban_resume_contained" in cards[0]["body"]
    assert sweep.escalate_stranded_contained(board, now=NOW + 3600) == []  # second tick
    assert board.execute(f"SELECT COUNT(*) FROM tasks WHERE {KEY}").fetchone()[0] == 1
    assert board.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND author='triage-sweep'",
                         (relay,)).fetchone()[0] == 1


def test_open_authority_or_a_linked_chat_creates_no_card(board):
    relay, authority, _ = _contained(board)
    assert sweep.escalate_stranded_contained(board, now=NOW) == []  # authority open
    _finish(board, authority, at=NOW - 60 * MIN)
    board.execute("UPDATE tasks SET session_id='20261120_000000_abcdef' WHERE id=?", (relay,))
    assert sweep.escalate_stranded_contained(board, now=NOW) == []  # a chat owns it: the owner wake handles it
    assert board.execute(f"SELECT COUNT(*) FROM tasks WHERE {KEY}").fetchone()[0] == 0
    assert [c["id"] for c in sweep.stranded_contained(board, now=NOW)] == [relay]


def test_escalation_writes_no_event_on_the_held_card(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 60 * MIN)
    before = board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind != 'commented'",
                           (relay,)).fetchone()[0]
    sweep.escalate_stranded_contained(board, now=NOW)
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind != 'commented'",
                         (relay,)).fetchone()[0] == before


def test_the_dispatcher_triage_sweep_runs_the_escalation_once(board):
    relay, authority, _ = _contained(board)
    _finish(board, authority, at=NOW - 60 * MIN)
    sweep.sweep_stale_triage(board, now=int(NOW))
    sweep.sweep_stale_triage(board, now=int(NOW) + 3600)
    assert board.execute(f"SELECT COUNT(*) FROM tasks WHERE {KEY}").fetchone()[0] == 1
    assert board.execute("SELECT status FROM tasks WHERE id=?", (relay,)).fetchone()[0] == "triage"  # still held
