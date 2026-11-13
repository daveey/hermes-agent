"""Quiet dispatcher ticks must not decode capability config/state repeatedly."""
from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_sweep_gate as gate


@pytest.fixture
def refresh_state(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"schema": reg.SCHEMA, "refreshed_at": 1000}))
    monkeypatch.setattr(reg, "registry_path", lambda: path)
    config = {"refresh_seconds": 120}
    load = Mock(side_effect=lambda: dict(config))
    monkeypatch.setattr(reg, "_config", load)
    revision = [1]
    monkeypatch.setattr(gate, "config_key", lambda: revision[0])
    monkeypatch.setattr(reg, "_refresh_gate", None, raising=False)
    monkeypatch.setattr(reg, "_thread", None)
    read = Mock(wraps=reg.read_local)
    monkeypatch.setattr(reg, "read_local", read)
    thread = Mock()
    thread.is_alive.return_value = False
    factory = Mock(return_value=thread)
    monkeypatch.setattr(reg.threading, "Thread", factory)
    return path, config, load, read, revision, factory


def test_quiet_ticks_read_once_and_start_at_exact_due(refresh_state):
    _, _, load, read, _, factory = refresh_state
    assert reg.maybe_refresh_async(now=1000) is False
    assert reg.maybe_refresh_async(now=1119) is False
    assert (load.call_count, read.call_count) == (1, 1)
    assert reg.maybe_refresh_async(now=1120) is True
    factory.return_value.start.assert_called_once()


def test_config_change_invalidates_future_due(refresh_state):
    _, config, _, _, revision, factory = refresh_state
    assert reg.maybe_refresh_async(now=1000) is False
    config["refresh_seconds"] = 60
    revision[0] += 1
    assert reg.maybe_refresh_async(now=1060) is True
    factory.return_value.start.assert_called_once()


def test_external_registry_replacement_invalidates_due(refresh_state):
    path, _, _, read, _, factory = refresh_state
    assert reg.maybe_refresh_async(now=1000) is False
    replacement = path.with_suffix(".replacement")
    replacement.write_text(json.dumps({"schema": reg.SCHEMA, "refreshed_at": 800}))
    replacement.replace(path)
    assert reg.maybe_refresh_async(now=1001) is True
    assert read.call_count == 2
    factory.return_value.start.assert_called_once()


def test_running_refresh_skips_reads_and_failed_refresh_retries(refresh_state):
    _, _, load, read, _, factory = refresh_state
    assert reg.maybe_refresh_async(now=1120) is True
    thread = factory.return_value
    thread.is_alive.return_value = True
    calls = load.call_count, read.call_count
    assert reg.maybe_refresh_async(now=1121) is False
    assert (load.call_count, read.call_count) == calls
    # Failure left the stale registry intact: next tick must retry, not cache success.
    thread.is_alive.return_value = False
    assert reg.maybe_refresh_async(now=1122) is True
    assert thread.start.call_count == 2


def test_disabled_refresh_reenabled_by_config_change(refresh_state):
    _, config, load, read, revision, factory = refresh_state
    config["enabled"] = False
    assert reg.maybe_refresh_async(now=1000) is False
    assert reg.maybe_refresh_async(now=1001) is False
    assert load.call_count == 1
    assert read.call_count == 0
    config["enabled"] = True
    revision[0] += 1
    assert reg.maybe_refresh_async(now=1120) is True
    factory.return_value.start.assert_called_once()


def test_clock_rollback_rechecks_record(refresh_state):
    _, _, load, _, _, _ = refresh_state
    assert reg.maybe_refresh_async(now=1050) is False
    assert reg.maybe_refresh_async(now=1000) is False
    assert load.call_count == 2
