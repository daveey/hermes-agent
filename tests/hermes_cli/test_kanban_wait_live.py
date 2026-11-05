"""An owner reachable through Desktop need not have a local task replica."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_awaiting as awaiting
from hermes_cli import kanban_wait_check as wait
from hermes_cli import kanban_wait_replica as replica
from tools.registry import registry
from tests.hermes_cli import test_kanban_wait_replica as fixtures

replica_store = fixtures.replica
_read_replica_task = replica.read_task

TID = "t_123456789abcdef0"


def test_real_missing_replica_row_uses_live_owner(replica_store, live, monkeypatch):
    monkeypatch.setattr(replica, "read_task", _read_replica_task)
    rc, text = wait.check_remote("other", "task", TID)
    assert rc == 1 and "live owner" in text


@pytest.fixture
def live(monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, "discover_plugins", lambda: None)
    monkeypatch.setattr(wait.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 255, "", "offline"))
    def missing(*args, **kwargs):
        raise replica.MissingReplica("missing replica")
    monkeypatch.setattr(replica, "read_task", missing)
    response = {"ok": True, "gateway": "other", "board": "default",
                "ref": f"@task:default@other/{TID}",
                "task": {"id": TID, "source": "live", "status": "running",
                         "wait_observation": {"board": "default", "human_request": False,
                                              "parked_agent": False, "orphan_reason": None}}}
    calls = []

    def handler(args, *, timeout_seconds):
        assert 0 < timeout_seconds <= 8
        calls.append(args)
        return json.dumps(response)

    monkeypatch.setattr(registry, "get_entry", lambda name: SimpleNamespace(handler=handler) if name == "cross_task" else None)
    return response, calls


@pytest.mark.parametrize("status,expected", [("running", 1), ("scheduled", 1), ("done", 0)])
def test_missing_replica_reads_live_owner(live, status, expected):
    response, calls = live
    response["task"]["status"] = status
    rc, text = wait.check_remote("other", "task", TID)
    assert rc == expected, text
    assert text.split()[:2] == [TID, status]
    assert calls == [{"action": "read", "target": f"@task:default@other/{TID}", "limit": 1}]
    if status != "done":
        awaiting.confirm_remote({"kind": "card", "gateway": "other", "ref": TID})
    else:
        with pytest.raises(awaiting.AwaitingError, match="already done"):
            awaiting.confirm_remote({"kind": "card", "gateway": "other", "ref": TID})


def test_live_preserves_since_and_custom_until(live):
    live[0]["task"]["status"] = "review"
    assert wait.check_remote("other", "task", TID, since=("review",))[0] == 1
    assert wait.check_remote("other", "task", TID, until=("done",))[0] == 1
    assert wait.check_remote("other", "task", TID)[0] == 0


@pytest.mark.parametrize("field,value", [("source", "replica"), ("source", None), ("id", "t_wrong"), ("status", "nonsense")])
def test_unproven_task_stays_error(live, field, value):
    live[0]["task"][field] = value
    assert wait.check_remote("other", "task", TID)[0] == 2


@pytest.mark.parametrize("field,value", [("ok", False), ("board", "different"), ("gateway", "different")])
def test_wrong_envelope_stays_error(live, field, value):
    live[0][field] = value
    assert wait.check_remote("other", "task", TID)[0] == 2


def test_replica_observations_do_not_need_desktop(live, monkeypatch):
    monkeypatch.setattr(replica, "check", lambda *a, **kw: (3, "known stale card"))
    assert wait.check_remote("other", "task", TID) == (3, "known stale card")
    assert not live[1]


def test_missing_plugin_keeps_error(live, monkeypatch):
    monkeypatch.setattr(registry, "get_entry", lambda name: None)
    assert wait.check_remote("other", "task", TID)[0] == 2


@pytest.mark.parametrize("status", ["scheduled", "blocked"])
def test_orphan_guards_survive_live_fallback(live, status):
    live[0]["task"]["status"] = status
    live[0]["task"]["wait_observation"].update(parked_agent=True, orphan_reason="producer gone")
    assert wait.check_remote("other", "task", TID)[0] == 0
    with pytest.raises(awaiting.AwaitingError, match="not live"):
        awaiting.confirm_remote({"kind": "card", "gateway": "other", "ref": TID})


@pytest.mark.parametrize("field", ["parked_agent", "orphan_reason", "human_request", "board"])
def test_incomplete_owner_proof_is_error(live, field):
    del live[0]["task"]["wait_observation"][field]
    assert wait.check_remote("other", "task", TID)[0] == 2


def test_live_gateway_alias(live):
    live[0]["gateway"] = "canonical"
    live[0]["ref"] = f"@task:default@canonical/{TID}"
    assert wait.check_remote("other", "task", TID)[0] == 1


def test_corrupt_replica_does_not_use_live_transport(live, monkeypatch):
    def corrupt(*args, **kwargs):
        raise ValueError("ambiguous replica")
    monkeypatch.setattr(replica, "read_task", corrupt)
    assert wait.check_remote("other", "task", TID)[0] == 2
    assert not live[1]


def test_discovery_failure_is_error_not_pending(live, monkeypatch):
    from hermes_cli import plugins
    def broken():
        raise RuntimeError("discovery failed")
    monkeypatch.setattr(plugins, "discover_plugins", broken)
    rc, text = wait.check_remote("other", "task", TID)
    assert rc == 2 and "discovery failed" in text


def test_old_unbounded_handler_is_not_called(live, monkeypatch):
    def old(*args, **kwargs):
        pytest.fail("unbounded handler called")
    monkeypatch.setattr(registry, "get_entry", lambda name: SimpleNamespace(handler=old))
    assert wait.check_remote("other", "task", TID)[0] == 2


def test_ssh_timeout_leaves_live_budget(live, monkeypatch):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 15
        raise subprocess.TimeoutExpired(args, 15)
    monkeypatch.setattr(wait.subprocess, "run", timeout)
    assert wait.check_remote("other", "task", TID)[0] == 1


def test_exhausted_budget_does_not_call_transport(live, monkeypatch):
    times = iter([0, 26])
    monkeypatch.setattr(wait.time, "monotonic", lambda: next(times))
    rc, text = wait.check_remote("other", "task", TID)
    assert rc == 2 and "budget exhausted" in text
    assert not live[1]
