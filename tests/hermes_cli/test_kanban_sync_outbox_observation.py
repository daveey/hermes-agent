"""Persistence is boot- and board-scoped, and never invents an observation."""
import json

import pytest

from hermes_cli import kanban_sync_outbox_observation as obs


@pytest.fixture
def memo(tmp_path, monkeypatch):
    obs.reset()
    monkeypatch.setattr(obs.replica_age, "_boot_id", lambda: "boot")
    path = tmp_path / "board.db"
    yield path
    obs.reset()


@pytest.mark.parametrize("patch", [
    {"boot": "other"}, {"since": -1}, {"since": 501}, {"seen_at": 1001},
    {"seen_at": 399}, {"since": float("nan")}, {"filed": "false"},
    {"warned_at": 9999}, {"warned_at": "bad"}, {"op_id": []},
])
def test_invalid_persisted_observation_resets(memo, patch):
    rec = dict(op_id="op", since=100, seen_at=500, boot="boot", filed=False, warned_at=None)
    rec.update(patch)
    obs._path(memo).write_text(json.dumps(rec), encoding="utf-8")
    assert obs.observe(memo, "op", 1000).since == 1000


@pytest.mark.parametrize("data", ["[]", "{", "null", "{}"])
def test_unreadable_memo_starts_fresh(memo, data):
    obs._path(memo).write_text(data, encoding="utf-8")
    assert obs.observe(memo, "op", 1000).since == 1000


def test_board_isolation_and_absence_remove_persistence(memo):
    other = memo.with_name("other.db")
    obs.observe(memo, "same-op", 100)
    obs.observe(other, "same-op", 300)
    obs.reset()
    assert obs.observe(memo, "same-op", 500).since == 100
    assert obs.observe(other, "same-op", 500).since == 300
    assert obs.observe(memo, None, 600) is None
    assert not obs._path(memo).exists()
    obs.reset()
    assert obs.observe(memo, "same-op", 700).since == 700


def test_write_failure_preserves_in_memory_budget(memo, monkeypatch):
    def denied(*a, **kw):
        raise OSError("read only")
    monkeypatch.setattr(type(memo), "write_text", denied)
    assert obs.observe(memo, "op", 100).since == 100
    assert obs.observe(memo, "op", 700).since == 100
