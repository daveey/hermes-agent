"""Host bystanders must not affect scratch cleanup in another pytest session."""
from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os
import subprocess
import sys
from types import SimpleNamespace

import psutil
import pytest

from tests import conftest as suite_conftest
from tests._fixtures.process_census import session_process_iter


@pytest.fixture
def foreign_deleted_cwd(tmp_path_factory, monkeypatch):
    """Replay psutil's real deleted-cwd stat against a guarded foreign home.

    Only the kernel readlink result is injected: the process exists (pytest's
    parent), and psutil's cwd/readlink/path_exists_strict and the I/O guard run
    unchanged. Never modify or signal the actual bystander.
    """
    foreign = tmp_path_factory.getbasetemp().with_name("foreign-home")
    poison = str(foreign / "pytest-of-user" / "garbage-dead" / "test-child") + " (deleted)"
    monkeypatch.setattr(suite_conftest, "_REAL_HERMES_ROOT_CANDIDATES",
                        [*suite_conftest._REAL_HERMES_ROOT_CANDIDATES, foreign])
    real_readlink = os.readlink
    bystander = os.getppid()

    def readlink(path, *args, **kwargs):
        if os.fspath(path) == f"/proc/{bystander}/cwd":
            return poison
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink)
    return bystander


def test_owned_job_scan_ignores_foreign_deleted_cwd(tmp_path, foreign_deleted_cwd):
    from hermes_cli.kanban_workspace_jobs import defer_owned_job_cleanup

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert not defer_owned_job_cleanup("t_0123456789abcdef", workspace)


def test_scratch_prune_ignores_foreign_deleted_cwd(tmp_path, foreign_deleted_cwd):
    from hermes_constants_scratch import prune_idle_entries

    root = tmp_path / "scratch"
    idle = root / "probe"
    idle.mkdir(parents=True)
    assert prune_idle_entries(root, 0, frozenset()) == 1
    assert not idle.exists()


def test_process_census_keeps_current_test_process(tmp_path):
    from pathlib import Path

    assert not Path.cwd().resolve().is_relative_to(tmp_path.resolve())
    assert os.getpid() in {proc.pid for proc in session_process_iter((str(tmp_path),))}


def test_process_census_keeps_real_child_with_requested_attributes(tmp_path):
    with subprocess.Popen([sys.executable, "-c", "input()"], cwd=tmp_path,
                          stdin=subprocess.PIPE, text=True) as child:
        try:
            found = {proc.pid: proc.info for proc in session_process_iter(
                (str(tmp_path),), ["pid", "cwd"])}
            assert child.pid in found
            assert found[child.pid]["cwd"] == str(tmp_path)
            assert os.getppid() not in found
        finally:
            child.communicate("done\n", timeout=10)


def test_cleanup_census_keeps_descendant_outside_session_roots(tmp_path, tmp_path_factory, monkeypatch):
    from pathlib import Path

    from hermes_cli import kanban_workspace_jobs as jobs

    cwd = Path(__file__).resolve().parent
    roots = [tmp_path_factory.getbasetemp(), Path(os.environ["HERMES_TEST_SANDBOX_HOME"])]
    assert all(not cwd.is_relative_to(root.resolve()) for root in roots)
    seen = set()

    def observe(proc, *args):
        seen.add(proc.pid)
        return False

    monkeypatch.setattr(jobs, "_holds_workspace", observe)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with subprocess.Popen([sys.executable, "-c", "input()"], cwd=cwd,
                          stdin=subprocess.PIPE, text=True) as child:
        try:
            assert not jobs.defer_owned_job_cleanup("t_0123456789abcdef", workspace)
            assert child.pid in seen
        finally:
            child.communicate("done\n", timeout=10)


def test_census_keeps_real_cwd_holder_without_ancestry(tmp_path, monkeypatch):
    from tests._fixtures import process_census as census

    # Model a lost/reparented ancestry snapshot, retaining real process/cwd I/O.
    monkeypatch.setattr(census, "_PROCESS", lambda pid: SimpleNamespace(
        pid=pid, children=lambda recursive: []))
    with subprocess.Popen([sys.executable, "-c", "input()"], cwd=tmp_path,
                          stdin=subprocess.PIPE, text=True) as child:
        try:
            assert child.pid in {proc.pid for proc in session_process_iter((str(tmp_path),))}
        finally:
            child.communicate("done\n", timeout=10)


@pytest.mark.parametrize("suffix,expected", [("", True), (" (deleted)", True), ("-sibling", False)])
def test_root_filter_deleted_cwd_and_path_boundary(tmp_path, monkeypatch, suffix, expected):
    from tests._fixtures import process_census as census

    cwd = str(tmp_path) + suffix
    # Exercise both procfs and portable fallback without touching any real pid.
    real_isdir, real_readlink = census.os.path.isdir, census.os.readlink
    monkeypatch.setattr(census.os.path, "isdir", lambda path: True if path == "/proc/self" else real_isdir(path))
    monkeypatch.setattr(census.os, "readlink", lambda path, *a, **kw:
                        cwd if path == "/proc/12345/cwd" else real_readlink(path, *a, **kw))
    assert census._rooted_in_session(SimpleNamespace(pid=12345), (str(tmp_path),)) is expected


def test_foreign_attributes_are_not_requested(monkeypatch, tmp_path):
    from tests._fixtures import process_census as census

    touched = []
    foreign = SimpleNamespace(pid=os.getppid(), as_dict=lambda **kw: touched.append(kw))
    monkeypatch.setattr(census, "_PROCESS_ITER", lambda: iter([foreign]))
    monkeypatch.setattr(census, "_rooted_in_session", lambda proc, roots: False)
    assert list(session_process_iter((str(tmp_path),), ["cwd", "environ"])) == []
    assert touched == []


def test_synthetic_process_census_override_still_wins(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter([sentinel]))
    assert list(psutil.process_iter()) == [sentinel]
