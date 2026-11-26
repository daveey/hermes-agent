"""Payload-free phase evidence from the real quiet preflight handler."""
from __future__ import annotations

import logging

import pytest

from gateway import quiet_check_diagnostics as diagnostics
from gateway import quiet_control
from tests.gateway import test_quiet_control as helpers

env = helpers.env


def test_check_attributes_existing_read_phases_without_changing_refusal(env, caplog):
    env.runner._quiet_attested = True
    env.runner._quiet_code = helpers.CleanLedger(("plugin-review-unverified:fixture",))
    with caplog.at_level(logging.INFO, logger="gateway.quiet_check_diagnostics"):
        result = env.control.check({"schema": 1}, helpers.PEER)
    assert result["admitted"] is False
    assert result["reasons"] == ["unknown:code:plugin-review-unverified:fixture"]
    rows = [r for r in caplog.records if r.name == "gateway.quiet_check_diagnostics"]
    assert [r.args[3] for r in rows] == [
        "coverage", "registry_inventory", "update", "window", "route", "extension_report", "predicates"]
    assert len({r.args[:3] for r in rows}) == 1
    assert all(r.args[4] >= 0 and r.args[5] >= 0 for r in rows)
    assert all("fixture" not in r.getMessage() for r in rows)


def test_handler_times_the_actual_inventory_update_and_predicate_reads(env, monkeypatch, caplog):
    clocks = [20_000_000_000, 2_000_000_000]
    monkeypatch.setattr(diagnostics, "monotonic_ns", lambda: clocks[0])
    monkeypatch.setattr(diagnostics, "thread_time_ns", lambda: clocks[1])
    for method, wall, cpu in (("_pending", 6, .125), ("_registry_view", 4, .25),
                              ("_update_status", 3, .5), ("_predicates", 5, .75)):
        original = getattr(env.control, method)

        def delayed(*args, _original=original, _wall=wall, _cpu=cpu, **kwargs):
            value = _original(*args, **kwargs)
            clocks[0] += int(_wall * 1_000_000_000)
            clocks[1] += int(_cpu * 1_000_000_000)
            return value

        monkeypatch.setattr(env.control, method, delayed)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        result = helpers.ok(env, "quiet-observe-check", {"schema": 1})
    assert result["admitted"] is False and "coverage_pending" in result["reasons"]
    rows = {r.args[3]: r.args[4:] for r in caplog.records if r.name == diagnostics.__name__}
    assert rows == {"coverage": (6000.0, 125.0), "registry_inventory": (4000.0, 250.0),
                    "update": (3000.0, 500.0), "window": (0.0, 0.0), "route": (0.0, 0.0),
                    "extension_report": (0.0, 0.0), "predicates": (5000.0, 750.0)}


def test_phase_wall_and_thread_cpu_are_independent_and_exclude_logging(monkeypatch):
    clocks = [10_000_000_000, 1_000_000_000]
    monkeypatch.setattr(diagnostics, "monotonic_ns", lambda: clocks[0])
    monkeypatch.setattr(diagnostics, "thread_time_ns", lambda: clocks[1])
    rows = []

    def log(_format, *args):
        rows.append(args)
        clocks[0] += 9_000_000_000
        clocks[1] += 3_000_000_000

    monkeypatch.setattr(diagnostics.logger, "info", log)
    timer = diagnostics.CheckDiagnostics()
    for _ in range(2):
        with timer.phase("registry_inventory"):
            clocks[0] += 4_000_000_000
            clocks[1] += 250_000_000
    assert [row[4:] for row in rows] == [(4000.0, 250.0), (4000.0, 250.0)]
    assert rows[0][:3] == rows[1][:3]
    assert rows[0][2] == 10_000_000_000


def test_logging_failure_preserves_original_phase_exception(monkeypatch):
    def broken(*_args):
        raise OSError("test sink unavailable")

    monkeypatch.setattr(diagnostics.logger, "info", broken)
    original = ValueError("original read failed")
    with pytest.raises(ValueError) as raised:
        with diagnostics.CheckDiagnostics().phase("registry_inventory"):
            raise original
    assert raised.value is original


def test_logging_failure_does_not_change_successful_wire_result(env, monkeypatch):
    env.runner._quiet_attested = True
    before = helpers.ok(env, "quiet-observe-check", {"schema": 1})
    assert before["admitted"] is True

    def broken(*_args):
        raise OSError("test sink unavailable")

    monkeypatch.setattr(diagnostics.logger, "info", broken)
    assert helpers.ok(env, "quiet-observe-check", {"schema": 1}) == before


def test_no_registry_keeps_refusal_and_only_times_existing_reads(env, caplog):
    env.runner._quiet_registry = None
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        result = helpers.ok(env, "quiet-observe-check", {"schema": 1})
    assert result["admitted"] is False
    assert "registry_missing" in result["reasons"]
    assert [r.args[3] for r in caplog.records if r.name == diagnostics.__name__] == [
        "coverage", "route", "extension_report", "predicates"]


def test_start_does_not_emit_check_timing(env, caplog):
    helpers.attest(env)
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        result = helpers.ok(env, "quiet-observe-start", helpers.start_params(env))
    assert result["valid"] is True
    assert not [r for r in caplog.records if r.name == diagnostics.__name__]


def test_malformed_check_does_not_begin_measurement(env, caplog):
    with caplog.at_level(logging.INFO, logger=diagnostics.__name__):
        result = helpers.call(env, "quiet-observe-check", {"schema": 1, "extra": "not logged"})
    assert result["ok"] is False
    assert not [r for r in caplog.records if r.name == diagnostics.__name__]
