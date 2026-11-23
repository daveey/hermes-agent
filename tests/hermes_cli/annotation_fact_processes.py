"""Isolated annotation transport targets; lifecycle facade is TEST ONLY.

Source capture is deliberately controlled SQLite, not the production adapter or
release latch. Real FactHost/ReplicaHandle and canonical annotation code run.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import builtins
from pathlib import Path
import socket
import sqlite3
import subprocess
import threading

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_annotation_disk as disk
from hermes_cli.kanban_activity_projection import project_kanban_activity
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint

SCOPES = (Scope('annotation-test', 'alpha'), Scope('annotation-test', 'beta'))
TOKENS = ('a' * 48, 'b' * 48)


def owner(directory, pipe):
    host = FactHost(directory, dict(zip(TOKENS, SCOPES)))
    try:
        host.start()
    except OSError as exc:
        pipe.send({'error': type(exc).__name__})
        pipe.close()
        return
    reads = 0
    db = sqlite3.connect(directory / 'source.db')
    db.execute('CREATE TABLE IF NOT EXISTS waiting (profile TEXT, task TEXT)')
    pipe.send({'ready': True})
    try:
        while True:
            cmd = pipe.recv()
            if cmd[0] == 'stop':
                break
            if cmd[0] == 'capture':
                _, index, tasks = cmd
                profile = SCOPES[index].profile_id
                with db:
                    db.execute('DELETE FROM waiting WHERE profile=?', (profile,))
                    db.executemany('INSERT INTO waiting VALUES (?,?)', [(profile, task) for task in tasks])
                captured = [r[0] for r in db.execute('SELECT task FROM waiting WHERE profile=?', (profile,))]
                reads += 1
                keys = {('default/' + task, 'active', False) for task in captured}
                parts = ({'s': len(captured)}, {}, {}, {}, {'s': set(captured)}, {}, {}, {},
                         {'s': keys}, {}, {}, {})
                projected = project_kanban_activity([{'id': 's'}], parts=parts,
                                                    observed_at=1234.0, now=1234.0, external={})[0]
                patch = {k: v for k, v in projected.items() if k != 'id'}
                host.store.publish([Fact(FactKey(SCOPES[index], 'SessionFact', 's'),
                                         {'annotations': {'kanban': patch}}, health='fresh')])
                pipe.send({'expected': projected, 'reads': reads})
            elif cmd[0] == 'stats':
                pipe.send({'reads': reads, 'epoch': host.store.snapshot(SCOPES[0]).epoch})
    finally:
        db.close()
        host.close()
        pipe.close()


class LifecycleFacade:
    """Only the missing coordinator seam is doubled, never store or transport."""
    def __init__(self, v1):
        self.v1 = v1
        self.callback = None

    def __getattr__(self, name):
        return getattr(self.v1, name)

    def bind_consumer(self, name, scope, callback):
        assert name == 'annotations'
        self.callback = callback
        return SimpleNamespace(close=lambda: None)


@contextmanager
def request_guard():
    """Trip only on request thread; real replica sockets remain operational."""
    request_thread = threading.get_ident()
    calls = []
    def guarded(original, label):
        def call(*args, **kwargs):
            if threading.get_ident() == request_thread:
                calls.append(label)
                raise AssertionError('request I/O: ' + label)
            return original(*args, **kwargs)
        return call
    with pytest.MonkeyPatch.context() as patch:
        for obj, name in ((builtins, 'open'), (Path, 'open'), (Path, 'stat'),
                          (sqlite3, 'connect'), (socket, 'socket'), (subprocess, 'Popen'),
                          (activity, '_annotation_snapshot'), (activity, '_open_ro'),
                          (activity, 'cached_ro'), (disk, 'load_host'), (disk, 'load')):
            patch.setattr(obj, name, guarded(getattr(obj, name), name))
        yield calls


def replica(directory, index, pipe):
    scope = SCOPES[index]
    handle = acquire(scope, endpoint=Endpoint(str(directory / 'facts.sock'), TOKENS[index]))
    lifecycle = []
    facade = LifecycleFacade(handle.v1)
    consumer = annotations.AnnotationFacts(SimpleNamespace(v1=facade, demand=handle.demand), scope,
        stop_legacy=lambda: lifecycle.append('stop'), start_legacy=lambda: lifecycle.append('start'))
    consumer.on_mode('facts', 1)  # controlled seam only; NOT global release activation
    pipe.send({'ready': True})
    try:
        while True:
            cmd = pipe.recv()
            if cmd == 'stop':
                break
            if cmd == 'read':
                try:
                    with request_guard() as calls, annotations.use(consumer):
                        rows = activity.annotate_kanban_activity([{'id': 's'}])
                    pipe.send({'rows': rows, 'io': calls, 'epoch': handle.v1.snapshot(scope).epoch})
                except annotations.AnnotationUnavailable:
                    pipe.send({'unavailable': True})
            elif cmd == 'privacy':
                try:
                    handle.v1.snapshot(SCOPES[1 - index])
                except ValueError:
                    pipe.send({'denied': True})
                else:
                    pipe.send({'denied': False})
            elif cmd == 'rollback':
                consumer.on_mode('legacy', 2)
                consumer.on_mode('legacy', 2)
                pipe.send({'lifecycle': lifecycle, 'subscription': consumer._subscription is not None})
    finally:
        consumer.close()
        handle.close()
        pipe.close()
