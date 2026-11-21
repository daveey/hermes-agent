"""Cached green observations cannot cross head, publication, or database identities."""
from concurrent.futures import Future

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_pr_idle_checks as checks
from hermes_cli import kanban_pr_idle_green as idle
from tests.hermes_cli import test_kanban_active_pr_idle_green as helpers

kanban_home = helpers.kanban_home
green = helpers.green
pytestmark = pytest.mark.usefixtures("kanban_home")


@pytest.mark.parametrize("error", [AttributeError, LookupError, AssertionError, ImportError])
def test_failed_future_is_removed_and_other_cards_keep_running(green, error):
    failed = Future()
    failed.set_exception(error("must never escape the observer"))
    checks._pending[("broken",)] = (checks.time.monotonic(), failed)
    for _ in range(3):
        assert not checks.green(("broken",), helpers.URL, "a", helpers.SHA)
        assert ("broken",) not in checks._pending
        assert checks.green(("healthy",), helpers.URL, "a", helpers.SHA)


@pytest.mark.parametrize("component", ["head", "identity", "database"])
def test_cached_green_is_not_reused_after_identity_changes(green, monkeypatch, component):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        states = {helpers.URL: "OPEN"}
        assert idle.releases(conn, tid, states)
        green[0]["ok"] = False
        if component == "head":
            monkeypatch.setattr(prs, "pr_head", lambda *a, **kw: "b" * 40)
        elif component == "identity":
            conn.execute("UPDATE tasks SET assignee='b' WHERE id=?", (tid,))
            conn.commit()
        else:
            # Same task and publication on a distinct board: only PRAGMA differs.
            class OtherBoard:
                def execute(self, sql, *args):
                    if sql == "PRAGMA database_list":
                        return [(0, "main", "/other/board.db")]
                    return conn.execute(sql, *args)
            assert not idle.releases(OtherBoard(), tid, states)
            assert len(green[1]) == 2
            return
        assert not idle.releases(conn, tid, states)
        assert len(green[1]) == 2


def test_pr_cache_clear_invalidates_cached_green_and_pending(green):
    key = ("cached",)
    assert checks.green(key, helpers.URL, "a", helpers.SHA)
    pending = Future()
    checks._pending[("pending",)] = (checks.time.monotonic(), pending)
    green[0]["ok"] = False
    prs.clear_cache()
    assert not checks._cache and not checks._pending
    assert pending.cancelled()
    assert not checks.green(key, helpers.URL, "a", helpers.SHA)


def test_operator_can_lower_active_pr_cap(monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "900")
    assert dispatch.guard_hold_cap_seconds("active_pr") == 900
