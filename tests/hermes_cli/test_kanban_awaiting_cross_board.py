"""A qualified producer is read on its board without switching the waiter's write scope."""
import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    paths = [kb.board_own_db_path(b) for b in ("default", "runners")]
    for path in paths:
        kb.init_db(db_path=path)
    return paths


@pytest.mark.parametrize("source", [0, 1], ids=["default-to-runners", "runners-to-default"])
def test_schedule_reads_only_qualified_producer(boards, monkeypatch, source):
    target = 1 - source
    with kbc.connect_closing(db_path=boards[target]) as owner:
        producer = kb.create_task(owner, title="Original authentication owner", assignee="worker")
        assert kb.block_task(owner, producer, reason="Existing authentication", kind="needs_input", human_request={
            "action": "Authenticate. Unblock = retry; archive = abandon.", "why_human": "OS authentication",
            "interaction": "user_terminal", "scope": "fixture", "evidence": "fixture"})
        before = kb.get_task(owner, producer)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(boards[source]))
    with kbc.connect_closing(db_path=boards[source]) as conn:
        waiter = kb.create_task(conn, title="Release waiter", assignee="worker")
        board = ("default", "runners")[target]
        record = {"kind": "card", "ref": producer, "board": board}
        assert kb.schedule_task(conn, waiter, reason="Wait for original owner", awaiting=record,
                                wake_check=f"hermes kanban wait-check task {producer} --board {board} --from blocked")
        assert kb.get_task(conn, waiter).status == "scheduled"
        assert kb.get_task(conn, producer) is None
        event = [e for e in kb.list_events(conn, waiter) if e.kind == "scheduled"][-1]
        assert event.payload["awaiting"]["board"] == board
        assert kaw.card_orphan_reason(conn, waiter) is None
    from hermes_cli import kanban_wait_check as wait
    assert wait.check_local("task", producer, board=board, since=("blocked",))[0] == 1
    with kbc.connect_closing(db_path=boards[target]) as owner:
        after = kb.get_task(owner, producer)
        assert (after.status, after.human_request) == (before.status, before.human_request)


def test_missing_board_is_not_created_or_replaced_by_local_producer(boards):
    with kbc.connect_closing(db_path=boards[0]) as conn:
        waiter = kb.create_task(conn, title="Waiter", assignee="worker")
        producer = kb.create_task(conn, title="Wrong board producer", assignee="worker")
        with pytest.raises(kaw.AwaitingError):
            kb.schedule_task(conn, waiter, awaiting={"kind": "card", "ref": producer, "board": "missing"})
        assert not kb.board_own_db_path("missing").exists()
        assert kb.get_task(conn, waiter).status != "scheduled"


def test_cross_board_cycle_is_refused_and_missing_owner_read_is_safe(boards):
    with kbc.connect_closing(db_path=boards[0]) as left, kbc.connect_closing(db_path=boards[1]) as right:
        a = kb.create_task(left, title="A", assignee="worker")
        b = kb.create_task(right, title="B", assignee="worker")
        assert kb.schedule_task(left, a, awaiting={"kind": "card", "ref": b, "board": "runners"})
        event = [e for e in kb.list_events(left, a) if e.kind == "scheduled"][-1]
        assert event.payload["wake_check"] == f"hermes kanban wait-check task {b} --board runners"
        with pytest.raises(kaw.AwaitingError, match="cycles back"):
            kb.schedule_task(right, b, awaiting={"kind": "card", "ref": a, "board": "default"})
    boards[1].unlink()
    with kbc.connect_closing(db_path=boards[0]) as left:
        assert "unreadable producer board" in kaw.card_orphan_reason(left, a)


def test_finished_producer_is_read_on_its_board(boards):
    from hermes_cli.kanban_wake_check import finished_producer
    with kbc.connect_closing(db_path=boards[0]) as left, kbc.connect_closing(db_path=boards[1]) as right:
        a = kb.create_task(left, title="A", assignee="worker")
        b = kb.create_task(right, title="B", assignee="worker")
        assert kb.schedule_task(left, a, awaiting={"kind": "card", "ref": b, "board": "runners"})
        assert kb.archive_task(right, b)
        hit = finished_producer(left, a, "false")
        assert hit["producer_status"] == "archived"
        assert hit["board"] == "runners"


def test_cross_board_cycle_cannot_close_through_local_edge(boards):
    with kbc.connect_closing(db_path=boards[0]) as left, kbc.connect_closing(db_path=boards[1]) as right:
        a = kb.create_task(left, title="A", assignee="worker")
        b = kb.create_task(left, title="B", assignee="worker")
        c = kb.create_task(right, title="C", assignee="worker")
        assert kb.schedule_task(right, c, awaiting={"kind": "card", "ref": a, "board": "default"})
        assert kb.schedule_task(left, b, awaiting={"kind": "card", "ref": c, "board": "runners"})
        with pytest.raises(kaw.AwaitingError, match="cycles back"):
            kb.schedule_task(left, a, awaiting={"kind": "card", "ref": b})


def test_large_local_fan_in_preserves_bounded_check_behavior(boards):
    with kbc.connect_closing(db_path=boards[0]) as conn:
        parents = [kb.create_task(conn, title=f"Parent {i}", assignee="worker") for i in range(40)]
        producer = kb.create_task(conn, title="Fan-in", assignee="worker", parents=parents)
        waiter = kb.create_task(conn, title="Waiter", assignee="worker")
        assert kb.schedule_task(conn, waiter, awaiting={"kind": "card", "ref": producer})


def test_producer_note_names_board():
    from hermes_cli.kanban_wake_check import producer_note
    note = producer_note({"ref": "t_12345678", "board": "runners", "gateway": None,
                          "producer_status": "archived", "completed_at": "now"})
    assert "on runners" in note
    assert "--board runners" in note
    assert "--gateway <gw>" not in note


def test_same_board_qualifier_keeps_descendant_guard(boards):
    with kbc.connect_closing(db_path=boards[0]) as conn:
        parent = kb.create_task(conn, title="Parent", assignee="worker")
        child = kb.create_task(conn, title="Child", assignee="worker", parents=[parent])
        with pytest.raises(kaw.AwaitingError, match="descendant"):
            kb.schedule_task(conn, parent, awaiting={"kind": "card", "ref": child, "board": "Default"})
