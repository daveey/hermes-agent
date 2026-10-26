"""A failed board must not prevent dispatch or health checks on its peers."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings


@pytest.fixture
def boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    for slug in ("bad", "good"):
        kb.create_board(slug)
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["bad", "good"])
    return dispatcher


def test_corrupt_board_does_not_abort_gateway_iteration(boards, monkeypatch):
    path = kb.board_dir("bad") / "kanban.db"
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    path.write_bytes(header.ljust(100, b"\x00") + b"invalid sqlite page" * 128)
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    dispatched = []
    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: dispatched.append(kw["board"]))
    monkeypatch.setattr(kbd, "has_spawnable_ready", lambda conn: True)
    assert boards.auto_decompose_tick(3) == 0
    boards.tick_once()
    assert dispatched == ["good"]
    assert boards.ready_nonempty()


@pytest.mark.parametrize("operation", ["health", "decomposition"])
def test_board_body_failure_is_contained_and_lock_released(boards, monkeypatch, operation):
    seen = []

    def fail_bad(conn):
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path == str(kb.board_dir("bad") / "kanban.db"):
            raise LookupError("board-local failure")
        seen.append("good")
        return True

    if operation == "health":
        monkeypatch.setattr(kbd, "has_spawnable_ready", fail_bad)
        assert boards.ready_nonempty()
    else:
        def decompose(slug, budget):
            if slug == "bad":
                raise LookupError("board-local failure")
            seen.append(slug)
            return 1, 1
        monkeypatch.setattr(boards, "_decompose_board", lambda module, slug, budget: decompose(slug, budget))
        assert boards.auto_decompose_tick(3) == 1
    assert seen == ["good"]
    with kbc._dispatch_tick_lock((kb.board_dir("bad") / "kanban.db").resolve()) as held:
        assert held
