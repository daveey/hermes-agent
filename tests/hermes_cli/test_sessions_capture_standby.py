"""Standby capture child: prepared once, used once, retired with custody, always accounted."""
import json
import struct
import subprocess
import threading
import time

import pytest

from hermes_cli import loop_accounting
from hermes_cli import sessions_capture_process as module
from hermes_cli.sessions_capture_process import CaptureProcess, CaptureUnavailable
from tests.hermes_cli.test_sessions_capture_process import _burn, command, frame
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('linux')

# A frame naming the child that wrote it.
PID_FRAME = ('import json, os, struct, sys\nsys.stdin.buffer.read()\n'
             'body = json.dumps({"pid": os.getpid()}).encode()\n'
             'sys.stdout.buffer.write(struct.pack("!I", len(body)) + body)\nsys.stdout.buffer.flush()\n')
# Burns CPU before it blocks on stdin, so an unused standby has CPU to account.
BUSY_STANDBY = ('import time\nend = time.process_time() + .2\n'
                'while time.process_time() < end:\n    pass\n' + PID_FRAME)


def stat():
    return dict(loop_accounting.counters().get('sessions.capture.child', {}))


def delta(before, key):
    return stat().get(key, 0) - before.get(key, 0)


def served_by(capture):
    return json.loads(capture('sessions', {}))['pid']


@pytest.fixture(autouse=True)
def generation(monkeypatch):
    # The real identity reads the checkout's .git, which a worktree keeps under the real Hermes home.
    monkeypatch.setattr(module, '_generation', lambda: 'boot', raising=False)


@pytest.fixture
def spawns(monkeypatch):
    """Every child spawned, and every pid whose group was stopped; no child outlives the test."""
    children, stopped = [], []
    spawn, stop = module.spawn_owned, module.stop_owned_group_survivors

    def spawned(*args, **kwargs):
        children.append(spawn(*args, **kwargs))
        return children[-1]

    def stopping(pid, **kwargs):
        stopped.append(pid)
        return stop(pid, **kwargs)
    monkeypatch.setattr(module, 'spawn_owned', spawned)
    monkeypatch.setattr(module, 'stop_owned_group_survivors', stopping)
    yield children, stopped
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(5)
        stop(child.pid, proc=child, grace=0)


def test_prepared_child_serves_the_next_capture_only(spawns):
    children, _ = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    standby = capture._standby.child
    assert standby.poll() is None and capture._child is None
    assert served_by(capture) == standby.pid
    assert capture._standby is None and len(children) == 1
    assert served_by(capture) == children[1].pid != standby.pid
    assert len(children) == 2
    capture.close()


def test_prepare_is_idempotent_and_refused_when_closed_or_busy(spawns):
    children, _ = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    capture.prepare()
    assert len(children) == 1
    capture.close()
    capture.prepare()
    assert len(children) == 1 and capture._standby is None

    busy = CaptureProcess(seconds=30, command=command('import time; time.sleep(30)'))
    thread = threading.Thread(target=lambda: pytest.raises(CaptureUnavailable, busy, 'sessions', {}))
    thread.start()
    wait_until(lambda: busy._child is not None, what='active capture child')
    busy.prepare()
    assert len(children) == 2 and busy._standby is None
    busy.close()
    thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize('stale', ['age', 'generation'])
def test_stale_standby_is_retired_accounted_and_replaced(spawns, monkeypatch, stale):
    children, stopped = spawns
    generations = iter(['boot', 'boot' if stale == 'age' else 'updated'])
    monkeypatch.setattr(module, '_generation', lambda: next(generations))
    capture = CaptureProcess(command=command(BUSY_STANDBY))
    capture.prepare()
    standby = capture._standby.child
    wait_until(lambda: (module._child_cpu(standby.pid) or 0) >= .15, what='standby CPU')
    if stale == 'age':
        monkeypatch.setattr(module, 'STANDBY_MAX_AGE_S', -1)
    before = stat()
    capture.command = command(PID_FRAME)
    assert served_by(capture) == children[1].pid != standby.pid
    assert standby.poll() is not None and standby.pid in stopped
    assert delta(before, 'runs') == 2 and delta(before, 'skips') == 1 and delta(before, 'errors') == 0
    assert delta(before, 'cpu_s') >= .15
    capture.close()
    assert delta(before, 'runs') == 2  # nothing left to account


def test_fresh_standby_of_the_same_generation_is_used(spawns, monkeypatch):
    children, _ = spawns
    monkeypatch.setattr(module, '_generation', lambda: 'boot', raising=False)
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    before = stat()
    assert served_by(capture) == children[0].pid
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 0
    capture.close()


def test_generation_is_the_checkout_fingerprint(monkeypatch):
    from pathlib import Path
    from hermes_cli._startup_fast import project_root_str
    monkeypatch.undo()
    monkeypatch.setattr(module, 'read_git_revision_fingerprint', lambda root: ('git', root))
    assert module._generation() == ('git', Path(project_root_str()))


def _zombie(pid):
    with open(f'/proc/{pid}/stat', encoding='ascii', errors='replace') as stat_file:
        return stat_file.read().rpartition(')')[2].split()[0] == 'Z'


def test_dead_standby_is_replaced_and_its_cpu_accounted(spawns):
    children, stopped = spawns
    capture = CaptureProcess(command=command(_burn(.2, '').replace('sys.stdin.buffer.read()\n', '')))
    capture.prepare()
    standby = capture._standby.child
    wait_until(lambda: _zombie(standby.pid), what='standby exit')
    before = stat()
    capture.command = command(PID_FRAME)
    assert served_by(capture) == children[1].pid
    assert standby.pid in stopped
    assert delta(before, 'runs') == 2 and delta(before, 'skips') == 1
    assert delta(before, 'fallbacks') == 0  # sampled before the poll that reaped it
    assert delta(before, 'cpu_s') >= .15
    capture.close()


def test_close_retires_and_accounts_a_live_standby(spawns):
    _, stopped = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    standby = capture._standby.child
    before = stat()
    capture.close()
    assert standby.poll() is not None and standby.pid in stopped and capture._standby is None
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1 and delta(before, 'errors') == 0
    capture.close()
    assert delta(before, 'runs') == 1
    with pytest.raises(CaptureUnavailable, match='closed'):
        capture('sessions', {})


def test_close_attempts_the_standby_when_the_active_child_keeps_custody(spawns, monkeypatch):
    # White box: the public flow never holds both, so close() is given both to prove it tries each.
    children, stopped = spawns
    capture = CaptureProcess(seconds=30, command=command('import time; time.sleep(30)'))
    errors = []

    def run():
        try:
            capture('sessions', {})
        except (CaptureUnavailable, TimeoutError) as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    wait_until(lambda: capture._child is not None, what='active capture child')
    active = capture._child
    capture._standby = module._Standby(capture._spawn(), time.monotonic())
    standby = capture._standby.child
    tracked = module.stop_owned_group_survivors
    monkeypatch.setattr(module, 'stop_owned_group_survivors',
                        lambda pid, **kwargs: pid != active.pid and tracked(pid, **kwargs))
    before = stat()
    with pytest.raises(TimeoutError, match='custody retained'):
        capture.close()
    thread.join(5)
    assert not thread.is_alive() and errors
    assert standby.poll() is not None and standby.pid in stopped and capture._standby is None
    assert capture._child is active  # custody of the unstopped group is kept
    assert delta(before, 'skips') == 1 and delta(before, 'runs') == 2
    assert len(children) == 2


def test_standby_whose_group_does_not_stop_stays_owned_and_is_accounted_once(spawns, monkeypatch):
    children, _ = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    standby = capture._standby
    monkeypatch.setattr(module, 'STANDBY_MAX_AGE_S', -1)
    tracked = module.stop_owned_group_survivors
    monkeypatch.setattr(module, 'stop_owned_group_survivors', lambda pid, **kwargs: False)
    before = stat()
    with pytest.raises(TimeoutError, match='custody retained'):
        capture('sessions', {})
    assert capture._standby is standby and capture._child is None and len(children) == 1
    monkeypatch.setattr(module, 'stop_owned_group_survivors', tracked)
    assert served_by(capture) == children[1].pid
    assert capture._standby is None
    assert delta(before, 'runs') == 2 and delta(before, 'skips') == 1
    capture.close()


def test_prepare_spawn_failure_leaves_the_reader_usable(spawns):
    children, _ = spawns
    capture = CaptureProcess(command=['/nonexistent/capture-worker'])
    before = stat()
    capture.prepare()
    assert capture._standby is None and not children and delta(before, 'runs') == 0
    capture.command = command(PID_FRAME)
    assert served_by(capture) == children[0].pid
    capture.close()


def test_partially_prepared_child_is_rolled_back(spawns, monkeypatch):
    children, stopped = spawns

    def broken():
        raise RuntimeError('no identity')
    monkeypatch.setattr(module, '_generation', broken)
    capture = CaptureProcess(command=command(PID_FRAME))
    before = stat()
    with pytest.raises(RuntimeError, match='no identity'):
        capture.prepare()
    assert capture._standby is None and len(children) == 1
    assert children[0].poll() is not None and children[0].pid in stopped
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1
    capture.close()


def bounded(scenario):
    """Run ``scenario`` on its own thread, so a self-deadlock fails the test instead of hanging it."""
    errors = []

    def run():
        try:
            scenario()
        except BaseException as error:  # health: allow BLE001 -- re-raised on the test thread below
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), 'the reader deadlocked on its own lock'
    if errors:
        raise errors[0]


def test_close_reentered_during_the_spawn_retires_the_child_prepare_was_bringing_up(spawns, monkeypatch):
    # What a signal handler's close() does when it runs on the thread that is inside prepare().
    children, stopped = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    spawn = module.spawn_owned

    def closing(*args, **kwargs):
        child = spawn(*args, **kwargs)
        capture.close()
        return child
    monkeypatch.setattr(module, 'spawn_owned', closing)
    before = stat()
    bounded(capture.prepare)
    assert capture._standby is None and len(children) == 1  # no standby on a closed reader
    assert children[0].poll() is not None and children[0].pid in stopped
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1
    capture.prepare()
    capture.close()
    assert len(children) == 1 and delta(before, 'runs') == 1
    with pytest.raises(CaptureUnavailable, match='closed'):
        capture('sessions', {})


def test_close_reentered_while_it_retires_the_standby_stops_and_accounts_it_once(spawns, monkeypatch):
    children, stopped = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    standby = children[0]
    kill, entered = module.kill_process_tree, []

    def reentering(proc, **kwargs):
        if not entered:  # the standby is still alive: the inner close() has all of the work to do
            entered.append(proc.poll())
            capture.close()
        return kill(proc, **kwargs)
    monkeypatch.setattr(module, 'kill_process_tree', reentering)
    before = stat()
    bounded(capture.close)
    assert entered == [None] and capture._standby is None and len(children) == 1
    assert standby.poll() is not None and standby.pid in stopped
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1 and delta(before, 'errors') == 0


def test_capture_that_starts_while_the_standby_spawns_keeps_one_active_child(spawns, monkeypatch):
    children, stopped = spawns
    capture = CaptureProcess(seconds=30, command=command('import time; time.sleep(30)'))
    spawn, threads = module.spawn_owned, []

    def racing(*args, **kwargs):
        child = spawn(*args, **kwargs)
        if not threads:  # prepare()'s spawn: a capture takes the reader before the standby is published
            threads.append(threading.Thread(target=lambda: pytest.raises(CaptureUnavailable, capture, 'sessions', {})))
            threads[0].start()
            wait_until(lambda: capture._child is not None, what='capture racing the standby spawn')
        return child
    monkeypatch.setattr(module, 'spawn_owned', racing)
    before = stat()
    capture.prepare()
    assert capture._standby is None and capture._child is children[1]
    assert children[0].poll() is not None and children[0].pid in stopped  # the late standby is not kept
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1
    capture.close()
    threads[0].join(5)
    assert not threads[0].is_alive() and len(children) == 2
    assert delta(before, 'runs') == 2


class _HandoffLock:
    """The reader's lock, which runs ``after_publish`` on the preparing thread the moment prepare()
    releases the lock it published the standby under (once, at the outermost release)."""
    def __init__(self, capture, after_publish):
        self.capture, self.after_publish = capture, after_publish
        self.inner, self.depth, self.owner, self.fired = threading.RLock(), 0, None, False

    def __enter__(self):
        self.inner.acquire()
        self.depth += 1
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.depth -= 1
        fire = (self.depth == 0 and not self.fired and self.owner == preparing[0]
                and self.capture._standby is not None)
        self.inner.release()
        if fire:
            self.fired = True
            self.after_publish()


preparing: list = [None]


def test_capture_that_takes_the_just_published_standby_keeps_it(spawns):
    # The handoff window: published, lock released, prepare() not yet returned. A capture that takes
    # the standby there owns it; prepare() must neither stop it nor account it.
    children, _ = spawns
    capture = CaptureProcess(seconds=30, command=command(PID_FRAME))
    served, threads = [], []

    def take():
        threads.append(threading.Thread(target=lambda: served.append(served_by(capture))))
        threads[0].start()
        wait_until(lambda: capture._child is not None or served, what='capture taking the standby')
    lock = _HandoffLock(capture, take)
    capture._lock = lock  # type: ignore[assignment]
    preparing[0] = threading.get_ident()
    before = stat()
    capture.prepare()
    threads[0].join(10)
    assert not threads[0].is_alive() and lock.fired
    assert served == [children[0].pid] and len(children) == 1
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 0 and delta(before, 'errors') == 0
    capture.close()
    assert delta(before, 'runs') == 1


def test_close_in_the_handoff_window_retires_the_published_standby_once(spawns):
    children, stopped = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    lock = _HandoffLock(capture, capture.close)
    capture._lock = lock  # type: ignore[assignment]
    before = stat()

    def prepare():
        preparing[0] = threading.get_ident()
        capture.prepare()
    bounded(prepare)
    assert lock.fired and capture._standby is None and len(children) == 1
    assert children[0].poll() is not None and children[0].pid in stopped
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1


def test_unkept_standby_whose_group_does_not_stop_stays_owned_for_close(spawns, monkeypatch):
    # prepare() lost the race to close(); its own rollback cannot stop the group. The child must stay
    # owned (published) so a later close() retries, accounted once, and is never used for a capture.
    children, _ = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    spawn = module.spawn_owned

    def closing(*args, **kwargs):
        child = spawn(*args, **kwargs)
        capture.close()
        return child
    monkeypatch.setattr(module, 'spawn_owned', closing)
    tracked = module.stop_owned_group_survivors
    monkeypatch.setattr(module, 'stop_owned_group_survivors', lambda pid, **kwargs: False)
    before = stat()
    with pytest.raises(TimeoutError, match='custody retained'):
        capture.prepare()
    assert capture._standby is not None and capture._standby.child is children[0]
    monkeypatch.setattr(module, 'stop_owned_group_survivors', tracked)
    capture.close()
    assert capture._standby is None and children[0].poll() is not None
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 1


def test_standby_whose_retirement_began_is_retired_not_used(spawns, monkeypatch):
    children, stopped = spawns
    capture = CaptureProcess(command=command(PID_FRAME))
    capture.prepare()
    standby = capture._standby
    assert standby is not None
    assert standby.accounted.acquire(blocking=False)  # as a failed or interrupted retirement leaves it
    before = stat()
    assert served_by(capture) == children[1].pid != standby.child.pid
    assert standby.child.poll() is not None and standby.child.pid in stopped
    assert delta(before, 'runs') == 1 and delta(before, 'skips') == 0  # its record was already taken
    capture.close()


def test_every_spawned_child_is_accounted_exactly_once(spawns, monkeypatch):
    children, _ = spawns
    before = stat()
    capture = CaptureProcess(seconds=5, command=command(PID_FRAME))
    served_by(capture)                                  # fresh, success
    capture.prepare()
    served_by(capture)                                  # standby, success
    capture.command = command('import sys, os; sys.stdin.buffer.read(); os._exit(7)')
    with pytest.raises(CaptureUnavailable):
        capture('sessions', {})                         # fresh, failure
    capture.prepare()
    with pytest.raises(CaptureUnavailable):
        capture('sessions', {})                         # standby, failure
    capture.command = command(PID_FRAME)
    capture.prepare()
    monkeypatch.setattr(module, 'STANDBY_MAX_AGE_S', -1)
    served_by(capture)                                  # standby discarded by age + fresh, success
    monkeypatch.setattr(module, 'STANDBY_MAX_AGE_S', 30)
    capture.prepare()
    capture.close()                                     # standby discarded by close
    assert len(children) == 7
    assert delta(before, 'runs') == 7
    assert delta(before, 'skips') == 2 and delta(before, 'errors') == 2
    assert all(child.poll() is not None for child in children)


def test_fake_frame_helper_matches_the_wire():
    # Guard for the fixtures above: the shared frame helper still yields one valid frame.
    out = subprocess.run(command(frame()), input=b'', capture_output=True, check=True).stdout
    assert struct.unpack('!I', out[:4])[0] == len(out) - 4
