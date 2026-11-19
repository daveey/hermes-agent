"""Mutation-killing clauses: writer-final custody, export-vs-arm, arm budget binding."""
import json

import pytest

from hermes_cli import kanban_fairness_activation as activation
from hermes_cli import kanban_fairness_budget as budget
from hermes_cli import kanban_fairness_root as assembly
from hermes_cli import kanban_fairness_runtime as runtime


def cfg(tmp_path):
    return {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "fixture",
                                           "directory": str(tmp_path / "capture")}}}


def _stop_writer_without_begin_close(session, **custody):
    """Stop the writer directly so only its own final check can observe custody."""
    if "producers" in custody:
        session.producers[object()] = True
    if "readers" in custody:
        session.readers = 1
    session.stopping.set()
    session.wake.set()
    session.thread.join(5)
    assert not session.thread.is_alive()


@pytest.mark.parametrize("custody", ["producers", "readers"])
def test_writer_final_abandons_when_custody_remains(tmp_path, monkeypatch, custody):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = runtime.activate(cfg(tmp_path), home=tmp_path, board="default",
                               source_gateway="gw", board_uid="board", clock_error_ns=0)
    assert session is not None
    try:
        _stop_writer_without_begin_close(session, **{custody: True})
        assert session.abandoned.is_set()
    finally:
        session.producers.clear()
        session.readers = 0
        session.close()


def test_writer_final_without_custody_is_not_abandoned(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = runtime.activate(cfg(tmp_path), home=tmp_path, board="default",
                               source_gateway="gw", board_uid="board", clock_error_ns=0)
    assert session is not None
    try:
        _stop_writer_without_begin_close(session)
        assert not session.abandoned.is_set()
    finally:
        session.close()


def test_export_refused_while_runtime_arm_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = runtime.activate(cfg(tmp_path), home=tmp_path, board="default",
                               source_gateway="gw", board_uid="board", clock_error_ns=0)
    assert session.enqueue("loss", session.loss_payload("redaction", 1))
    assert session.close()
    root = session.out.parent.parent
    target = tmp_path / "out.tar.gz"
    (root / "runtime-arm.json").write_text("{}")
    with pytest.raises(ValueError, match="disarm runtime capture before export"):
        assembly.export(root, target)
    assert not target.exists()
    assert not list(tmp_path.glob("out.tar.gz.*.tmp"))
    (root / "runtime-arm.json").unlink()
    assembly.export(root, target)
    assert target.exists()


def test_start_rejects_budget_limit_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = cfg(tmp_path)
    activation.arm(config, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    directory = tmp_path / "capture"
    meta = directory / budget.NAME
    meta.write_text(json.dumps({"version": 1, "max_bytes": budget._limit(directory) - 1}))
    monkeypatch.setattr(runtime, "activate", lambda *a, **kw: pytest.fail("must not activate"))
    with pytest.raises(ValueError, match="runtime arm budget mismatch"):
        activation.start(config, home=tmp_path)
    assert not (directory / "runtime").exists()
