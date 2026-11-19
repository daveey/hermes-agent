"""Adversarial storage boundaries use real envelopes and recomputed integrity hashes."""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_fairness_segments as seg
from hermes_cli import kanban_fairness_store as store
from hermes_cli.kanban_fairness_schema import make_record


def record(seq=1, epoch="e1"):
    return make_record(capture_id="capture", board_uid="board", source_gateway="gw",
                       source_epoch=epoch, seq=seq, kind="row_change", wall_ns=100,
                       mono_ns=10, clock_error_ns=0,
                       payload={"task_id": "task", "op": "update", "fields": {"status": "ready"},
                                "journal_seq": seq, "journal_kind": "task", "database_uid": "db",
                                "source_uid": "source"})


def save(out, manifest):
    manifest["final_seal"] = seg.final_seal(manifest)
    (out / seg.MANIFEST).write_bytes(seg.encoded(manifest))


def sealed(out, records=None):
    seg.append_records(out, records or [record()])
    return seg.seal_records(out, expected_sources=seg.receipts(out)["sources"])


def codes(out):
    return {g["code"] for g in store.load(out)["gaps"]}


def test_real_no_gap_roundtrip(tmp_path):
    records = [record(), record(2), record(1, "e2")]
    sealed(tmp_path, records)
    loaded = store.load(tmp_path)
    assert loaded["records"] == records
    # Journal sequence is global, not exporter-epoch local.
    assert {g["code"] for g in loaded["gaps"]} == {"journal_sequence_gap"}
    other = tmp_path / "other"
    sealed(other, [record(), record(2)])
    assert store.load(other)["gaps"] == []


@pytest.mark.parametrize("position", [0, 3, 6])
def test_oversize_journal_row_exports_loss_without_stalling(tmp_path, monkeypatch, position):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_fairness_journal as journal

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    out = tmp_path / "capture"
    with kbc.connect_closing() as conn:
        journal.arm(conn, "capture")
        for index in range(7):
            kb.create_task(conn, title="fixture", assignee="a",
                           idempotency_key="x" * 300_000 if index == position else f"key-{index}")
        rows = list(journal.rows(conn, 0))
        assert len(rows) == 14
        result = store.drain(conn, out)
        assert result["journal_seq"] == rows[-1][0]
        assert store.drain(conn, out) == result
        store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    capture = store.load(out)
    losses = [r for r in capture["records"] if r["kind"] == "loss"]
    assert len(losses) == 1
    assert losses[0]["payload"]["reason"] == "oversize"
    assert losses[0]["payload"]["count"] == 1
    exported = [r for r in capture["records"] if r["kind"] != "loss"]
    expected = [r[0] for r in rows if len(r[-1].encode("utf-8")) < 256 * 1024]
    assert [r["payload"]["journal_seq"] for r in exported] == expected
    assert len(exported) == 13
    assert capture["gaps"]  # Loss never certifies complete coverage.
    assert "export_failed" not in {g["code"] for g in capture["gaps"]}


@pytest.mark.parametrize("value", [None, [], 3, "bad", {"segments": []}])
def test_malformed_manifest_and_receipts_fail_closed(tmp_path, value):
    (tmp_path / seg.MANIFEST).write_text(json.dumps(value))
    assert codes(tmp_path)
    (tmp_path / seg.MANIFEST).unlink()
    (tmp_path / seg.RECEIPTS).write_text(json.dumps(value))
    assert codes(tmp_path)
    with pytest.raises(ValueError):
        seg.append_records(tmp_path, [record()])


@pytest.mark.parametrize("field,value", [("segments", [None]), ("segments", {}), ("sources", [None]),
    ("sources", {}), ("gaps", [None]), ("gaps", "bad"), ("bytes", True), ("bytes", 0),
    ("version", True), ("journal", []), ("journal", {"stored": "bad"})])
def test_malformed_fields_fail_closed(tmp_path, field, value):
    manifest = sealed(tmp_path)
    manifest[field] = value
    save(tmp_path, manifest)
    assert codes(tmp_path)


@pytest.mark.parametrize("seqs", [[1, 1], [2, 1], [1, 3]])
def test_reader_rechecks_sequence_after_rehash(tmp_path, seqs):
    manifest = sealed(tmp_path, [record(), record(2)])
    data = b"".join(seg.encoded(record(n)) + b"\n" for n in seqs)
    entry = manifest["segments"][0]
    (tmp_path / entry["name"]).write_bytes(data)
    entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest["bytes"] = len(data)
    save(tmp_path, manifest)
    assert "sequence_gap" in codes(tmp_path)


def test_epoch_tail_order_and_absent_seal(tmp_path):
    seg.append_records(tmp_path, [record()])
    seg.append_records(tmp_path, [record(1, "e2")])
    manifest = seg.seal_records(tmp_path, expected_sources=seg.receipts(tmp_path)["sources"])
    manifest["sources"].pop()
    manifest["segments"].reverse()
    save(tmp_path, manifest)
    assert {"source_tail", "segment_order"} <= codes(tmp_path)
    manifest.pop("final_seal")
    (tmp_path / seg.MANIFEST).write_bytes(seg.encoded(manifest))
    assert "unsealed" in codes(tmp_path)


def test_unknown_mono_and_gap_dedup(tmp_path):
    records = [record(), record(2)]
    for item in records:
        item["mono_ns"] = None
    sealed(tmp_path, records)
    gaps = store.load(tmp_path)["gaps"]
    assert {g["code"] for g in gaps} == {"clock_unknown"}
    assert len(gaps) == 1


def test_segment_ceiling_before_read(tmp_path, monkeypatch):
    manifest = sealed(tmp_path)
    monkeypatch.setattr(seg, "SEGMENT_BYTES", 10)
    assert codes(tmp_path)
    # Declared segment ceiling is checked even with a recomputed final seal.
    manifest["segments"][0]["bytes"] = seg.MAX_CAPTURE_BYTES
    save(tmp_path, manifest)
    assert codes(tmp_path)


def test_empty_source_cuts_and_metadata_identity_refused(tmp_path):
    seg.append_records(tmp_path, [])
    with pytest.raises(ValueError):
        seg.seal_records(tmp_path, expected_sources=[
            {"source_gateway": "gw", "source_epoch": "e1", "last_seq": 0}])
    with pytest.raises(ValueError):
        seg.append_records(tmp_path, [record()], _metadata={"board_uid": "other"})


def test_owner_bound_and_malformed_owner(tmp_path, monkeypatch):
    sealed(tmp_path)
    (tmp_path / store.OWNERS).write_text("[1]\n")
    assert "bad_owner_record" in codes(tmp_path)
    monkeypatch.setattr(seg, "MAX_CAPTURE_BYTES", 10)
    assert codes(tmp_path)


def test_write_manifest_refuses_reseal_before_db_access(tmp_path):
    sealed(tmp_path)
    with pytest.raises(ValueError):
        store.write_manifest(None, tmp_path, board="board", assumptions={}, policy={})


def test_database_cursor_published_with_segment_receipt(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_fairness_journal as journal

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    out = tmp_path / "capture"
    publications = []
    real_write = seg.write

    def observe(path, data):
        real_write(path, data)
        if path.name == seg.RECEIPTS:
            receipt = json.loads(path.read_bytes())
            if receipt["segments"]:
                publications.append(receipt)
                assert receipt["journal_seq"] == sum(s["records"] for s in receipt["segments"])

    monkeypatch.setattr(seg, "write", observe)
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="fixture", assignee="a")
        journal.arm(conn, "capture")
        result = store.drain(conn, out)
        assert len(publications) == 1
        assert result["journal_seq"] == 1
        store.drain(conn, out)
        assert len(publications) == 1


@pytest.mark.parametrize("field,value", [("records", True), ("records", -1), ("bytes", "2"),
    ("name", []), ("sha256", {}), ("sha256", "bad")])
def test_bad_segment_receipt_shape(tmp_path, field, value):
    manifest = sealed(tmp_path)
    manifest["segments"][0][field] = value
    save(tmp_path, manifest)
    assert codes(tmp_path)


def test_read_limits_precede_allocation(tmp_path, monkeypatch):
    sealed(tmp_path)
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert not path.name.startswith("segment-")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(seg, "MAX_METADATA_BYTES", 1)
    monkeypatch.setattr(Path, "open", guarded)
    assert "bad_manifest" in codes(tmp_path)


def test_owner_file_over_bound(tmp_path, monkeypatch):
    sealed(tmp_path)
    (tmp_path / store.OWNERS).write_bytes(b" " * 100)
    # Manifest remains within bound; owner ceiling is independently enforced.
    real_read = seg.read_bounded

    def small_owner(path, limit):
        return real_read(path, 10 if path.name == store.OWNERS else limit)

    monkeypatch.setattr(seg, "read_bounded", small_owner)
    assert "bad_owner_record" in codes(tmp_path)
