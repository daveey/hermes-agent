"""Legacy idempotency replay stays atomic across concurrent creators."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_origin_admission as admission


@pytest.mark.parametrize("same_key", [True, False, None])
def test_legacy_recheck_after_concurrent_early_miss(tmp_path, monkeypatch, same_key):
    db = tmp_path / "board.db"
    kb.init_db(db)
    barrier = threading.Barrier(2)
    original = admission.early_existing

    def early(*args):
        result = original(*args)
        assert result is None
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(admission, "early_existing", early)

    def create(index):
        with kbc.connect_closing(db) as conn:
            key = None if same_key is None else "same-key" if same_key else f"key-{index}"
            return kb.create_task(conn, title="isolated race fixture", idempotency_key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    expected = 1 if same_key else 2
    assert len(set(ids)) == expected, ids
    with kbc.connect_closing(db) as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == expected
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='created'").fetchone()[0] == expected


def test_archived_legacy_key_allows_new_card(tmp_path):
    with kbc.connect_closing(tmp_path / "board.db") as conn:
        original = kb.create_task(conn, title="old", idempotency_key="reusable")
        assert kb.archive_task(conn, original)
        replacement = kb.create_task(conn, title="new", idempotency_key="reusable")
        assert replacement != original
        assert kb.create_task(conn, title="retry", idempotency_key="reusable") == replacement
        assert kb.get_task(conn, original).status == "archived"
