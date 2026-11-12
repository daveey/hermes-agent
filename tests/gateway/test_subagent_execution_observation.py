"""Native gateway subagent.list projection shares the canonical registry producer."""
from types import SimpleNamespace

from gateway import session_ancillary as ancillary
from hermes_cli import kanban_execution_observations as scope
from hermes_cli.turn_process_execution_observations import registry_subagent


def test_native_list_projection_matches_registry_observation(monkeypatch):
    child = SimpleNamespace(session_id="child", _parent_session_id="parent",
                            get_activity_summary=lambda: {"last_activity_ts": 1000})
    record = {"subagent_id": "registry-id", "agent": child, "owner_agent_session_id": "parent",
              "started_at": 900, "status": "running"}
    monkeypatch.setattr(ancillary, "_require_current_route", lambda *args: None)
    monkeypatch.setattr(ancillary, "_owner_objects", lambda *args, **kwargs: (None, [record]))
    monkeypatch.setattr(scope, "local_scope", lambda: ("edge", "work"))
    monkeypatch.setattr(ancillary.time, "time", lambda: 1005)
    response = ancillary._project(None, None, {}, "subagents")
    (row,) = response["subagents"]
    assert row["child_session_id"] == "child" and row["subagent_id"] == "registry-id"
    assert row["execution_observations"] == [registry_subagent(record, gateway="edge", profile="work", now=1005)]
    assert row["execution_observations"][0]["state"] == "running"
    assert row["execution_observations"][0]["identity"]["generation"] == "session=child"


def test_native_legacy_child_is_unknown_and_adapter_failure_keeps_roster(monkeypatch):
    monkeypatch.setattr(scope, "local_scope", lambda: ("edge", "default"))
    row = ancillary.subagent_row({"subagent_id": "legacy", "status": "running", "started_at": 1000,
                                  "owner_agent_session_id": "parent"})
    assert row["execution_observations"][0]["state"] == "unknown"
    monkeypatch.setattr(scope, "local_scope", lambda: (_ for _ in ()).throw(RuntimeError("scope unavailable")))
    row = ancillary.subagent_row({"subagent_id": "legacy"})
    assert row["subagent_id"] == "legacy" and row["execution_observations"] == []
