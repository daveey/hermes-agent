"""Remote board obligations and plugin closeouts share canonical card identity."""

import pytest

from hermes_cli import kanban_session_activity as ksa


@pytest.mark.parametrize("kind,external", [("ci", 1), ("deploy", 1), ("card", 0), ("clock", 1), ("external", 0), (None, 0)])
@pytest.mark.parametrize("status", ["scheduled", "running", "unknown", "done"])
def test_replica_external_wait_uses_the_parked_snapshot(remote_projection, monkeypatch, kind, external, status):
    key = "edge/default/default/t_ci"
    cards = [{
        "key": key, "id": "t_ci", "session": sid, "status": status,
        "gateway": "edge", "board": "default", "profile": "default", "unfinished": status != "done",
        "awaiting": {"kind": kind, "ref": "vendor-job"} if kind is not None else None,
        "park": {"wake_at": 9_999_999_999, "outcome": None},
    } for sid in ("s_root", "s_tip")]
    monkeypatch.setattr(ksa, "_remote_cards", lambda *args: cards)
    for _ in range(2):
        row = ksa.annotate_kanban_activity([{
            "id": "s_tip", "_lineage_ids": ["s_root", "s_tip"],
        }])[0]
        expected = external if status == "scheduled" else 0
        assert row.get("kanban_external", 0) == expected
        assert row.get("kanban_external_unknown", 0) == (expected if kind != "clock" else 0)
        assert row.get("kanban_external_down", 0) == 0
        assert expected <= row.get("kanban_parked", 0)
        assert row.get("kanban_blocked", 0) == 0


@pytest.fixture
def remote_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa, "_waiting_sources", {})
    monkeypatch.setattr(ksa, "_remote_wait", lambda card: None)
    ksa._reset_cache_for_tests()
    yield
    ksa._reset_cache_for_tests()


@pytest.mark.parametrize("status,active,blocked,parked", [
    ("running", 1, 0, 0),
    ("scheduled", 1, 0, 1),
    ("unknown", 1, 0, 1),
    ("blocked", 0, 1, 0),
])
@pytest.mark.parametrize("extra_closeout", [False, True])
def test_remote_closeout_overlap_counts_once(
    remote_projection, monkeypatch, status, active, blocked, parked, extra_closeout,
):
    key = "hub/default/default/t_shared"
    cards = [{
        "key": key, "id": "t_shared", "session": sid, "status": status,
        "gateway": "hub", "board": "default", "profile": "default", "unfinished": True,
        "block_kind": "needs_input",
        "human_request": {
            "action": "Choose a release window.", "why_human": "Only the owner can choose downtime.",
            "interaction": "reply", "scope": "Staging release", "evidence": "No release window selected.",
        },
    } for sid in ("s_root", "s_tip")]
    monkeypatch.setattr(ksa, "_remote_cards", lambda *args: cards)
    pending = [key] + (["hub/default/default/t_finished"] if extra_closeout else [])
    ksa.register_waiting_source("closeout", lambda: {"s_tip": pending})
    for _ in range(2):  # fresh and cached annotation, including compression aliases
        row = ksa.annotate_kanban_activity([{
            "id": "s_tip", "_lineage_ids": ["s_root", "s_tip"],
        }])[0]
        assert row.get("kanban_active", 0) == active + int(extra_closeout)
        assert row.get("kanban_blocked", 0) == blocked
        assert row.get("kanban_parked", 0) == parked
        assert row["kanban_waiting_on"] == sorted(pending)
    # The board-only waiting API remains local-only; scoped closeout ids stay in the row.
    assert ksa.waiting_on_task_ids() == {}


def test_remote_waiting_identity_does_not_collapse_another_gateway(remote_projection, monkeypatch):
    key = "hub/default/default/t_shared"
    other_key = "other/default/default/t_shared"
    monkeypatch.setattr(ksa, "_remote_cards", lambda *args: [{
        "key": key, "id": "t_shared", "session": "s", "status": "running",
        "gateway": "hub", "board": "default",
    }])
    ksa.register_waiting_source("closeout", lambda: {"s": [other_key]})
    row = ksa.annotate_kanban_activity([{"id": "s"}])[0]
    assert row["kanban_active"] == 2
    assert row["kanban_waiting_on"] == [other_key]
