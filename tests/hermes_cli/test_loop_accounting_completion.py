"""CPU left outside submitted functions is attributed to the completing work item."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from hermes_cli import loop_accounting as la


@pytest.fixture(autouse=True)
def accounting():
    la.stop()
    la.reset()
    yield
    la.uninstrument()
    la.reset()


def burn(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def test_completion_cpu_named_without_counting_target_twice():
    la.instrument_executors()
    release = threading.Event()
    entered = threading.Event()

    def job():
        entered.set()
        assert release.wait(10)
        burn(0.03)
        return 42

    def completion(future):
        assert future.result() == 42
        burn(0.06)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(job)
        try:
            assert entered.wait(10)
            future.add_done_callback(completion)
        finally:
            release.set()
        assert future.result(timeout=10) == 42
    rows = la.counters()
    name = la._callable_name(job)
    inner = rows['thread/' + name]
    outer = rows['executor_item/' + name]
    assert inner['runs'] == outer['runs'] == 1
    assert 0.025 <= inner['cpu_s'] < 0.06
    assert 0.055 <= outer['cpu_s'] < 0.09
    assert la.snapshot()['process']['accounted_cpu_s'] == pytest.approx(
        inner['cpu_s'] + outer['cpu_s'], abs=0.005)


def test_default_target_capacity_keeps_names_past_64():
    la.instrument_thread_targets()
    for i in range(70):
        def target():
            pass
        target.__qualname__ = f'target_{i}'
        thread = threading.Thread(target=target)
        thread.start()
        thread.join()
    assert len(la._target_exited) == 70
    assert la.TARGET_OTHER not in la._target_exited


def test_proc_stat_user_system_direction(tmp_path):
    # Linux fields 14/15 (utime/stime), not merely their commutative sum.
    stat = tmp_path / 'stat'
    fields = ['S'] + ['0'] * 10 + ['123', '37'] + ['0'] * 10
    stat.write_text('42 (name with ) spaces) ' + ' '.join(fields), encoding='ascii')
    assert la._proc_stat_split(str(stat), 100.0) == ('name with ) spaces', 1.23, 0.37)


@pytest.mark.parametrize('prescoped', [False, True])
def test_deadline_callable_is_named_and_context_preserved(prescoped):
    from contextvars import ContextVar
    from agent.deadline import run_bounded_sync

    value = ContextVar('deadline-value', default='missing')
    value.set('submitted')

    def job():
        burn(0.02)
        return value.get()

    target = la.scoped_call('thread', job) if prescoped else job
    result = run_bounded_sync(target, 10)
    assert result.value == 'submitted'
    rows = {key: row for key, row in la.counters().items() if key.startswith('thread/')}
    assert list(rows) == ['thread/' + la._callable_name(job)]
    assert next(iter(rows.values()))['runs'] == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_deadline_failure_keeps_exception_and_scope_toggle(monkeypatch, enabled):
    from agent.deadline import run_bounded_sync

    monkeypatch.setattr(la, 'scopes_enabled', lambda: enabled)
    failure = ValueError('original')

    def job():
        raise failure

    with pytest.raises(ValueError) as caught:
        run_bounded_sync(job, 10)
    assert caught.value is failure
    assert bool(la.counters()) is enabled


def test_item_hook_restores_and_disabled_scopes_leave_no_rows(monkeypatch):
    from concurrent.futures.thread import _WorkItem

    original = _WorkItem.run
    assert la.instrument_executors()
    wrapped = _WorkItem.run
    assert wrapped is not original
    assert not la.instrument_executors()
    assert _WorkItem.run is wrapped
    monkeypatch.setattr(la, 'scopes_enabled', lambda: False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: 42).result(timeout=10) == 42
    assert la.counters() == {}
    la.uninstrument()
    assert _WorkItem.run is original


@pytest.mark.parametrize('shape', ['legacy', 'task', 'unknown', 'malformed'])
def test_item_shapes_keep_original_invocation(monkeypatch, shape):
    from concurrent.futures.thread import _WorkItem
    from types import SimpleNamespace

    seen = []

    def original(item, *args, **kwargs):
        seen.append((item, args, kwargs))
        return 42

    def job():
        pass

    monkeypatch.setattr(_WorkItem, 'run', original)
    la.instrument_executors()
    item = SimpleNamespace()
    if shape == 'legacy':
        item.fn, item.args = job, ()
    elif shape == 'task':
        item.task = (job, (), {})
    elif shape == 'malformed':
        item.task = (job, ())
    assert _WorkItem.run(item, 'context', extra=3) == 42
    assert seen == [(item, ('context',), {'extra': 3})]
    rows = la.counters()
    if shape in ('unknown', 'malformed'):
        assert rows == {}
    else:
        assert rows['executor_item/' + la._callable_name(job)]['runs'] == 1
    la.uninstrument()


@pytest.mark.platforms('linux')
def test_native_proc_split_distinguishes_user_burn_and_syscalls():
    import os

    results = {}

    def measure(label, work):
        path = f'/proc/self/task/{threading.get_native_id()}/stat'
        before = la._proc_stat_split(path, la._clk_tck())
        work()
        after = la._proc_stat_split(path, la._clk_tck())
        assert before is not None and after is not None
        results[label] = (after[1] - before[1], after[2] - before[2])

    def syscalls():
        fd = os.open('/dev/zero', os.O_RDONLY)
        try:
            end = time.thread_time() + 0.3
            while time.thread_time() < end:
                os.read(fd, 1024 * 1024)
        finally:
            os.close(fd)

    def user_work():
        end = time.thread_time() + 0.3
        while time.thread_time() < end:
            sum(i * i for i in range(10000))

    for label, work in [('user', user_work), ('system', syscalls)]:
        thread = threading.Thread(target=measure, args=(label, work))
        thread.start()
        thread.join(30)
        assert not thread.is_alive()
    assert results['user'][0] > results['user'][1] + 0.1, results
    assert results['system'][1] > results['system'][0] + 0.1, results


@pytest.mark.parametrize('phase', ['enter', 'exit', 'name'])
@pytest.mark.parametrize('raises', [False, True])
def test_deadline_diagnostics_never_retry_or_replace_operation(monkeypatch, phase, raises):
    from agent.deadline import run_bounded_sync

    original = la.Scope

    class BrokenScope(original):
        def __enter__(self):
            if phase == 'enter':
                raise RuntimeError('diagnostic enter')
            return super().__enter__()

        def __exit__(self, *args):
            raise RuntimeError('diagnostic exit')

    def broken_name(*args):
        raise RuntimeError('diagnostic name')

    monkeypatch.setattr(la, 'Scope', BrokenScope)
    if phase == 'name':
        monkeypatch.setattr(la, 'scoped_call', broken_name)
    calls = []
    failure = ValueError('operation')

    def job():
        calls.append(1)
        if raises:
            raise failure
        return 42

    if raises:
        with pytest.raises(ValueError) as caught:
            run_bounded_sync(job, 10)
        assert caught.value is failure
    else:
        assert run_bounded_sync(job, 10).value == 42
    assert calls == [1]


@pytest.mark.parametrize('phase', ['enter', 'exit'])
def test_item_diagnostic_failure_does_not_kill_pool(monkeypatch, phase):
    original = la.Scope

    class BrokenItemScope(original):
        def __enter__(self):
            if self.name.startswith('executor_item/') and phase == 'enter':
                raise RuntimeError('diagnostic enter')
            return super().__enter__()

        def __exit__(self, *args):
            if self.name.startswith('executor_item/'):
                raise RuntimeError('diagnostic exit')
            return super().__exit__(*args)

    monkeypatch.setattr(la, 'Scope', BrokenItemScope)
    la.instrument_executors()
    calls = []

    def job():
        calls.append(1)
        return len(calls)

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(job).result(timeout=10) == 1
        assert pool.submit(job).result(timeout=10) == 2
    assert calls == [1, 1]


def test_unknown_thread_args_are_not_inspected():
    def original(*args):
        return args

    class InvocationOnly:
        def __iter__(self):
            raise AssertionError('diagnostic inspection must not iterate')

    assert la._target_key(original, InvocationOnly()) == la._callable_name(original)
