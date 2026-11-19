"""Real envelope writer/reader continuity and integrity regressions."""
import json

import pytest

from hermes_cli import kanban_fairness_store as store
from hermes_cli.kanban_fairness_schema import BadRecord, make_record


def record(seq=1, epoch="e1", **kw):
    return make_record(capture_id="capture", board_uid="board", source_gateway="gw",
                       source_epoch=epoch, seq=seq, kind="loss", wall_ns=100,
                       mono_ns=10, clock_error_ns=0, payload={"count": 0}, **kw)


def seal(out, *sources):
    return store.seal_records(out, expected_sources=[
        {"source_gateway": "gw", "source_epoch": epoch, "last_seq": seq} for epoch, seq in sources])


def codes(out):
    return {g["code"] for g in store.load(out)["gaps"]}


def test_roundtrip_and_epoch_change(tmp_path):
    records = [record(), record(2), record(epoch="e2")]
    store.append_records(tmp_path, records)
    seal(tmp_path, ("e1", 2), ("e2", 1))
    assert store.load(tmp_path)["records"] == records
    assert "sequence_gap" not in codes(tmp_path)


@pytest.mark.parametrize("seq", [1, 0, 3])
def test_reject_duplicate_nonmonotonic_or_missing_sequence(tmp_path, seq):
    store.append_records(tmp_path, [record()])
    with pytest.raises((BadRecord, ValueError)):
        store.append_records(tmp_path, [record(seq)])
    assert not (tmp_path / "manifest.json").exists()


def test_absent_seal_unknown_clock_and_missing_epoch_tail(tmp_path):
    r = record()
    r["clock_error_ns"] = None
    store.append_records(tmp_path, [r])
    assert "unsealed" in codes(tmp_path)
    with pytest.raises(ValueError):
        seal(tmp_path, ("e1", 2))
    seal(tmp_path, ("e1", 1))
    assert "clock_unknown" in codes(tmp_path)


def test_changed_segment_and_final_seal(tmp_path):
    store.append_records(tmp_path, [record()])
    manifest = seal(tmp_path, ("e1", 1))
    path = tmp_path / manifest["segments"][0]["name"]
    path.write_bytes(path.read_bytes() + b"\n")
    assert "segment_mismatch" in codes(tmp_path)
    manifest.pop("final_seal")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert "unsealed" in codes(tmp_path)


def test_bound_precedes_write_and_identity_is_immutable(tmp_path):
    with pytest.raises(ValueError):
        store.append_records(tmp_path, [record()], max_bytes=1)
    assert not list(tmp_path.glob("segment-*.jsonl"))
    store.append_records(tmp_path, [record()])
    other = record(2)
    other["board_uid"] = "other"
    with pytest.raises(ValueError):
        store.append_records(tmp_path, [other])


def test_sealed_directory_is_not_appendable(tmp_path):
    store.append_records(tmp_path, [record()])
    seal(tmp_path, ("e1", 1))
    with pytest.raises(ValueError):
        store.append_records(tmp_path, [record(2)])
