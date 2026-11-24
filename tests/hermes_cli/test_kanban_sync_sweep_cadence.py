"""Loop-only cadence, preserved across tenures; housekeeping remains unconditional."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_sync_client as client


def _cadence():
    from hermes_cli import kanban_sync_sweep_cadence
    return kanban_sync_sweep_cadence


def test_unchanged_list_waits_thirty_seconds():
    cadence = _cadence()
    cadence.reset()
    boards = [("b", Path("/b")), ("a", Path("/a"))]
    assert cadence.due(boards, 100)
    assert not cadence.due(list(reversed(boards)), 101)
    assert not cadence.due(boards, 129.999)
    assert cadence.due(boards, 130)
    assert not cadence.due(boards, 131)


def test_list_change_sweeps_inside_window():
    cadence = _cadence()
    cadence.reset()
    assert cadence.due([("a", Path("/a"))], 100)
    assert cadence.due([], 101)
    assert cadence.due([("a", Path("/a"))], 102)
    assert cadence.due([("a", Path("/replacement"))], 103)
    assert not cadence.due([("a", Path("/replacement"))], 104)


def _loop(monkeypatch):
    from hermes_cli import kanban_sync_schema_alert, kanban_sync_links_pass
    from hermes_cli import machine_metrics, machine_cpu_sampler, machine_metrics_sender
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
    monkeypatch.setattr(client, "board_paths", list)
    monkeypatch.setattr(client, "deferred_repairs_due", lambda *a: [])
    monkeypatch.setattr(client.backlog, "due", lambda *a: [])
    monkeypatch.setattr(client.outbox_age, "after_round", lambda *a: None)
    monkeypatch.setattr(client.outbox_age, "round_started", lambda: None)
    monkeypatch.setattr(client.sync_timing, "report", lambda *a: None)
    monkeypatch.setattr(client.sync_timing, "wait", lambda *a: None)
    monkeypatch.setattr(client, "next_deferred_repair", lambda *a: None)
    monkeypatch.setattr(client, "sync_all_once", lambda *a, **kw: [])
    monkeypatch.setattr(client.time, "monotonic", lambda: 100.0)


def test_two_rounds_and_new_tenure_sweep_once(monkeypatch):
    _cadence().reset()
    _loop(monkeypatch)
    sweep = Mock()
    monkeypatch.setattr(client, "sweep_gone_board_handles", sweep)
    for _ in range(2):
        calls = iter([True, True, False])
        client._run_forever(lambda: next(calls), 1, Mock())
    assert sweep.call_count == 1


@pytest.mark.parametrize("raises", [False, True])
def test_loop_clears_diagnostic_flag_on_every_exit(monkeypatch, raises):
    from hermes_cli import kanban_sync_probe_diag as diag
    _cadence().reset()
    _loop(monkeypatch)
    monkeypatch.setattr(client, "sweep_gone_board_handles", lambda: None)
    seen = []
    def run(*a, **kw):
        seen.append(diag.active())
        if raises:
            raise RuntimeError("round failed")
        return []
    monkeypatch.setattr(client, "sync_all_once", run)
    calls = iter([True, False])
    if raises:
        with pytest.raises(RuntimeError, match="round failed"):
            client._run_forever(lambda: next(calls), 1, Mock())
    else:
        client._run_forever(lambda: next(calls), 1, Mock())
    assert seen == [True]
    assert not diag.active()
