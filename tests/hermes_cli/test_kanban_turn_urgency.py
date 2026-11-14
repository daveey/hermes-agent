"""Fail-closed local turn-urgency observation reader."""

import json

import pytest

from hermes_cli import kanban_turn_urgency as urgency


def observation():
    return {
        "version": 1,
        "observed_at": 1000,
        "valid_until": 1120,
        "board": "work",
        "source_revision": "revision-1",
        "owners": [
            {
                "task_id": "task-1",
                "source": "core-lane",
                "ref": "refs/pull/1/head",
                "head": "abc123",
                "expires_at": 1110,
            }
        ],
    }


def write_observation(tmp_path, value):
    path = tmp_path / "urgency.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_read_valid_owners(tmp_path):
    value = observation()
    value["owners"].append(dict(value["owners"][0], task_id="task-2", source="state-turn"))
    value["owners"].append(dict(value["owners"][0]))
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == {"task-1", "task-2"}


def test_board_scoped_default_paths(tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    (tmp_path / "cache").mkdir()
    for board, filename in [("work", "kanban-turn-urgency-work.json"),
                            ("default", "kanban-turn-urgency.json")]:
        doc = observation()
        doc["board"] = board
        doc["owners"][0]["task_id"] = board
        (tmp_path / "cache" / filename).write_text(json.dumps(doc))
        assert urgency.read_owners(board=board, now_s=1050) == {board}
    assert urgency.read_owners(board="other", now_s=1050) == set()
    assert urgency.read_owners(board="../work", now_s=1050) == set()


@pytest.mark.parametrize("field,bad", [
    ("version", None), ("version", True), ("version", 1.0), ("version", 2),
    ("board", "other"), ("board", ""), ("board", "   "), ("board", 1),
    ("source_revision", ""), ("source_revision", "  "), ("source_revision", None),
    ("owners", {}), ("owners", None), ("owners", "task-1"),
    ("observed_at", 1051), ("valid_until", 1050), ("valid_until", 1049),
    ("valid_until", 1121), ("valid_until", 999),
] + [(field, bad) for field in ("observed_at", "valid_until")
     for bad in (None, True, False, "1000", float("nan"), float("inf"), -float("inf"), 10**400)])
def test_invalid_global_fails_closed(tmp_path, field, bad):
    value = observation()
    value[field] = bad
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == set()


@pytest.mark.parametrize("field", ["version", "observed_at", "valid_until", "board", "source_revision", "owners"])
def test_missing_global_field(tmp_path, field):
    value = observation()
    del value[field]
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == set()


@pytest.mark.parametrize("value", [None, [], 1, "cache"])
def test_non_object_document(tmp_path, value):
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == set()


@pytest.mark.parametrize("now", [True, False, None, "1050", float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_now(tmp_path, now):
    path = write_observation(tmp_path, observation())
    assert urgency.read_owners(board="work", now_s=now, path=path) == set()


@pytest.mark.parametrize("board", ["", "  ", None, 1])
def test_invalid_requested_board(tmp_path, board):
    value = observation()
    value["board"] = board
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board=board, now_s=1050, path=path) == set()


def test_observed_at_inclusive_and_empty_owners(tmp_path):
    value = observation()
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1000, path=path) == {"task-1"}
    value["owners"] = []
    path = write_observation(tmp_path, value)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == set()


@pytest.mark.parametrize("field,bad", [
    ("task_id", ""), ("task_id", []), ("source", "foreign"), ("ref", None), ("head", "  "),
    ("expires_at", True), ("expires_at", float("inf")), ("expires_at", 1050),
])
def test_invalid_owner_invalidates_document(tmp_path, field, bad):
    value = observation()
    value["owners"].append(dict(value["owners"][0], **{field: bad}))
    assert urgency.read_owners(board="work", now_s=1050,
                              path=write_observation(tmp_path, value)) == set()


@pytest.mark.parametrize("raw", [b"{", b"\xff", b" " * (128 * 1024 + 1), b"[" * 2000],
                         ids=["broken-json", "bad-utf8", "oversized", "deep-json"])
def test_invalid_bytes_fail_closed(tmp_path, raw):
    path = tmp_path / "cache.json"
    path.write_bytes(raw)
    assert urgency.read_owners(board="work", now_s=1050, path=path) == set()


def test_missing_and_default_path(tmp_path, monkeypatch):
    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert urgency.read_owners(board="work", now_s=1050) == set()
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "kanban-turn-urgency-work.json").write_text(json.dumps(observation()), encoding="utf-8")
    assert urgency.read_owners(board="work", now_s=1050) == {"task-1"}

