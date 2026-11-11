"""Health thresholds are independent; a protected floor is not failed maintenance."""
import json
import logging
import time

import pytest

from tools import checkpoint_maintenance as maintenance
from tools import checkpoint_manager as checkpoints
from tests.tools.test_checkpoint_size_pending import _over_cap_store


@pytest.mark.parametrize("cap,age", [(0.5, 1), (1, 72)])
def test_health_independent_thresholds(tmp_path, monkeypatch, caplog, cap, age):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    (base / ".last_prune").write_text(str(time.time()), encoding="utf-8")
    (base / ".last_prune_success").write_text(str(time.time() - age * 3600), encoding="utf-8")
    caplog.set_level(logging.ERROR)
    assert maintenance.check_store_health(base, cap) is True
    assert "checkpoint store over cap:" in caplog.text


def test_protected_floor_is_success(tmp_path, monkeypatch, caplog):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 2)
    caplog.set_level(logging.INFO)
    out = maintenance.prune_checkpoints(retention_days=0, delete_orphans=False,
                                        checkpoint_base=base, max_total_size_mb=0.5)
    assert out["errors"] == 0
    assert out["protected_floor"] is True
    assert (base / ".last_prune_success").exists()
    (base / ".last_prune_success").write_text("1", encoding="utf-8")
    assert maintenance.check_store_health(base, 0.5) is False
    assert "protected floor" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # A subsequent real checkpoint invalidates the floor observation.
    manager = checkpoints.CheckpointManager(enabled=True, max_total_size_mb=0)
    work = tmp_path / "project-000"
    (work / "note.txt").write_text("new history", encoding="utf-8")
    assert manager.ensure_checkpoint(str(work), "new")
    (base / ".last_health_check").unlink()
    assert maintenance.check_store_health(base, 0.5) is True


def test_failed_prune_does_not_refresh_success(tmp_path, monkeypatch):
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    success = base / ".last_prune_success"
    success.write_text("1", encoding="utf-8")
    # Explicitly exercise a failing maintenance return.
    monkeypatch.setattr(maintenance, "_prune_checkpoints", lambda *args: {"errors": 1})
    result = maintenance.prune_checkpoints(checkpoint_base=base)
    assert result["errors"] == 1
    assert success.read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize("fault", ["pending", "refs_failed", "empty", "foreign", "parents", "parents_failed"])
def test_floor_record_fails_closed(tmp_path, monkeypatch, fault):
    from tools import checkpoint_health as health
    store = tmp_path / "store"
    store.mkdir()
    if fault == "pending":
        (store / ".gc-pending").touch()
    def git(args, *rest):
        if args[0] == "for-each-ref":
            refs = "" if fault == "empty" else (
                "refs/heads/foreign abc" if fault == "foreign" else checkpoints._REFS_PREFIX + "/project abc")
            return fault != "refs_failed", refs, ""
        return fault != "parents_failed", "abc" if fault == "parents" else "", ""
    monkeypatch.setattr(health, "_run_git", git)
    assert health.record_floor(store) is False
    assert not (tmp_path / health.FLOOR_MARKER).exists()


@pytest.mark.parametrize("fault", ["pending", "missing", "corrupt", "array", "size", "refs", "git_failure"])
def test_floor_receipt_invalidated(tmp_path, monkeypatch, fault):
    from tools import checkpoint_health as health
    store = tmp_path / "store"
    store.mkdir()
    refs = checkpoints._REFS_PREFIX + "/project abc"
    receipt = {"refs": "different" if fault == "refs" else refs,
               "size": 11 if fault == "size" else 10}
    marker = tmp_path / health.FLOOR_MARKER
    if fault != "missing":
        marker.write_text("bad" if fault == "corrupt" else "[]" if fault == "array" else json.dumps(receipt),
                          encoding="utf-8")
    if fault == "pending":
        (store / ".gc-pending").touch()
    monkeypatch.setattr(health, "_run_git", lambda *args: (fault != "git_failure", refs, ""))
    assert health.protected_floor(store, 10) is False


@pytest.mark.parametrize("ref", ["refs/hermes-foreign/unrelated", "refs/hermesproject"])
def test_near_prefix_foreign_refs_rejected(tmp_path, monkeypatch, ref):
    from tools import checkpoint_health as health
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    store = base / "store"
    ok, refs, _ = checkpoints._run_git(["for-each-ref", "--format=%(objectname)"], store, str(tmp_path))
    assert ok
    assert checkpoints._run_git(["update-ref", ref, refs.splitlines()[0]], store, str(tmp_path))[0]
    assert health.record_floor(store) is False
    # Even a legacy receipt must fail closed during revalidation.
    ok, listing, _ = checkpoints._run_git(["for-each-ref", "--format=%(refname) %(objectname)"], store, str(tmp_path))
    assert ok
    size = checkpoints._dir_size_bytes(store)
    (base / health.FLOOR_MARKER).write_text(json.dumps({"refs": listing, "size": size}))
    assert health.protected_floor(store, size) is False


def test_busy_store_defers_health_observation(tmp_path, monkeypatch, caplog):
    from tools.checkpoint_pruning import store_lock
    base, _ = _over_cap_store(tmp_path, monkeypatch, 1)
    caplog.set_level(logging.ERROR)
    with store_lock(base):
        assert maintenance.check_store_health(base, 0.5) is False
    assert not (base / ".last_health_check").exists()
    assert not caplog.records
    assert maintenance.check_store_health(base, 0.5) is True
