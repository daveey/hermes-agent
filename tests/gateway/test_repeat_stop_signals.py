"""[daveey] Repeat stop signals during one shutdown count as one (defect gateway-cc28b19bca).

Under launchd the gateway can get the same SIGTERM from several forwarders in one stop (launchd's group
signal, the signed wrapper, the core-update launcher, stderr_timestamp). Each used to start its own
shutdown pass: another forensics snapshot + detached diagnostic, another deadline arm and another
``runner.stop()`` task.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from types import SimpleNamespace

import gateway.run as run_mod


def test_three_sigterms_start_one_shutdown(monkeypatch, caplog):
    arms, diags = [], []
    monkeypatch.setattr(run_mod.exit_deadline, "arm_for_signal", lambda runner: arms.append(1))
    import gateway.shutdown_forensics as forensics
    monkeypatch.setattr(forensics, "spawn_async_diagnostic", lambda *a, **k: diags.append(1))

    stops = []

    async def stop():
        stops.append(1)

    runner = SimpleNamespace(_signal_initiated_shutdown=False, stop=stop)
    flag = [False]
    handler = run_mod._start_gateway_make_shutdown_signal_handler(runner, flag)

    async def main():
        for _ in range(3):
            handler(signal.SIGTERM)
        for _ in range(3):
            await asyncio.sleep(0)  # let the stop() task(s) run

    with caplog.at_level(logging.INFO, logger=run_mod.logger.name):
        asyncio.run(main())

    assert stops == [1]
    assert arms == [1]
    assert diags == [1]
    assert flag[0] is True
    assert runner._stop_signal_count == 3
    repeats = [r.getMessage() for r in caplog.records if "again during shutdown" in r.getMessage()]
    assert len(repeats) == 2 and "#3" in repeats[-1]


def test_signal_exit_record_carries_the_repeat_count(monkeypatch):
    """The unplanned-signal record (what gateway.exit_signal logs) says how many copies one stop absorbed."""
    monkeypatch.setattr(run_mod.exit_deadline, "arm_for_signal", lambda runner: None)
    import gateway.shutdown_forensics as forensics
    monkeypatch.setattr(forensics, "spawn_async_diagnostic", lambda *a, **k: None)

    async def stop():
        return None

    runner = SimpleNamespace(_signal_initiated_shutdown=False, stop=stop)
    handler = run_mod._start_gateway_make_shutdown_signal_handler(runner, [False])

    async def main():
        handler(signal.SIGTERM)
        assert "stop_signals" not in runner._unplanned_signal
        handler(signal.SIGTERM)
        handler(signal.SIGTERM)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert runner._unplanned_signal["signal"] == "SIGTERM"
    assert runner._unplanned_signal["stop_signals"] == 3


def test_start_refused_record_flags_an_external_supervisor(monkeypatch):
    """The coach collector keys on this flag: a live owner refusing a supervised start is this defect."""
    import sys

    from gateway import run_exit_reason
    from hermes_cli.gateway_exit_diag import nonzero_exit_record
    import gateway.run_shutdown as rs
    monkeypatch.setattr(rs, "_last_signal_exit", None)
    monkeypatch.setattr(run_exit_reason, "_last_start_refusal",
                        {"reason": "host_refused", "owner_pid": 97265, "owner_alive": True})
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run", "--replace", "--external-supervisor"])
    tag, rec = nonzero_exit_record()
    assert tag == "gateway.start_refused" and rec["external_supervisor"] is True and rec["owner_alive"] is True
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    assert "external_supervisor" not in nonzero_exit_record()[1]
