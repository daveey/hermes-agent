"""A concurrent runner must not reap a live test's deliberately deleted cwd."""
import os
import shutil
import signal
from unittest.mock import Mock

import psutil
import pytest

from scripts import run_tests_parallel as runner
from scripts import run_tests_host_slots as slots
from tests.scripts import test_run_tests_host_slots as holders
import hermes_constants_scratch as scratch

slot_env = holders.slot_env
pytestmark = pytest.mark.platforms("linux")


@pytest.mark.parametrize("mode", ["fresh-runner", "aged-runner", "default-pruner"])
def test_runner_sweep_deleted_cwd_scope(slot_env, monkeypatch, tmp_path, mode):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    # Match conftest's parent policy isolation in the fresh holder interpreter.
    monkeypatch.setenv("HERMES_TEST_POLICY_ROOT", str(tmp_path / "hermes_test"))
    tree = tmp_path / "attempt" / "worktree"
    tree.mkdir(parents=True)
    child, _ = holders._spawn_holder(tree)
    proc = psutil.Process(child.pid)
    # Never inventory or signal another test's or the host's processes.
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter([proc]))
    monkeypatch.setattr(scratch, "_registered_live_pids", lambda home: set())
    try:
        shutil.rmtree(tree)
        if mode == "aged-runner":
            os.utime(tree.parent, (1, 1))
        if mode == "default-pruner":
            scratch.prune_idle_entries(tmp_path, 24, frozenset())
        else:
            runner._sweep_killed_run_roots(str(tmp_path))
        if mode != "fresh-runner":
            # psutil.wait_procs reaps this child, so Popen no longer owns its status.
            assert proc.returncode == -signal.SIGTERM
            assert tree.parent.exists() is (mode == "default-pruner")
            return
        assert child.poll() is None, (
            f"startup sweep killed a live attempt's holder: {getattr(proc, 'returncode', None)}"
        )
        notes = slots.reclaim_stale_slots(str(slot_env), 1)
        assert notes, "the slot reclaimer still owns deleted-tree recovery"
        assert child.wait(30) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(30)


def test_runner_scope_keeps_deleted_child_when_idle_entry_is_rescued(monkeypatch, tmp_path):
    entry = tmp_path / "attempt"
    entry.mkdir()
    keeper, child = Mock(pid=10001), Mock(pid=10002)
    for proc in (keeper, child):
        proc.uids.return_value.real = os.getuid()
    keeper.cwd.return_value = str(entry)
    child.cwd.return_value = str(entry / "deleted") + " (deleted)"
    monkeypatch.setattr(psutil, "process_iter", lambda *args: iter([keeper, child]))
    monkeypatch.setattr(scratch, "_own_lineage", lambda: set())
    monkeypatch.setattr(scratch, "_registered_live_pids", lambda home: set())
    monkeypatch.setattr(scratch, "_owned_by_hermes", lambda proc, registered: proc is keeper)
    doomed = [entry]
    assert scratch.reap_processes_rooted_in(tmp_path, doomed, reap_deleted_cwds=False) == 0
    assert doomed == []
    keeper.terminate.assert_not_called()
    child.terminate.assert_not_called()
