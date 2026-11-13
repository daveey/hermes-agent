"""Quiet-v3 local-work coverage of agent-turn lifetime seams, driven through real production code.

Only external effects (model/provider, subprocess handle, lease admission) are faked. The registry is a
real ``hermes_cli.local_work.Registry`` installed through its own global; no counter is seeded.
"""
import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import local_work
from tests.waiting import wait_until


def _home():
    from hermes_constants import get_hermes_home
    return str(get_hermes_home())


@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    registry = local_work.Registry(homes=[_home()])
    monkeypatch.setattr(local_work, '_registry', registry)
    return registry


def _clean(registry):
    snap = registry.snapshot()
    assert snap.unknowns == (), snap.unknowns
    assert snap.active == 0, dict(snap.per_kind)


# ---------------------------------------------------------------- row 1: agent turn

def test_turn_exception_before_lease_admit_releases_exactly_once(reg, monkeypatch):
    from agent import turn_facade, turn_facade_lease, background_review

    seen = {}

    def admit(agent, **kwargs):
        seen['active_during_admit'] = reg.snapshot().active
        raise RuntimeError('pre-agent failure')

    monkeypatch.setattr(turn_facade_lease, 'admit_durable_turn_lease', admit)
    monkeypatch.setattr(background_review, 'cancel_background_review_for_live_turn', lambda agent: None)

    class FakeAgent(turn_facade.TurnFacadeMixin):
        session_id = 's'
        platform = 'test'

    epoch0 = reg.snapshot().epoch
    with pytest.raises(RuntimeError, match='pre-agent failure'):
        FakeAgent().run_conversation('hi')
    assert seen['active_during_admit'] == 1, 'the turn scope must open before the durable lease admits'
    assert reg.snapshot().epoch > epoch0
    _clean(reg)  # exactly once: a second finish would latch unmatched-finish


def test_turn_scope_spans_cancel_review_fence_and_nested_work(reg, monkeypatch):
    """Reserve is the first statement: even the background-review fence runs inside the scope."""
    from agent import turn_facade, background_review

    seen = []
    monkeypatch.setattr(background_review, 'cancel_background_review_for_live_turn',
                        lambda agent: (seen.append(reg.snapshot().active), (_ for _ in ()).throw(KeyError('stop')))[0])

    class FakeAgent(turn_facade.TurnFacadeMixin):
        session_id = 's'

    with pytest.raises(KeyError):
        FakeAgent().run_conversation('hi')
    assert seen == [1]
    _clean(reg)


def test_turn_without_registry_is_inert(monkeypatch):
    from agent import turn_facade, background_review
    monkeypatch.setattr(local_work, '_registry', None)
    monkeypatch.setattr(background_review, 'cancel_background_review_for_live_turn',
                        lambda agent: (_ for _ in ()).throw(KeyError('stop')))

    class FakeAgent(turn_facade.TurnFacadeMixin):
        session_id = 's'

    with pytest.raises(KeyError):
        FakeAgent().run_conversation('hi')


# ---------------------------------------------------------------- row 12: background review + requeue

def _review_agent(registry, monkeypatch, *, cancelled, observed):
    import run_agent
    from agent import background_review, review_idle_queue
    from hermes_cli import backend_retirement

    review_run = SimpleNamespace(cancel_requested=threading.Event(), retirement_hold=None)
    if cancelled:
        review_run.cancel_requested.set()
    monkeypatch.setattr(background_review, 'prepare_background_review_run', lambda agent: review_run)
    monkeypatch.setattr(background_review, 'finish_background_review_run', lambda agent, run: None)
    monkeypatch.setattr(background_review, 'spawn_background_review_thread',
                        lambda *a, **k: (lambda: observed.append(('target', registry.snapshot().active)), 'p'))
    monkeypatch.setattr(backend_retirement.retirement, 'begin_background_review', lambda *a, **k: object())
    monkeypatch.setattr(backend_retirement.retirement, 'end_background_review', lambda hold: None)
    monkeypatch.setattr(run_agent, '_review_should_defer', lambda agent, cfg: True)
    monkeypatch.setattr(run_agent, '_review_queue_key', lambda agent: 'k')
    queue = review_idle_queue.ReviewIdleQueue()
    monkeypatch.setattr(review_idle_queue, 'QUEUE', queue)
    monkeypatch.setattr(queue, '_ensure_thread', lambda: None)

    real_enqueue = queue.enqueue

    def spy(agent, key, kwargs):
        observed.append(('requeue', registry.snapshot().active))
        real_enqueue(agent, key, kwargs)
    monkeypatch.setattr(queue, 'enqueue', spy)
    agent = SimpleNamespace()
    agent._maybe_requeue_preempted_review = run_agent.AIAgent._maybe_requeue_preempted_review.__get__(agent)
    agent._REVIEW_REQUEUE_MAX_ATTEMPTS = 3
    agent._spawn_background_review_run = run_agent.AIAgent._spawn_background_review_run.__get__(agent)
    return agent, queue


def test_background_review_requeue_has_no_zero_gap(reg, monkeypatch):
    import run_agent
    observed = []
    agent, queue = _review_agent(reg, monkeypatch, cancelled=True, observed=observed)
    done = threading.Event()
    real_release_probe = reg.finish

    def finish(token):
        result = real_release_probe(token)
        if reg.snapshot().active == 0:
            done.set()
        return result
    monkeypatch.setattr(reg, 'finish', finish)

    run_agent.AIAgent._spawn_background_review_now.__get__(agent)([{'role': 'user', 'content': 'x'}], review_memory=True)
    assert done.wait(10), 'the review thread never finished'
    kinds = [k for k, _ in observed]
    assert kinds == ['target', 'requeue'], observed
    assert all(active >= 1 for _, active in observed), observed  # requeue ran while the predecessor still held
    assert queue.pending_count() == 1, 'the preempted review was requeued'
    _clean(reg)


def test_background_review_early_exit_and_start_failure_release_once(reg, monkeypatch):
    import run_agent
    from agent import background_review
    observed = []
    agent, _queue = _review_agent(reg, monkeypatch, cancelled=False, observed=observed)
    monkeypatch.setattr(background_review, 'prepare_background_review_run', lambda a: None)  # not admitted
    run_agent.AIAgent._spawn_background_review_now.__get__(agent)([], review_memory=True)
    _clean(reg)

    monkeypatch.setattr(background_review, 'prepare_background_review_run',
                        lambda a: SimpleNamespace(cancel_requested=threading.Event()))

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(run_agent.threading, 'Thread', Boom)
    with pytest.raises(RuntimeError):
        run_agent.AIAgent._spawn_background_review_now.__get__(agent)([], review_memory=True)
    _clean(reg)


def test_idle_queue_item_reserves_at_pop_and_empty_poll_does_not(reg, monkeypatch):
    from agent import review_idle_queue
    queue = review_idle_queue.ReviewIdleQueue()
    queue._server_idle = lambda: True
    queue._now = lambda: 1e9  # aged: dispatches regardless of idleness
    before = reg.snapshot().epoch
    for _ in range(5):  # empty polls
        assert queue._pop_dispatchable() is None
    assert reg.snapshot().epoch == before

    spawned = []
    agent = SimpleNamespace(_spawn_background_review_now=lambda **kw: spawned.append(reg.snapshot().active))
    queue._pending['k'] = review_idle_queue._PendingReview(agent, 'k', {'task_cfg': None}, 0.0,
                                                           __import__('contextvars').copy_context())
    monkeypatch.setattr(queue, '_still_enabled', lambda item: True)
    item = queue._pop_dispatchable()
    assert item is not None and reg.snapshot().active == 1, 'selected item is tracked atomically with the pop'
    item.context.run(queue._dispatch, item)
    assert spawned == [1]
    item.work.release()
    _clean(reg)


# ---------------------------------------------------------------- row 13: memory manager

def test_memory_job_canceled_before_run_releases_once(reg):
    from agent.memory_manager import MemoryManager
    manager = MemoryManager()
    gate, started = threading.Event(), threading.Event()

    def blocker():
        started.set()
        gate.wait(10)
    manager._submit_background(blocker)
    assert started.wait(10)
    manager._submit_background(lambda: None, kind='prefetch')  # queued behind the blocker
    queued = [f for f in manager._background_futures if not f.running() and not f.done()]
    assert len(queued) == 1 and reg.snapshot().active == 2
    assert queued[0].cancel()
    assert reg.snapshot().active == 1, 'canceled-before-run released its token exactly once'
    gate.set()
    wait_until(lambda: reg.snapshot().active == 0, what='the running job to release')
    manager._drain_sync_executor()
    _clean(reg)


def test_memory_public_entries_are_scoped_and_short_job_bumps_epoch(reg):
    from agent.memory_manager import MemoryManager
    manager = MemoryManager()
    seen = []
    manager._each_provider = lambda *a, **k: seen.append(reg.snapshot().active) or []
    epoch = reg.snapshot().epoch
    manager.prefetch_all('please recall the thing', session_id='s')
    assert seen == [1] and reg.snapshot().active == 0
    assert reg.snapshot().epoch >= epoch + 2  # sampled count is 0 before and after, epoch still moved
    done = threading.Event()
    epoch = reg.snapshot().epoch
    manager._submit_background(done.set)
    assert done.wait(10)
    wait_until(lambda: reg.snapshot().active == 0, what='the short job to release')
    assert reg.snapshot().epoch > epoch
    manager._drain_sync_executor()
    _clean(reg)


# ---------------------------------------------------------------- authority selected-row lifetime

def _local_authority(tmp_path, monkeypatch):
    from gateway.config import Platform
    from gateway import session_local_recovery
    from tests.gateway.test_session_authority_cancel_settlement import _authority
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.LOCAL)
    monkeypatch.setattr(session_local_recovery, 'restore_local_session', lambda authority, sid: None)
    monkeypatch.setattr(session_local_recovery, 'reopen_local_session', lambda authority, ref: None)
    return db, authority


@pytest.mark.asyncio
async def test_selected_row_reserves_before_preflight_and_empty_poll_does_not(tmp_path, monkeypatch):
    from gateway import session_local_mcp
    from gateway.session_authority import _Attempt
    from tests.gateway.test_session_authority_cancel_settlement import REF, _submit
    db, authority = _local_authority(tmp_path, monkeypatch)
    registry = local_work.Registry(homes=[_home()])
    monkeypatch.setattr(local_work, '_registry', registry)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    live = authority.sessions['s']
    with db:
        epoch = registry.snapshot().epoch
        for _ in range(5):  # a full simulated capture of empty polls
            row, first = await authority._claim_next(REF, live)
            assert (row, first) == (None, None)
        assert registry.snapshot().epoch == epoch, 'empty polls must not change the epoch'

        probed = []

        async def refreeze(authority, ref, live):
            probed.append(registry.snapshot().active)
        monkeypatch.setattr(session_local_mcp, 'refreeze_editor_mcp', refreeze)
        await _submit(authority, 'work')
        attempt = _Attempt()
        row, _ = await authority._claim_next(REF, live, attempt)
    assert probed == [1], 'the selected row holds a token before refreeze/reopen preflight'
    assert row and len(attempt.work) == 1 and registry.snapshot().active == 1, 'token transfers on success'
    attempt.work[0].release()
    _clean(registry)


@pytest.mark.asyncio
async def test_refused_selected_head_releases_its_token(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError
    from tests.gateway.test_session_authority_cancel_settlement import REF, _submit
    db, authority = _local_authority(tmp_path, monkeypatch)
    registry = local_work.Registry(homes=[_home()])
    monkeypatch.setattr(local_work, '_registry', registry)
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    live = authority.sessions['s']
    with db:
        await _submit(authority, 'refused')
        live.source = SimpleNamespace(platform=live.source.platform, user_id='someone-else')
        epoch = registry.snapshot().epoch
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await authority._claim_next(REF, live)
    assert registry.snapshot().epoch > epoch, 'the selected attempt was observable'
    _clean(registry)


@pytest.mark.asyncio
async def test_drain_holds_the_claimed_token_through_execution_and_settlement(tmp_path, monkeypatch):
    from gateway import session_finite
    from tests.gateway.test_session_authority_cancel_settlement import REF, _submit
    from tests.waiting import async_wait_until
    db, authority = _local_authority(tmp_path, monkeypatch)
    registry = local_work.Registry(homes=[_home()])
    monkeypatch.setattr(local_work, '_registry', registry)
    during = []

    async def execute(authority, ref, row):
        during.append(registry.snapshot().active)
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        await _submit(authority, 'work')
        await async_wait_until(lambda: during, timeout=10, what='the claimed row to execute')
        live = authority.sessions['s']
        await async_wait_until(lambda: live.task is None or live.task.done(), timeout=10, what='drain exit')
    assert during == [1]
    _clean(registry)


# ---------------------------------------------------------------- managed child lifetime

class _Pipe:
    def close(self):
        pass

    def write(self, data):
        pass

    def flush(self):
        pass


class _FakeChild:
    """Subprocess handle double: ``alive`` models whether death can be proven (poll)."""

    def __init__(self, alive):
        self.pid = 4194999
        self.alive = alive
        self.stdin, self.stdout, self.stderr = _Pipe(), _Pipe(), None

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        if self.alive:
            raise subprocess.TimeoutExpired('child', timeout)
        return 0

    def kill(self):
        pass

    terminate = kill


@pytest.mark.asyncio
async def test_managed_child_reserves_before_spawn_and_releases_after_verified_exit(reg, monkeypatch):
    import os
    from gateway.session_managed_worker import ManagedWorker
    from gateway.session_worker_spawn import acquire_process
    monkeypatch.setattr(os, 'getpgid', lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    at_spawn = []
    child = _FakeChild(alive=False)

    def spawn(*a, **k):
        at_spawn.append(reg.snapshot().active)
        return child
    epoch = reg.snapshot().epoch
    process = await acquire_process(spawn)
    assert at_spawn == [1] and reg.snapshot().active == 1, 'reserved before the off-loop creation'
    assert reg.snapshot().epoch > epoch
    worker = ManagedWorker(process)
    # closed / popped-from-map is not proof: nothing released until close() verifies exit.
    worker.closed.set()
    assert reg.snapshot().active == 1
    await asyncio.to_thread(worker.close)
    _clean(reg)
    await asyncio.to_thread(worker.close)  # a second close cannot double release
    _clean(reg)


@pytest.mark.asyncio
async def test_managed_child_whose_death_cannot_be_proven_keeps_token_and_unknown(reg, monkeypatch):
    import os
    from gateway.session_managed_worker import ManagedWorker
    from gateway.session_worker_spawn import acquire_process
    monkeypatch.setattr(os, 'getpgid', lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    process = await acquire_process(lambda *a, **k: _FakeChild(alive=True))
    worker = ManagedWorker(process)
    await asyncio.to_thread(worker.close)
    snap = reg.snapshot()
    assert snap.active == 1, 'unproven death must stay nonzero'
    assert 'managed-child-death-unproven' in snap.unknowns


@pytest.mark.asyncio
async def test_failed_spawn_and_cancelled_prespawn_release(reg):
    from gateway.session_worker_spawn import acquire_process

    def failing(*a, **k):
        raise OSError('exec failed')
    with pytest.raises(OSError):
        await acquire_process(failing)
    _clean(reg)


# ---------------------------------------------------------------- delegation / cron / title

def test_delegation_worker_cancelled_before_run_releases_once_and_short_worker_bumps_epoch(reg):
    from concurrent.futures import ThreadPoolExecutor
    from agent import local_work_agent
    executor = ThreadPoolExecutor(max_workers=1)
    gate, started = threading.Event(), threading.Event()
    try:
        first = local_work_agent.submit(executor, lambda: (started.set(), gate.wait(10)),
                                        kind='delegation_worker', source='test')
        assert started.wait(10)
        second = local_work_agent.submit(executor, lambda: None, kind='delegation_worker', source='test')
        assert second.cancel() and reg.snapshot().active == 1
        gate.set()
        first.result(10)
        wait_until(lambda: reg.snapshot().active == 0, what='worker release')
        epoch = reg.snapshot().epoch
        local_work_agent.submit(executor, lambda: None, kind='delegation_worker', source='test').result(10)
        wait_until(lambda: reg.snapshot().active == 0, what='short worker release')
        assert reg.snapshot().epoch >= epoch + 2  # <1 s worker: count sampled 0, epoch still advanced
    finally:
        executor.shutdown(wait=True)
    _clean(reg)


def test_title_upgrade_is_tracked_even_outside_a_turn_scope(reg, monkeypatch):
    from agent import title_generator
    gate, started = threading.Event(), threading.Event()
    monkeypatch.setattr(title_generator, 'auto_title_session',
                        lambda *a, **k: (started.set(), gate.wait(10)))
    monkeypatch.setattr(title_generator, 'is_titleable_user_message', lambda m: True)
    monkeypatch.setattr(title_generator, '_auto_title_enabled', lambda: True)
    monkeypatch.setattr(title_generator, 'apply_instant_title', lambda *a, **k: None)
    monkeypatch.setattr(title_generator, '_model_title_upgrade_enabled', lambda: True)
    monkeypatch.setattr(title_generator, '_kanban_task_title', lambda: None)
    monkeypatch.setattr(title_generator, 'title_upgrade_must_wait_for_turn', lambda rt: False)
    upgrade = title_generator.maybe_auto_title(SimpleNamespace(), 's', 'hello there')
    assert started.wait(10) and reg.snapshot().active == 1
    gate.set()
    upgrade.join(10)
    wait_until(lambda: reg.snapshot().active == 0, what='title thread release')
    _clean(reg)
