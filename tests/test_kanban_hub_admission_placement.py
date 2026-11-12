"""Hub candidate admission never changes non-hub or structural decisions."""
import pytest

from hermes_cli import kanban_hub_admission as judge
from hermes_cli import kanban_placement as placement


@pytest.fixture
def gate(monkeypatch):
    calls = []
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    def fake(fields, *, hub_reason=None):
        calls.append((fields, hub_reason))
        return judge.Verdict(admit=fields.get("title") == "Chrome", need="hub_chrome",
                             evidence="Chrome", why="test verdict")
    monkeypatch.setattr(judge, "admit", fake)
    return calls


def decide(fields, gateway="hub"):
    return placement.decide(fields, self_gw=gateway,
                            cfg={"worker_pools": {"hub": ["worker-1"]}},
                            live=lambda _: None, carries=True)


@pytest.mark.parametrize("pin", ["auto", "local"])
@pytest.mark.parametrize("title,admitted", [("Chrome", True), ("Read ~/.hermes/logs on the hub", False)])
def test_hub_auto_candidate(gate, title, admitted, pin):
    result = decide({"title": title, "placement": pin})
    assert result["hub_admission"]["admit"] is admitted
    assert result.get("placement_policy") == (None if admitted else "worker-first-v1")
    if not admitted:
        assert "hub:write" in result["requires"]
    assert len(gate) == 1


@pytest.mark.parametrize("pin", ["hub", "hermes-hub"])
@pytest.mark.parametrize("title,where", [("Chrome", "hub"), ("Read logs", "refuse")])
def test_explicit_foreign_hub(gate, pin, title, where):
    assert decide({"title": title, "placement": pin}, "mac")["where"] == where
    assert len(gate) == 1


@pytest.mark.parametrize("fields", [{"workspace_kind": "dir"}, {"board": "other"},
                                     {"requires": ["host_repo"]}, {"needs": ["ssh:somewhere"]},
                                     {"assignee": "other"}])
def test_structural_zero_calls(gate, fields):
    decide({"title": "Read ~/.hermes/logs on the hub", **fields})
    assert gate == []


@pytest.mark.parametrize("fields,gateway", [({"placement": "local"}, "mac"),
    ({"runs_on": ["machine:worker-1"]}, "hub"), ({"title": "code"}, "mac")])
def test_nonhub_zero_calls(gate, fields, gateway):
    decide(fields, gateway)
    assert gate == []


def test_force_operator_audit(gate, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ID", "operator-session")
    result = decide({"title": "Read logs", "placement": "hub", "force_hub": True})
    assert result["hub_admission"]["forced"] is True
    assert result["hub_admission"]["session"] == "operator-session"
    assert gate == []


@pytest.mark.parametrize("variable", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_force_worker_refused(gate, monkeypatch, variable):
    monkeypatch.setenv(variable, "t_worker")
    result = decide({"placement": "hub", "force_hub": True})
    assert result["where"] == "refuse"
    assert gate == []


def test_machine_hub_pin_denied(gate):
    result = decide({"title": "Read logs", "runs_on": ["machine:hub"]})
    assert result["where"] == "refuse"
    assert len(gate) == 1


def test_machine_hub_pin_admitted(gate):
    result = decide({"title": "Chrome", "runs_on": ["machine:hub"]})
    assert result["where"] == "local"
    assert result["hub_admission"]["admit"] is True


def test_judge_exception_cannot_fall_back_to_hub(gate, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("judge unavailable")
    monkeypatch.setattr(judge, "admit", broken)
    result = decide({"placement": "hub"})
    assert result["where"] == "refuse"
    assert result["hub_admission"]["why"] == "admission unavailable"


def test_denied_without_pool_refuses(gate):
    result = placement.decide({"title": "Read logs"}, self_gw="hub", cfg={})
    assert result["where"] == "refuse"
    assert result["hub_admission"]["admit"] is False


def test_tool_keeps_verdict_and_effective_contract(gate):
    from tools.kanban_tools_placement import _local_placement
    fields = {"title": "Read logs", "placement": "local"}
    decision = decide(fields)
    result = _local_placement(placement, fields, decision, machine_ok=None, note_local_pin=True)
    assert result == decision
    assert len(gate) == 1
