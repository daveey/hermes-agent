"""Custody scratch GC shares the pass budget and the archive-refusal backoff. [daveey]"""
import pytest

from hermes_cli import kanban_scratch_archive as archive
from hermes_cli import kanban_scratch_backoff as bo
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers
from tests.hermes_cli import test_kanban_scratch_custody as custody_tests

env = custody_tests.env
evicted = custody_tests.evicted
remote = custody_tests.remote


@pytest.fixture
def archives(monkeypatch):
    calls = []
    real = archive.archive_workspace

    def spy(workspace, *args, **kwargs):
        calls.append(workspace)
        return real(workspace, *args, **kwargs)

    monkeypatch.setattr(archive, "archive_workspace", spy)
    return calls


def test_custody_refusal_backs_off_then_retries_a_changed_tree(env, remote, archives):
    tid, path, _ = evicted(env)
    helpers._run("clone", "-q", "--bare", str(remote), str(path / "bare"))
    assert gc.gc_task_scratch(env.conn, older_than_days=7) == 0
    assert gc.gc_task_scratch(env.conn, older_than_days=7) == 0
    assert archives == [path], "refused custody tree re-archived inside its backoff"
    assert bo.Refusals("default").entries[f"custody:{tid}"]["count"] == 1
    import shutil
    shutil.rmtree(path / "bare")
    helpers._run("clone", "-q", str(remote), str(path / "repo"))
    assert gc.gc_task_scratch(env.conn, older_than_days=7) == 1
    assert archives == [path, path] and not path.exists()
    assert f"custody:{tid}" not in bo.Refusals("default").entries


def test_spent_budget_skips_custody_before_any_board_read(env, caplog):
    _tid, path, _ = evicted(env)
    budget = bo.Budget(1, clock=iter([0.0, 5.0, 5.0]).__next__)
    assert gc.gc_task_scratch(env.conn, older_than_days=7, budget=budget) == 0
    assert env.calls == 0 and path.exists()
    assert budget.deferred == 1 and "deferred 1" in caplog.text
    assert gc.gc_task_scratch(env.conn, older_than_days=7) == 1
    assert not path.exists()
