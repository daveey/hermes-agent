"""A core update decided past hermes-sync's cron bound cuts cron runs instead of waiting for them
(t_01a18dbde9f8b2f0).

hermes-sync stops counting in-flight cron runs as busy once its drain has waited CORE_DRAIN_TARGET_S minus the
apply budget, and marks that apply's update intent ``"cron_cut": true``. The gateway's restart wait otherwise
waits up to restart_after_turn_timeout (1800 s) for every cron run, so the update would still land past the
target (hermes-hub 2026-10-30 14:38 UTC: one apply blocked 30 min in that wait). Under such an intent, cron
runs (and confirmed Kanban cards) get the same short cap, and every cut run is logged with its job id and
start time; its next tick reruns it. A plain intent keeps waiting for cron (test_core_update_kanban_wait.py).
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import run_shutdown_restart_wait as wait
from tests.gateway.restart_test_helpers import make_restart_runner


def _virtual_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))

    async def advance(seconds):
        clock[0] += seconds
    monkeypatch.setattr(asyncio, 'sleep', advance)
    return clock


def _runner(tmp_path, monkeypatch, intent_body, *, cron=1, kanban=0, chat=0):
    runner, _ = make_restart_runner()
    intent = tmp_path / 'intent'
    intent.write_text(json.dumps(intent_body) + '\n')
    runner._restart_update_intent = intent
    runner._restart_after_turn_timeout = 1800
    total = cron + kanban + chat
    runner._active_work_count = runner._awaitable_work_count = lambda: total
    runner._restart_wait_cron_counts = lambda: {'awaitable': cron, 'wedged': 0, 'restart_safe': 0}
    runner._describe_active_work = list
    monkeypatch.setattr(wait, 'kanban_managed_count', lambda _: kanban, raising=False)
    from cron import scheduler
    monkeypatch.setattr(scheduler, 'get_running_job_details', lambda: [
        {'job_id': f'job{i}', 'elapsed_s': 600.0 + i, 'worker_pid': None, 'restart_safe': False}
        for i in range(cron)])
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize('kanban', [0, 2])
async def test_cron_cut_intent_caps_the_cron_wait_and_logs_each_cut(monkeypatch, tmp_path, caplog, kanban):
    runner = _runner(tmp_path, monkeypatch, {'pid': 1, 'kind': 'unfenced', 'cron_cut': True}, cron=2, kanban=kanban)
    clock = _virtual_clock(monkeypatch)
    with caplog.at_level('INFO', logger='gateway.run'):
        assert await runner._await_active_work_before_restart() is False
    assert 15 <= clock[0] < 15.2, f'waited {clock[0]}s for cron runs the update decided to cut'
    cut = [r.getMessage() for r in caplog.records if 'cutting cron run' in r.getMessage()]
    assert len(cut) == 2, cut
    assert 'job0' in cut[0] and 'started' in cut[0] and 'next tick reruns it' in cut[0]
    assert 'job1' in cut[1]


@pytest.mark.asyncio
async def test_plain_intent_still_waits_for_cron(monkeypatch, tmp_path):
    runner = _runner(tmp_path, monkeypatch, {'pid': 1, 'kind': 'unfenced'}, cron=1)
    clock = _virtual_clock(monkeypatch)
    assert await runner._await_active_work_before_restart() is False
    assert 1800 <= clock[0] < 1800.2


@pytest.mark.asyncio
async def test_cron_cut_never_cuts_a_chat_turn(monkeypatch, tmp_path):
    runner = _runner(tmp_path, monkeypatch, {'pid': 1, 'kind': 'unfenced', 'cron_cut': True}, cron=1, chat=1)
    clock = _virtual_clock(monkeypatch)
    assert await runner._await_active_work_before_restart() is False
    assert 1800 <= clock[0] < 1800.2, 'a chat turn keeps its full wait even when cron may be cut'


def test_unreadable_intent_does_not_cut_cron(tmp_path):
    runner, _ = make_restart_runner()
    intent = tmp_path / 'intent'
    intent.write_text('not json')
    assert runner._intent_cuts_cron(intent) is False
    assert runner._intent_cuts_cron(None) is False
    intent.write_text(json.dumps({'pid': 1, 'cron_cut': True}))
    assert runner._intent_cuts_cron(intent) is True


@pytest.mark.parametrize('cron_cut, leash', [(True, wait.CORE_UPDATE_KANBAN_STOP_LEASH_S), (False, None)])
def test_stop_leash_after_a_cron_cut_covers_the_cut_runs(monkeypatch, tmp_path, cron_cut, leash):
    """After the capped wait, stop() takes the short leash over the cut cron runs instead of a second full
    drain; a plain intent keeps the normal stop budget while a cron run is in flight."""
    body = {'pid': 1, 'kind': 'unfenced', **({'cron_cut': True} if cron_cut else {})}
    runner = _runner(tmp_path, monkeypatch, body, cron=1, kanban=1)
    runner._restart_requested = True
    runner._core_update_kanban_cap_spent = True
    runner._core_update_kanban_snapshot_stop_leash()
    assert runner._stop_kanban_leash_s == leash
