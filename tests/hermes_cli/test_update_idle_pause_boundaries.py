"""Boundary regressions for pre-swap idle pause review findings."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import control_socket
from hermes_cli import update_cmd_posix_pause as pause, update_pause_record as record


def test_idle_client_waits_past_gateway_callback_deadline(monkeypatch, tmp_path):
    query = Mock(return_value={"pausing": True})
    monkeypatch.setattr(control_socket, "query_gateway_control", query)
    control_socket.pause_gateway_for_update(tmp_path, idle_only=True)
    assert query.call_args.kwargs["timeout"] > 5.0
    assert query.call_args.kwargs["report_unsupported"] is True


@pytest.mark.platforms("posix")
def test_old_gateway_refusal_has_no_restart_debt(tmp_path, monkeypatch):
    server = control_socket.GatewayControlServer(home=tmp_path)
    monkeypatch.setattr(control_socket, "_query_unix_socket", lambda home, request, timeout: server.handle_request_line(request))
    entry = {"pid": 4242, "home": str(tmp_path), "kind": "systemd", "unit": "old.service"}
    token = {**pause._empty_token(), "posix_units": [entry], "stop_sent": []}
    monkeypatch.setattr(record, "write", lambda *a, **k: None)
    with pytest.raises(pause.PauseRefused, match="does not support idle pause"):
        pause._stop_gateways(token, [entry])
    assert token["stop_sent"] == []
    assert token["posix_units"] == []


@pytest.mark.platforms("posix")
def test_busy_later_gateway_settles_earlier_accepted_stop(monkeypatch):
    entries = [{"pid": 4242, "home": "/fixture/a"}, {"pid": 4243, "home": "/fixture/b"}]
    now = [0.0]
    monkeypatch.setattr(pause, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)))
    monkeypatch.setattr(pause, "_ask_to_drain", lambda e: e["pid"] == 4242)
    monkeypatch.setattr(pause, "_alive", lambda *a: now[0] < 0.4)
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *a: None)
    disarm = Mock()
    monkeypatch.setattr(pause, "_disarm", disarm)
    with pytest.raises(pause.PauseRefused, match="busy"):
        pause._stop_gateways({}, entries)
    disarm.assert_called_once_with(entries[0])
    assert 0.4 <= now[0] < 1.0


def test_nonexecuting_churn_gate_does_not_pause_but_stash_does(monkeypatch):
    from hermes_cli.update_cmd_pause_gate import _moves_for
    request = Mock(return_value="busy")
    monkeypatch.setattr(pause, "pause_at_commit_point", request)
    with _moves_for(None, pause_gate=False)(paths=["package-lock.json"]):
        pass
    request.assert_not_called()
    with pytest.raises(SystemExit) as refused:
        with _moves_for(None)(revert=True):
            pytest.fail("mutation ran past refusal")
    assert refused.value.code == 1


def test_no_restart_run_clears_previous_pause_verdict(monkeypatch):
    monkeypatch.setattr(pause, "_RUN", {"posix_pause_verdict": "busy"})
    assert pause.arm_pause(no_gateway_restart=True) is None
    assert pause.pause_at_commit_point() is None
