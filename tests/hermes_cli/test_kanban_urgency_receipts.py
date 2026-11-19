"""Real urgency reads retain their decisions while recording sanitized observations."""
import base64
import hashlib
import json

import pytest

from hermes_cli import kanban_turn_urgency as urgency
from hermes_cli.kanban_fairness_payloads import urgency_payload


def document():
    return {"version": 1, "board": "default", "source_revision": "rev",
            "observed_at": 100, "valid_until": 200,
            "owners": [{"task_id": "task", "source": "core-lane", "ref": "ref",
                        "head": "head", "expires_at": 200}]}


@pytest.mark.parametrize("case,outcome,owners", [
    ("valid", "valid", {"task"}), ("expired", "expired", set()),
    ("missing", "missing", set()), ("malformed", "invalid", set()),
    ("extra", "invalid", {"task"}), ("oversize", "oversize", set()),
])
def test_real_read_receipt(tmp_path, monkeypatch, case, outcome, owners):
    from hermes_cli import kanban_fairness_runtime as runtime

    receipts = []
    monkeypatch.setattr(runtime, "enabled", lambda **kw: True)
    monkeypatch.setattr(runtime, "submit", lambda **kw: receipts.append(kw) or True)
    path = tmp_path / "owners.json"
    value = document()
    if case == "extra":
        value["secret"] = "never persist this"
    raw = json.dumps(value).encode()
    if case == "malformed":
        raw = b'{"secret":"never persist this"'
    if case == "oversize":
        raw = b"x" * (128 * 1024 + 10)
    if case != "missing":
        path.write_bytes(raw)
    result = urgency.read_owners(board="default", now_s=201 if case == "expired" else 150, path=path)
    assert result == owners
    assert len(receipts) == 1
    receipt = urgency_payload(receipts[0]["payload"])
    assert receipt["outcome"] == outcome
    assert receipt["owner_ids"] == sorted(result)
    if case in ("valid", "expired"):
        assert base64.b64decode(receipt["raw_b64"]) == raw
        assert receipt["raw_complete"]
    elif case != "missing":
        assert receipt["raw_b64"] is None and receipt["redacted"]
        assert "never persist this" not in json.dumps(receipt)
    if case != "missing":
        observed = raw[:128 * 1024 + 1]
        assert receipt["raw_size"] == len(observed)
        assert receipt["raw_sha256"] == hashlib.sha256(observed).hexdigest()


@pytest.mark.parametrize("mutation,now,outcome", [
    ({"version": 2}, 201, "invalid"),
    ({"board": "other"}, 201, "invalid"),
    ({"source_revision": ""}, 201, "invalid"),
    ({"owners": "bad"}, 201, "invalid"),
    ({"observed_at": 300}, 201, "invalid"),
    ({"valid_until": 10**400}, 150, "invalid"),
    ({}, float("nan"), "invalid_request"),
])
def test_invalid_is_not_mislabeled_expired(tmp_path, monkeypatch, mutation, now, outcome):
    from hermes_cli import kanban_fairness_runtime as runtime

    receipts = []
    monkeypatch.setattr(runtime, "enabled", lambda **kw: True)
    monkeypatch.setattr(runtime, "submit", lambda **kw: receipts.append(kw) or True)
    value = document() | mutation
    path = tmp_path / "owners.json"
    path.write_text(json.dumps(value))
    assert urgency.read_owners(board="default", now_s=now, path=path) == set()
    assert urgency_payload(receipts[0]["payload"])["outcome"] == outcome


@pytest.mark.parametrize("error", [ValueError, OSError, RuntimeError])
def test_observation_failure_does_not_change_result(tmp_path, monkeypatch, error):
    from hermes_cli import kanban_fairness_runtime as runtime

    monkeypatch.setattr(runtime, "enabled", lambda **kw: True)

    def fail(**kw):
        raise error("evidence unavailable")

    monkeypatch.setattr(runtime, "submit", fail)
    path = tmp_path / "owners.json"
    path.write_text(json.dumps(document()))
    assert urgency.read_owners(board="default", now_s=150, path=path) == {"task"}


@pytest.mark.parametrize("case,outcome,expected", [
    ("duplicate", "invalid", {"task"}),
    ("nan", "invalid", set()),
    ("owner_expired", "expired", set()),
    ("invalid_expired_owner", "invalid", set()),
    ("io", "io_error", set()),
    ("bad_board", "invalid_request", set()),
])
def test_reader_edge_receipts(tmp_path, monkeypatch, case, outcome, expected):
    from hermes_cli import kanban_fairness_runtime as runtime

    receipts = []
    monkeypatch.setattr(runtime, "enabled", lambda **kw: True)
    monkeypatch.setattr(runtime, "submit", lambda **kw: receipts.append(kw) or True)
    value = document()
    if case == "nan":
        value["observed_at"] = float("nan")
    if case in {"owner_expired", "invalid_expired_owner"}:
        value["owners"][0]["expires_at"] = 149
    if case == "invalid_expired_owner":
        value["owners"][0]["source"] = "bad"
    raw = json.dumps(value).encode()
    if case == "duplicate":
        raw = raw.replace(b'"version": 1', b'"version": 1, "version": 1')
    path = tmp_path / "owners.json"
    path.write_bytes(raw)
    if case == "io":
        path = tmp_path  # Real directory open failure, not a mocked reader.
    board = "../invalid" if case == "bad_board" else "default"
    result = urgency.read_owners(board=board, now_s=150, path=None if case == "bad_board" else path)
    assert result == expected
    payload = urgency_payload(receipts[0]["payload"])
    assert payload["outcome"] == outcome
    assert payload["owner_ids"] == sorted(result)
    if case in {"duplicate", "nan", "invalid_expired_owner"}:
        assert payload["redacted"] and payload["raw_b64"] is None
    if case in {"io", "bad_board"}:
        assert payload["raw_sha256"] is None and payload["raw_size"] is None


def test_disabled_does_not_build_receipt(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_runtime as runtime

    monkeypatch.setattr(runtime, "enabled", lambda **kw: False)
    monkeypatch.setattr(runtime, "submit", lambda **kw: pytest.fail("disabled submit"))
    path = tmp_path / "owners.json"
    path.write_text(json.dumps(document()))
    assert urgency.read_owners(board="default", now_s=150, path=path) == {"task"}
