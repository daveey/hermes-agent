"""Exercise actual bounded consumer and durable envelope writer."""
import json
import threading

from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.kanban_fairness_store import load
from hermes_cli.kanban_turn_urgency import read_owners


def activate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return runtime.activate({"kanban": {"fairness_capture": {"enabled": True, "capture_id": "test"}}},
                            home=tmp_path, board="default", source_gateway="gateway", board_uid="board",
                            clock_error_ns=0)


def test_real_reader_writer_and_seal(tmp_path, monkeypatch):
    session = activate(tmp_path, monkeypatch)
    try:
        assert runtime.enabled(board="default")
        assert not runtime.enabled(board="other")
        path = tmp_path / "owners.json"
        path.write_text(json.dumps(dict(version=1, board="default", source_revision="rev",
                                      observed_at=100, valid_until=200, owners=[])))
        assert read_owners(board="default", now_s=150, path=path) == set()
    finally:
        assert session.close()
    loaded = load(session.out)
    assert len(loaded["records"]) == 1
    assert loaded["records"][0]["payload"]["outcome"] == "valid"
    assert loaded["manifest"]["final_seal"]


def test_writer_failure_stays_unsealed(tmp_path, monkeypatch):
    session = activate(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(runtime, "append_records", fail)
    assert runtime.submit(board="default", kind="loss", payload=session.loss_payload("redaction", 1))
    assert not session.close()
    assert session.failed
    assert not (session.out / "manifest.json").exists()


def test_overflow_and_tail_are_bounded(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    actual = runtime.append_records
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)
    monkeypatch.setattr(runtime, "append_records", held)
    session = activate(tmp_path, monkeypatch)
    payload = session.loss_payload("redaction", 1)
    try:
        assert runtime.submit(board="default", kind="loss", payload=payload)
        assert entered.wait(2)
        monkeypatch.setattr(runtime, "MAX_RECORDS", 2)
        assert runtime.submit(board="default", kind="loss", payload=payload)
        assert runtime.submit(board="default", kind="loss", payload=payload)
        assert not runtime.submit(board="default", kind="loss", payload=payload)
        assert session.loss["dropped"] == 1
        assert session.bytes <= runtime.MAX_QUEUE_BYTES
        assert not session.close(timeout=0)
    finally:
        release.set()
        session.thread.join(3)
    assert not session.sealed
    assert not (session.out / "manifest.json").exists()
    records = [json.loads(line) for path in session.out.glob("*.jsonl") for line in path.read_text().splitlines()]
    assert any(r["kind"] == "loss" and r["payload"]["reason"] == "dropped" for r in records)
