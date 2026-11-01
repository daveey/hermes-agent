"""Remote-only assignment contracts across label discovery, tool/CLI create and repinning."""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_capabilities import CapabilityMismatchError
from tests.hermes_cli.test_kanban_requires_placement import _publish
from tests.hermes_cli.test_kanban_runs_on_remote_assignee import NEEDS, PROFILE, _count
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER

pytest_plugins = ["tests.hermes_cli.test_kanban_requires_placement"]


def test_plain_label_remote_profile_creates(gws, monkeypatch):
    hub, _ = gws
    machines = {**MACHINES, WORKER: {**MACHINES[WORKER], "labels": ["linux", "gpu"]}}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": list(machines.values())})
    _publish(monkeypatch, {WORKER: {PROFILE: set(NEEDS)}})
    tid = kb.create_task(hub.conn, title="GPU work", assignee=PROFILE, requires=NEEDS, runs_on=["gpu"])
    assert kb.get_task(hub.conn, tid).assignee == PROFILE
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0]) == ["gpu"]


@pytest.mark.parametrize("registry_state", ["unreachable", "empty", "missing-self"])
def test_unknown_local_labels_keep_local_validation(gws, monkeypatch, registry_state):
    hub, _ = gws
    def fetch():
        if registry_state == "unreachable":
            raise OSError("Board unavailable")
        return {"me": HUB, "machines": [] if registry_state == "empty" else [MACHINES[WORKER]]}
    wn.set_registry_fetch(fetch)
    _publish(monkeypatch, {WORKER: {PROFILE: set(NEEDS)}})
    before = _count(hub.conn)
    with pytest.raises(CapabilityMismatchError, match="does not exist"):
        kb.create_task(hub.conn, title="GPU work", assignee=PROFILE, requires=NEEDS, runs_on=["gpu"])
    assert _count(hub.conn) == before


def test_heavy_route_remote_only_assignee(gws, monkeypatch):
    from hermes_cli import kanban_placement as kp
    from tools import kanban_tools as kt
    hub, _ = gws
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    monkeypatch.setattr(kp, "config", lambda: {"heavy_route": {HUB: WORKER}, "assignees": [PROFILE]})
    _publish(monkeypatch, {WORKER: {PROFILE: set(NEEDS)}})
    out = json.loads(kt._handle_create({"title": "Run regression tests", "assignee": PROFILE, "requires": NEEDS}))
    assert not out.get("error"), out
    assert out["placement"]["why"].startswith("heavy work"), out
    row = hub.conn.execute("SELECT assignee, runs_on FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
    assert row[0] == PROFILE and json.loads(row[1]) == [f"machine:{WORKER}"]


@pytest.mark.parametrize("remote", [True, False])
def test_cli_runs_on_remote_only_assignee(gws, monkeypatch, capsys, remote):
    from hermes_cli import kanban as kc
    hub, _ = gws
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    _publish(monkeypatch, {WORKER: {PROFILE: set(NEEDS)}})
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers())
    target = WORKER if remote else HUB
    args = parser.parse_args(["kanban", "create", "CLI work", "--assignee", PROFILE,
                              "--runs-on", f"machine:{target}", "--requires", "terminal", "--requires", "file",
                              "--requires", "git", "--json"])
    before = _count(hub.conn)
    rc = kc.kanban_command(args)
    output = capsys.readouterr()
    if not remote:
        assert rc == 2 and "does not exist" in output.err
        assert _count(hub.conn) == before
        return
    assert rc == 0, output
    out = json.loads(output.out)
    row = hub.conn.execute("SELECT assignee, runs_on FROM tasks WHERE id=?", (out["id"],)).fetchone()
    assert row[0] == PROFILE and json.loads(row[1]) == [f"machine:{WORKER}"]


@pytest.mark.parametrize("remote", [True, False])
def test_set_model_override_uses_execution_host_policy(gws, monkeypatch, remote):
    from hermes_cli.kanban_model_assignment import ModelPinNotInRegistryError, ModelProviderMismatchError
    from hermes_cli.kanban_pin_provider import PinProviderUnresolvedError
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    hub, _ = gws
    with open(hub.home / "config.yaml", "a") as fh:
        fh.write("model:\n  default: claude-sonnet-5-5\n  provider: anthropic\n" + _managed_yaml())
    hub.enter()
    _publish(monkeypatch, {WORKER: {"default": set(NEEDS)}})
    tid = kb.create_task(hub.conn, title="repin", assignee="default", requires=NEEDS,
                         runs_on=[f"machine:{WORKER if remote else HUB}"])
    if not remote:
        with pytest.raises(ModelPinNotInRegistryError):
            kb.set_model_override(hub.conn, tid, "claude-sonnet-4-5", "anthropic")
        assert kb.get_task(hub.conn, tid).model_override is None
        return
    assert kb.set_model_override(hub.conn, tid, "claude-sonnet-4-5", "anthropic")
    task = kb.get_task(hub.conn, tid)
    assert (task.model_override, task.provider_override) == ("claude-sonnet-4-5", "anthropic")
    with pytest.raises(ModelProviderMismatchError):
        kb.set_model_override(hub.conn, tid, "gpt-5", "anthropic")
    monkeypatch.setattr("hermes_cli.models_detect.provider_has_credentials", lambda p: p == "anthropic")
    with pytest.raises(PinProviderUnresolvedError):
        kb.set_model_override(hub.conn, tid, "claude-opus-5-5")
    assert kb.get_task(hub.conn, tid).model_override == "claude-sonnet-4-5"
    assert kb.set_model_override(hub.conn, tid, "class:deep")
    assert kb.get_task(hub.conn, tid).provider_override is None
    assert kb.set_model_override(hub.conn, tid, None)
    assert kb.get_task(hub.conn, tid).model_override is None
