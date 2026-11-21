"""Root loading must not silently omit runtime producers or their lost tails."""
import json
import shutil
import tarfile
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli import kanban_fairness_store as store
from scripts import kanban_fairness as cli


def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = runtime.activate(
        {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "root-test"}}},
        home=tmp_path, board="default", source_gateway="gateway", board_uid="board", clock_error_ns=0)
    assert session is not None
    assert session.enqueue("loss", session.loss_payload("redaction", 1))
    assert session.close()
    return session, session.out.parent.parent


def test_root_collects_runtime_and_preserves_envelopes(tmp_path, monkeypatch):
    session, root = capture(tmp_path, monkeypatch)
    result = store.load(root)
    assert result["records"] == store.load(session.out)["records"]
    inventory = json.loads((root / "runtime-epochs.json").read_text())
    assert inventory["epochs"] == [session.identity]


def test_deleted_epoch_remains_expected(tmp_path, monkeypatch):
    session, root = capture(tmp_path, monkeypatch)
    shutil.rmtree(session.out)
    assert "runtime_epoch_missing" in {g["code"] for g in store.load(root)["gaps"]}


def test_unregistered_epoch_is_not_ignored(tmp_path, monkeypatch):
    session, root = capture(tmp_path, monkeypatch)
    (root / "runtime-epochs.json").unlink(missing_ok=True)
    result = store.load(root)
    assert result["records"] == store.load(session.out)["records"]
    assert "runtime_inventory" in {g["code"] for g in result["gaps"]}


def test_export_retains_nested_orphan_and_temporary_evidence(tmp_path, monkeypatch):
    session, root = capture(tmp_path, monkeypatch)
    (session.out / "orphan.tmp").write_bytes(b"bounded orphan")
    empty = root / "runtime" / ("c" * 32)
    empty.mkdir()
    assert "capture_orphan" in {g["code"] for g in store.load(root)["gaps"]}
    target = tmp_path / "capture.tar.gz"
    assert cli._export(SimpleNamespace(input=str(root), output=str(target))) == 0
    with tarfile.open(target) as bundle:
        names = bundle.getnames()
    assert f"{root.name}/runtime/{session.epoch}/orphan.tmp" in names
    assert f"{root.name}/runtime/{session.epoch}/manifest.json" in names
    assert f"{root.name}/runtime/{empty.name}" in names


def test_export_refuses_symlinks_and_self_output(tmp_path, monkeypatch):
    _, root = capture(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        cli._export(SimpleNamespace(input=str(root), output=str(root / "bundle.tar.gz")))
    (tmp_path / "elsewhere").write_bytes(b"must not export")
    (root / "escape").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        cli._export(SimpleNamespace(input=str(root), output=str(tmp_path / "bundle.tar.gz")))


def test_inventory_survives_deleted_epoch_and_missing_budget(tmp_path, monkeypatch):
    session, root = capture(tmp_path, monkeypatch)
    shutil.rmtree(session.out)
    (root / "capture-budget.json").unlink()
    assert "runtime_epoch_missing" in {g["code"] for g in store.load(root)["gaps"]}


def test_deleted_runtime_tree_and_inventory_leave_gap(tmp_path, monkeypatch):
    _, root = capture(tmp_path, monkeypatch)
    shutil.rmtree(root / "runtime")
    (root / "runtime-epochs.json").unlink()
    assert "runtime_inventory" in {g["code"] for g in store.load(root)["gaps"]}


def test_empty_unregistered_epoch_leaves_gap(tmp_path, monkeypatch):
    _, root = capture(tmp_path, monkeypatch)
    (root / "runtime" / ("a" * 32)).mkdir()
    assert "runtime_inventory" in {g["code"] for g in store.load(root)["gaps"]}


def test_scan_precedes_read_and_bounds_export(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_root as assembly

    _, root = capture(tmp_path, monkeypatch)
    monkeypatch.setattr(assembly, "MAX_ASSEMBLED", 10)
    monkeypatch.setattr(store, "_load_one", lambda *a: pytest.fail("read before bound"))
    assert store.load(root)["gaps"] == [{"code": "capture_bound"}]
    target = tmp_path / "oversize.tar.gz"
    with pytest.raises(ValueError):
        assembly.export(root, target)
    assert not target.exists()
    assert not list(tmp_path.glob("oversize.tar.gz.*.tmp"))


def test_registration_precedes_epoch_directory_and_thread(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_root as assembly

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = assembly.register
    def fail_after_registration(root, identity):
        original(root, identity)
        raise OSError("simulated crash before directory")
    monkeypatch.setattr(assembly, "register", fail_after_registration)
    with pytest.raises(OSError):
        runtime.activate({"kanban": {"fairness_capture": {"enabled": True, "capture_id": "crash"}}},
                         home=tmp_path, board="default", source_gateway="gateway", board_uid="board")
    root = tmp_path / "kanban/evidence/crash"
    assert "runtime_epoch_missing" in {g["code"] for g in store.load(root)["gaps"]}


def test_arm_ceiling_applies_before_database_activation(tmp_path, monkeypatch):
    from hermes_cli import config
    from hermes_cli import kanban_fairness_budget as budget

    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"fairness_capture": {"max_bytes": 64}}})
    monkeypatch.setattr(cli, "_connect", lambda *a: pytest.fail("database activated before plan budget"))
    args = SimpleNamespace(out=str(tmp_path / "small"), db="unused", board="default", replica=False,
                           replace=False, capture_id="small", clock_error_s=0, apply_lag_s=0,
                           unclaimed_exit="unknown", writer_clocks=False)
    with pytest.raises(budget.BudgetExceeded):
        cli._arm(args)
    assert budget._limit(tmp_path / "small") == 64


def test_export_failure_does_not_publish_or_replace(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_root as assembly

    _, root = capture(tmp_path, monkeypatch)
    target = tmp_path / "result.tar.gz"
    def fail(source, paths, stream):
        stream.write(b"partial")
        raise OSError("disk failure")
    monkeypatch.setattr(assembly, "_archive", fail)
    with pytest.raises(OSError):
        assembly.export(root, target)
    assert not target.exists()
    assert not list(tmp_path.glob("result.tar.gz.*.tmp"))
    target.write_bytes(b"original")
    with pytest.raises(ValueError):
        assembly.export(root, target)
    assert target.read_bytes() == b"original"


def test_fifo_refused_before_loader(tmp_path, monkeypatch):
    import os

    _, root = capture(tmp_path, monkeypatch)
    os.mkfifo(root / "manifest.json")
    monkeypatch.setattr(store, "_load_one", lambda *a: pytest.fail("FIFO was read"))
    assert store.load(root)["gaps"] == [{"code": "capture_bound"}]


def test_missing_inventory_cannot_be_recreated(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_root as assembly

    session, root = capture(tmp_path, monkeypatch)
    (root / assembly.INVENTORY).unlink()
    with pytest.raises(ValueError, match="inventory missing"):
        assembly.register(root, {**session.identity, "source_epoch": "b" * 32})
    assert not (root / assembly.INVENTORY).exists()


def _register_process(root, epoch, barrier):
    from hermes_cli import kanban_fairness_root as assembly

    barrier.wait(timeout=15)
    assembly.register(root, dict(capture_id="parallel", source_gateway="gateway", board_uid="board",
                                 source_epoch=epoch, clock_error_ns=None))


def test_concurrent_registration_keeps_every_expected_epoch(tmp_path):
    import multiprocessing
    from hermes_cli import kanban_fairness_budget as budget
    from hermes_cli import kanban_fairness_root as assembly

    root = tmp_path / "parallel"
    budget.initialize(root)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    epochs = ["a" * 32, "b" * 32]
    processes = [context.Process(target=_register_process, args=(root, epoch, barrier)) for epoch in epochs]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=15)
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
        assert set(assembly.inventory(root)) == set(epochs)
        assert "runtime_epoch_missing" in {g["code"] for g in store.load(root)["gaps"]}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(3)
