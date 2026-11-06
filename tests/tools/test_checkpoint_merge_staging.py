"""Contracts shared by upstream exclusions and fork staging guards."""
import os


import pytest

from tools import checkpoint_manager as cm


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "source.py").write_text("before\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(cm, "CHECKPOINT_BASE", root / "checkpoints")
    monkeypatch.setattr(cm, "selected_git_env", lambda base=None: dict(base if base is not None else os.environ))
    return root


def test_excluded_trees_do_not_exhaust_scan_or_budget(project, monkeypatch):
    monkeypatch.setattr(cm, "_MAX_FILES", 3)
    for directory in ("build", "agent-browser-chrome-test", "run.log", "checkpoints/store"):
        folder = project / directory
        folder.mkdir(parents=True)
        for i in range(5):
            (folder / f"item{i}").write_bytes(b"x" * 4096)
    # Do not initialize a pretend git store: the real store is created below.
    # The scan the take path prices and size-caps from (the a2db0efb sync named it _scan_tree).
    scan = cm._staging_scan(str(project), 1024)
    assert scan.count == 1
    assert scan.oversize == []
    assert set(scan.files) == {"source.py"}
    assert cm.CheckpointManager(enabled=True).ensure_checkpoint(str(project), "snapshot")


@pytest.mark.parametrize("entry", ["checkpoint", "diff", "safe_restore"])
@pytest.mark.parametrize("fallback", [
    pytest.param("newline", marks=pytest.mark.platforms("posix")), "write_failure",
])
def test_oversize_preflight_keeps_store_exclusion(project, monkeypatch, entry, fallback):
    mgr = cm.CheckpointManager(enabled=True, max_file_size_mb=1)
    assert mgr.ensure_checkpoint(str(project), "baseline")
    commit = mgr.list_checkpoints(str(project))[0]["hash"]
    name = "large\nfile.bin" if fallback == "newline" else "large.bin"
    (project / name).write_bytes(os.urandom(2 * 1024 * 1024))
    if fallback == "write_failure":
        monkeypatch.setattr(cm.staging, "write_oversize_excludes", lambda *args: None)
    (project / "source.py").write_text("after\n")
    mgr.record_agent_write(str(project / "source.py"))
    if entry == "checkpoint":
        mgr.new_turn()
        assert mgr.ensure_checkpoint(str(project), "after")
    elif entry == "diff":
        assert mgr.diff(str(project), commit)["success"]
    else:
        assert mgr._safe_restore_plan(str(project), commit)["success"]
    store = cm._store_path()
    # An incompressible oversize blob must never enter even as a dangling object.
    assert sum(p.stat().st_size for p in (store / "objects").rglob("*") if p.is_file()) < 1024 * 1024
    index = cm._index_path(store, cm._project_hash(str(project)))
    ok, paths, _ = cm._run_git(["ls-files", "-z"], store, str(project), index_file=index)
    assert ok
    assert name not in paths.split("\0")
    assert not any(p.startswith("checkpoints/") or p == ".checkpoints.lock" for p in paths.split("\0"))


def test_failed_oversize_removal_aborts_snapshot(project, monkeypatch):
    mgr = cm.CheckpointManager(enabled=True, max_file_size_mb=1)
    (project / "grows.bin").write_bytes(b"small")
    assert mgr.ensure_checkpoint(str(project), "baseline")
    before = mgr.list_checkpoints(str(project))[0]["hash"]
    (project / "grows.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    real_run = cm._run_git

    def fail_remove(args, *a, **kw):
        if args[:2] == ["update-index", "--force-remove"]:
            return False, "", "injected removal failure"
        return real_run(args, *a, **kw)

    monkeypatch.setattr(cm, "_run_git", fail_remove)
    mgr.new_turn()
    assert not mgr.ensure_checkpoint(str(project), "must fail")
    assert mgr.list_checkpoints(str(project))[0]["hash"] == before
