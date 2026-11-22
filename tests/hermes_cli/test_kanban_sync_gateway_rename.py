"""A Board gateway renamed in place: this gateway's own cards stay its own on the sync path. [daveey]

The Board's rename op rewrites ``tasks.created_on`` from the old name to the new one and re-emits each row on
the feed. The gateway then syncs under its new name while its live file still says ``created_on = <old>``
(t_01a204733c506aa0; plan review F10, re-review R7 e).

* a normal pull adopts the re-emitted row onto the existing local row (``kanban_db_sync._upsert_task``), and
  ``own_partition`` treats the card as own afterwards;
* the first full round under the new name, run while every local row still carries the old name, queues and
  sends nothing, deletes nothing, and leaves the live file agreeing with the Board.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

OLD, NEW, OTHER, BOARD = "mini", "daveey-mini-1", "hub", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, OLD)
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


def _round(kanban_home, svc, gateway):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(gateway), gateway)


def _settle(kanban_home, svc, gateway) -> None:
    for _ in range(4):  # push, then pull the echo back into the replica; a settled board repeats cleanly
        _round(kanban_home, svc, gateway)


def _created_on(conn) -> dict:
    return dict(conn.execute("SELECT id, created_on FROM tasks ORDER BY id").fetchall())


def board_rename(svc, old: str, new: str, *, writer: str) -> list[str]:
    """The Board's in-place rename of ``old``: every card it created is rewritten to ``created_on = new`` at a
    new version and re-emitted on the feed (``writer`` = the change's origin_gateway)."""
    b = svc.board(BOARD)
    moved = [uid for uid, row in b["rows"]["tasks"].items() if row.get("created_on") == old]
    for i, uid in enumerate(moved):
        svc._put(b, "tasks", uid, {"created_on": new}, writer, None, f"9999999999999-{i:04d}-board")
    return moved


def _ops_sent(svc) -> int:
    return sum(len(call[2]) for call in svc.calls if call[0] == "ops")


@pytest.fixture
def renamed(live, kanban_home, svc):
    """Two own cards (one with a comment) and one of another gateway's, settled; then the Board renames us."""
    own = [kb.create_task(live, title="own a", assignee="worker"), kb.create_task(live, title="own b", assignee="worker")]
    kb.add_comment(live, own[0], "me", "a note")
    other = kbc.connect(kanban_home / "other.db")
    try:
        ksc.ensure_identity(other, BOARD, OTHER)
        foreign = kb.create_task(other, title="theirs", assignee="worker")
        ksc.push(other, svc.hub(OTHER), BOARD, OTHER, primary=False)
    finally:
        other.close()
    _settle(kanban_home, svc, OLD)
    assert _created_on(live) == {own[0]: OLD, own[1]: OLD}
    assert not ksc.status(live)["pending"]
    return own, foreign


@pytest.mark.parametrize("writer", [OTHER, NEW, OLD])
def test_first_round_under_the_new_name_adopts_the_renamed_cards_and_sends_nothing(
        renamed, live, kanban_home, svc, writer):
    own, foreign = renamed
    assert sorted(board_rename(svc, OLD, NEW, writer=writer)) == sorted(own)
    head, sent = svc.board(BOARD)["seq"], _ops_sent(svc)

    result = _round(kanban_home, svc, NEW)   # local rows still say created_on = mini when this round starts

    assert _created_on(live) == {own[0]: NEW, own[1]: NEW}          # adopted in place, the foreign card not pulled in
    assert _ops_sent(svc) == sent and svc.board(BOARD)["seq"] == head, "the rename round must not write to the Board"
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state != 'acked'").fetchall()
    assert not live.execute("SELECT 1 FROM kanban_sync_tombstones").fetchall()
    assert kb.get_task(live, own[0]).title == "own a"
    assert [c.body for c in kb.list_comments(live, own[0])] == ["a note"]
    assert result.error is None and (result.push is None or result.push.rejected == 0), result
    assert sync.sync_state(live)["gateway_id"] == NEW

    _settle(kanban_home, svc, NEW)                                   # and it stays quiet
    assert _ops_sent(svc) == sent and svc.board(BOARD)["seq"] == head
    with kbc.connect(ksc.shadow_path(BOARD)) as shadow:
        report = ksc.diff(live, shadow, NEW)
    assert report["clean"] and report["checked"] == 2 and report["foreign_cards"] == 1, report
    assert foreign not in _created_on(live)


def test_a_normal_pull_adopts_the_re_emitted_row_and_own_partition_keeps_the_card(renamed, live, kanban_home, svc):
    """F10, at the two functions it names: ``pull(partition=<new>)`` applies the re-emitted ``tasks`` row onto
    the existing local row, and later changes to that card pass ``own_partition`` under the new name."""
    own, foreign = renamed
    board_rename(svc, OLD, NEW, writer=OTHER)
    ksc.ensure_identity(live, BOARD, NEW)

    res = ksc.pull(live, svc.hub(NEW), BOARD, observers=True, partition=NEW)

    assert not res.more and _created_on(live) == {own[0]: NEW, own[1]: NEW}
    b = svc.board(BOARD)
    svc._put(b, "tasks", own[0], {"title": "own a, edited on the Board"}, OTHER, None, "9999999999999-9000-board")
    svc._put(b, "tasks", foreign, {"title": "theirs, edited"}, OTHER, None, "9999999999999-9001-board")
    tail = [ksc.adapt(c) for c in b["changes"][-2:]]
    assert [c.uid for c in ksc.own_partition(live, tail, NEW)] == [own[0]]
    ksc.pull(live, svc.hub(NEW), BOARD, observers=True, partition=NEW)
    assert kb.get_task(live, own[0]).title == "own a, edited on the Board" and foreign not in _created_on(live)


def test_a_card_created_under_the_new_name_is_pushed_as_the_new_name(renamed, live, kanban_home, svc):
    own, _foreign = renamed
    board_rename(svc, OLD, NEW, writer=OTHER)
    _settle(kanban_home, svc, NEW)
    tid = kb.create_task(live, title="after the rename", assignee="worker")
    _settle(kanban_home, svc, NEW)
    assert svc.board(BOARD)["rows"]["tasks"][tid]["created_on"] == NEW
    assert _created_on(live) == {own[0]: NEW, own[1]: NEW, tid: NEW}
