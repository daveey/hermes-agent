"""Residual step scopes name ongoing loop work without adding board CPU twice."""
from functools import wraps
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import loop_accounting as la


@pytest.mark.parametrize("enabled", [True, False])
def test_real_loop_publishes_step_names(monkeypatch, enabled):
    from hermes_cli import kanban_sync_links_pass, machine_metrics
    from hermes_cli import machine_cpu_sampler, machine_metrics_sender
    from hermes_cli import kanban_sync_schema_alert, kanban_sync_sweep_cadence
    kanban_sync_sweep_cadence.reset()
    la.stop()
    la.reset()
    monkeypatch.setattr(la, "scopes_enabled", lambda: enabled)
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="test", url="unused"))
    monkeypatch.setattr(client, "Hub", lambda ident: Mock())
    monkeypatch.setattr(client.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(client, "_schema_ahead_boards", lambda boards: [])
    monkeypatch.setattr(kanban_sync_schema_alert, "note_start", lambda: None)
    monkeypatch.setattr(kanban_sync_links_pass, "LinksPass", lambda gateway: Mock())
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *a: Mock())
    monkeypatch.setattr(machine_cpu_sampler, "AdvisorySampler", Mock)
    monkeypatch.setattr(machine_metrics_sender, "MetricsSender", lambda *a: Mock())
    monkeypatch.setattr(machine_metrics_sender, "board_post", lambda ident: None)
    wanted = []
    def stub(owner, name, result):
        original = getattr(owner, name)
        @wraps(original)
        def replacement(*args, **kwargs):
            return result
        monkeypatch.setattr(owner, name, replacement)
        wanted.append("sync.step/" + la._callable_name(replacement))
    stub(client, "board_paths", [])
    stub(client, "sweep_gone_board_handles", None)
    stub(client.sync_timing, "select", [])
    stub(client, "deferred_repairs_due", [])
    stub(client.backlog, "due", [])
    stub(client.outbox_age, "after_round", [])
    stub(client.sync_timing, "report", None)
    stub(client.sync_timing, "wait", None)
    monkeypatch.setattr(client.outbox_age, "round_started", lambda: None)
    monkeypatch.setattr(client, "next_deferred_repair", lambda boards: None)
    monkeypatch.setattr(client, "sync_all_once", lambda *a, **kw: [])
    calls = iter([True, False])
    try:
        client._run_forever(lambda: next(calls), 1, Mock())
        rows = la.counters()
        assert {name for name in rows if name.startswith("sync.step/")} == (set(wanted) if enabled else set())
        if enabled:
            assert all(rows[name]["runs"] == 1 for name in wanted)
    finally:
        la.reset()
