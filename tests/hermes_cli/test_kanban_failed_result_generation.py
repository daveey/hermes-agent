"""The observer must never act on a board generation that is no longer at its path.

Archive, delete and archive+recreate are driven through the real native
``remove_board``/``create_board`` APIs after the readiness acknowledgement, at
three points: while polling, before proof, and between proof and closure.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_dispatch_owner as owner
from hermes_cli import kanban_failed_result as reconcile
from hermes_cli import kanban_failed_result_observer as observer
from hermes_cli.kanban_db_connect import _dispatch_tick_lock, connect_closing
from tools.environments.local import build_subprocess_env
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

SLUG = 'generation'
REPO = Path(__file__).resolve().parents[2]
_RECREATE = '''
import sys
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
kb.create_board(sys.argv[1])
with connect_closing(kb.kanban_db_path(board=sys.argv[1])) as conn:
    print(kb.create_task(conn, title='new generation', assignee='default'))
'''
ACTIONS = ['archive', 'delete', 'archive_recreate']


class Stuck(Exception):
    pass


@pytest.fixture
def gen(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path / 'kanban'))
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', SLUG)
    kb.create_board(SLUG)
    path = kb.kanban_db_path(board=SLUG)
    with connect_closing(path) as conn:
        tid = kb.create_task(conn, title='generation fixture', assignee='default')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert task is not None
        context = {'db': str(path), 'task_id': tid, 'run_id': task.current_run_id,
                   'claim_lock': task.claim_lock, 'board': SLUG,
                   'writer_pid': os.getpid(), 'writer_birth': kbd._process_fingerprint(os.getpid())}
        yield SimpleNamespace(conn=conn, context=context, path=path, tmp=tmp_path)


def _failed(gen):
    fixtures._failed_child(gen.conn, gen.context)


class Drive:
    """Shared hooks: spy on closure, relocate on demand, bound the poll loop."""

    def __init__(self, gen, monkeypatch, action, polls=6):
        self.gen, self.action, self.moved, self.outcome = gen, action, False, None
        self.closures, self.closures_after_move, self.sleeps = 0, 0, 0
        self.polls = polls
        self.on_sleep: Callable[[int], None] | None = None
        real_detect = kbd.detect_crashed_workers

        def detect(*args, **kwargs):
            self.closures += 1
            self.closures_after_move += self.moved
            return real_detect(*args, **kwargs)
        monkeypatch.setattr(kbd, 'detect_crashed_workers', detect)
        real_time = observer.time

        def sleep(seconds):
            self.sleeps += 1
            if self.sleeps > self.polls:
                raise Stuck('observer kept polling a stale generation')
            if self.on_sleep:
                self.on_sleep(self.sleeps)
        monkeypatch.setattr(observer, 'time', SimpleNamespace(
            sleep=sleep, monotonic=real_time.monotonic, time=real_time.time))

    def relocate(self):
        """One archive/delete/recreate attempt; busy refusal is also correct."""
        if self.moved:
            return
        try:
            kb.remove_board(SLUG, archive=self.action != 'delete')
        except ValueError as exc:
            assert 'busy' in str(exc), exc
            self.outcome = 'refused'
            return
        self.moved, self.outcome = True, 'moved'
        if self.action == 'archive_recreate':
            # A separate process, like the real observer's peers: recreating in
            # this process would make native recovery close the observer's handle.
            done = subprocess.run(
                [sys.executable, '-c', _RECREATE, SLUG], capture_output=True, text=True,
                encoding='utf-8', env=build_subprocess_env(), cwd=str(REPO), timeout=60, check=False)
            assert done.returncode == 0, done.stderr
            self.new_task = done.stdout.strip()

    def at_ack(self, monkeypatch):
        outer = self

        class Out:
            class buffer:
                @staticmethod
                def write(data):
                    outer.relocate()

                @staticmethod
                def flush():
                    pass
        monkeypatch.setattr(sys, 'stdout', Out)

    def new_generation_untouched(self):
        if self.action != 'archive_recreate' or not self.moved:
            return
        with connect_closing(self.gen.path) as fresh:
            assert fresh.execute('SELECT COUNT(*) FROM task_runs').fetchone()[0] == 0
            kinds = [r[0] for r in fresh.execute('SELECT kind FROM task_events')]
            assert 'crashed' not in kinds and 'worker_result' not in kinds, kinds
            assert fresh.execute('SELECT status FROM tasks WHERE id=?', (self.new_task,)).fetchone()[0] == 'ready'

    def old_generation_untouched(self):
        if self.moved and self.action != 'delete':
            assert self.gen.conn.execute('SELECT status FROM task_runs WHERE id=?',
                                         (self.gen.context['run_id'],)).fetchone()[0] == 'running'


@pytest.mark.parametrize('action', ACTIONS)
def test_observer_stops_polling_when_board_leaves_its_path(gen, monkeypatch, action):
    drive = Drive(gen, monkeypatch, action)
    drive.at_ack(monkeypatch)  # relocated immediately after the acknowledgement
    observer.observe(gen.context)  # a stale poller never returns -> Stuck
    assert drive.moved and drive.closures == 0
    drive.new_generation_untouched()


@pytest.mark.parametrize('action', ACTIONS)
def test_observer_stops_when_board_leaves_during_polling(gen, monkeypatch, action):
    drive = Drive(gen, monkeypatch, action)
    drive.on_sleep = lambda n: drive.relocate() if n == 1 else None
    observer.observe(gen.context)
    assert drive.moved and drive.sleeps <= 2 and drive.closures == 0
    drive.new_generation_untouched()


@pytest.mark.parametrize('action', ACTIONS)
def test_failed_run_is_not_closed_in_a_relocated_generation(gen, monkeypatch, action):
    _failed(gen)
    drive = Drive(gen, monkeypatch, action)
    drive.at_ack(monkeypatch)
    observer.observe(gen.context)
    assert drive.moved and drive.closures_after_move == 0
    drive.old_generation_untouched()
    drive.new_generation_untouched()


@pytest.mark.parametrize('action', ACTIONS)
def test_relocation_between_proof_and_closure_never_closes_stale_generation(gen, monkeypatch, action):
    _failed(gen)
    drive = Drive(gen, monkeypatch, action)
    real_proof = reconcile.failed_result_proof

    def proof(*args, **kwargs):
        result = real_proof(*args, **kwargs)
        if result is not None:
            drive.relocate()  # the writer proved failure; now the board vanishes
        return result
    monkeypatch.setattr(reconcile, 'failed_result_proof', proof)
    observer.observe(gen.context)
    assert drive.outcome in ('refused', 'moved')
    assert drive.closures_after_move == 0
    drive.old_generation_untouched()
    drive.new_generation_untouched()
    if drive.outcome == 'refused':  # exclusion kept the generation: closure happened exactly once
        assert drive.closures == 1
        assert gen.conn.execute('SELECT status FROM task_runs WHERE id=?',
                                (gen.context['run_id'],)).fetchone()[0] != 'running'


@pytest.mark.parametrize('action', ACTIONS)
def test_relocation_between_pre_lock_check_and_lock_is_revalidated(gen, monkeypatch, action):
    """Move the board right after the lock-free generation check passes."""
    _failed(gen)
    drive = Drive(gen, monkeypatch, action)
    real_check = owner.connection_db_path
    calls = []

    def check(conn, **kwargs):
        value = real_check(conn, **kwargs)
        calls.append(value)
        if len(calls) == 1:
            drive.relocate()
        return value
    monkeypatch.setattr(owner, 'connection_db_path', check)
    observer.observe(gen.context)
    assert drive.moved and drive.closures_after_move == 0
    drive.old_generation_untouched()
    drive.new_generation_untouched()


def test_closure_excludes_native_archive_by_holding_the_generation_lock(gen, monkeypatch):
    _failed(gen)
    Drive(gen, monkeypatch, 'archive')  # installs the polling bound
    real_detect = kbd.detect_crashed_workers
    seen = []

    def detect(*args, **kwargs):
        with pytest.raises(ValueError, match='busy'):
            kb.remove_board(SLUG)
        seen.append(True)
        return real_detect(*args, **kwargs)
    monkeypatch.setattr(kbd, 'detect_crashed_workers', detect)
    observer.observe(gen.context)
    assert seen == [True]
    assert gen.conn.execute('SELECT status FROM task_runs WHERE id=?',
                            (gen.context['run_id'],)).fetchone()[0] != 'running'


def test_polling_does_not_hold_the_dispatch_lock_between_polls(gen, monkeypatch):
    drive = Drive(gen, monkeypatch, 'archive')
    drive.on_sleep = lambda n: drive.relocate()  # native archive needs the lock free while idle
    observer.observe(gen.context)
    assert drive.outcome == 'moved'


@pytest.mark.parametrize('lock_before_open', [True, False])
def test_stalled_real_dispatch_process_does_not_prevent_closure(gen, monkeypatch, lock_before_open):
    _failed(gen)
    Drive(gen, monkeypatch, 'archive', polls=6)
    command = """
import sys
from pathlib import Path
from hermes_cli.kanban_db_connect import _dispatch_tick_lock
with _dispatch_tick_lock(Path(sys.argv[1])) as held:
    assert held
    print('locked', flush=True)
    sys.stdin.read()
"""
    proc = None

    def stall():
        nonlocal proc
        proc = subprocess.Popen([sys.executable, '-c', command, str(gen.path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=build_subprocess_env(), cwd=str(REPO))
        assert proc.stdout.readline().strip() == 'locked'

    if lock_before_open:
        stall()
    else:
        class Out:
            class buffer:
                write = staticmethod(lambda data: stall())
                flush = staticmethod(lambda: None)
        monkeypatch.setattr(sys, 'stdout', Out)
    start = time.monotonic()
    try:
        observer.observe(gen.context)
        assert proc.poll() is None
        assert gen.conn.execute('SELECT status FROM task_runs WHERE id=?',
                                (gen.context['run_id'],)).fetchone()[0] != 'running'
        assert time.monotonic() - start < 5
    finally:
        if proc:
            proc.communicate(input='', timeout=10)


@pytest.mark.parametrize('archive', [True, False])
def test_generation_lock_refuses_native_relocation_without_pointer_change(gen, archive):
    kb.set_current_board(SLUG)
    key = gen.path.with_name(gen.path.name + '.generation')
    with _dispatch_tick_lock(key, create_parent=False) as held:
        assert held
        with pytest.raises(ValueError, match='busy'):
            kb.remove_board(SLUG, archive=archive)
        assert kb.get_current_board() == SLUG
        assert gen.path.is_file()
