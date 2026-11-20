"""Drain diagnostics preserve unknowns and old-Board load reporting."""
import json

import pytest

from agent import core_update_drain as drain
from hermes_cli import machine_drain_metrics as md
from hermes_cli import machine_metrics as mm
from hermes_cli.kanban_sync_client import HubError


@pytest.fixture
def hold(tmp_path, monkeypatch):
    path = tmp_path / "drain-hold"
    monkeypatch.setattr(drain, "hold_paths", lambda: [path])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(drain.time, "time", lambda: 200)
    return path


def test_actual_producer_bound_and_dead_writer(hold):
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, "max_s": 1200}))
    assert md.collect() == {"drain_active": 1, "drain_until_ms": 1300000}
    hold.write_text(json.dumps({"pid": 124, "started_at": 100, "max_s": 1200}))
    assert md.collect() == {"drain_active": 0}


@pytest.mark.parametrize("extra", [{}, {"max_s": "1200"}])
def test_never_invents_end(hold, extra):
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, **extra}))
    assert md.collect() == {"drain_active": 1}


@pytest.mark.parametrize("max_s", [None, -1, 0, "bad", float("nan"), float("inf")])
def test_invalid_lease_is_inactive(hold, max_s):
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, "max_s": max_s}))
    assert md.collect() == {"drain_active": 0}


@pytest.mark.parametrize("now", [1300, 1301])
def test_expired_live_writer_is_inactive(hold, monkeypatch, now):
    monkeypatch.setattr(drain.time, "time", lambda: now)
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, "max_s": 1200}))
    assert md.collect() == {"drain_active": 0}


def test_absent_and_unparseable(hold, monkeypatch):
    assert md.collect() == {"drain_active": 0}
    hold.write_text("{")
    monkeypatch.setattr(drain.time, "time", lambda: hold.stat().st_mtime)
    assert md.collect() == {"drain_active": 1}


def test_unreadable_is_unknown(hold, monkeypatch):
    monkeypatch.setattr(type(hold), "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert md.collect() == {}


def test_stat_failure_is_unknown(hold, monkeypatch):
    hold.write_text(json.dumps({"pid": 123}))
    original = type(hold).stat
    def stat(path, *args, **kwargs):
        if path == hold:
            raise PermissionError("unreadable metadata")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(hold), "stat", stat)
    assert md.collect() == {}
    assert drain.read_hold(hold) is None  # scheduler compatibility


def test_reporter_falls_back_without_discarding_capacity():
    sent = []
    sample = {"cpu_load1": 1, "active_card_runs": 0, "drain_active": 1, "drain_until_ms": 1234}
    def send(value):
        sent.append(value)
        if "drain_active" in value:
            raise HubError(400, "unknown metric")
    assert mm.Reporter(send, collect_fn=lambda: sample).report_due()
    assert sent == [sample, {"cpu_load1": 1, "active_card_runs": 0}]


def test_unknown_claiming_drops_only_itself_and_keeps_counters():
    """An older Board that rejects ``claiming`` must not disable counters or drain."""
    sent, rejected = [], []
    cpu = {"cpu_busy_pct": 12.5, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": 1}
    counters = {"tokens_io_total": 45, "tokens_cache_total": 310,
                "sessions_started_total": 3, "sessions_ended_total": 2}
    sample = {"cpus": 4, "cpu_load1": 1.0, "active_card_runs": 0,
              "drain_active": 0, "claiming": 1, **cpu, **counters}

    def board(value):
        sent.append(dict(value))
        if "claiming" in value:
            raise HubError(400, "unknown field(s): claiming")

    md.send_compatible(board, sample, lambda: rejected.append(True))
    assert rejected == []
    assert len(sent) == 2
    assert "claiming" not in sent[1]
    assert "cpu_busy_pct" not in sent[1]
    assert sent[1]["drain_active"] == 0
    assert sent[1]["tokens_io_total"] == 45


def test_claiming_rejection_still_walks_older_board_generations():
    """Dropping claiming once must not skip the counter and drain downgrades."""
    sent, rejected = [], []
    sample = {"cpus": 4, "drain_active": 0, "claiming": 0,
              "cpu_busy_pct": 1, "tokens_io_total": 9, "tokens_by_model": {"m": 1}}

    def ancient(value):
        sent.append(dict(value))
        if set(value) - {"cpus"}:
            raise HubError(400, "unknown field(s): " + ",".join(sorted(set(value) - {"cpus"})))

    md.send_compatible(ancient, sample, lambda: rejected.append(True))
    assert "claiming" not in sent[1] and "cpu_busy_pct" not in sent[1]
    assert "tokens_by_model" in sent[1]  # claiming drops alone; the older map stays for its own step
    assert "tokens_by_model" not in sent[2]
    assert rejected == [True]
    assert sent[-1] == {"cpus": 4}


def test_no_retry_of_other_board_errors():
    sent = []
    def send(value):
        sent.append(value)
        raise HubError(500, "failure")
    assert not mm.Reporter(send, collect_fn=lambda: {"drain_active": 1}).report_due()
    assert len(sent) == 1
