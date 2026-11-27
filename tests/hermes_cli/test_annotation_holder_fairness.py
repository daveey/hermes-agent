"""Slow serial refreshes must serve the stalest demanded profile first."""
import contextvars
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('seconds', [6, 104])
def test_stalest_demanded_scope_first(monkeypatch, tmp_path, native, seconds):
    current = contextvars.ContextVar('fairness_scope')
    scopes = [('home', f'p{i}') for i in range(8)]
    contexts = {}
    for scope in scopes:
        ctx = contextvars.copy_context()
        ctx.run(current.set, scope)
        contexts[scope] = ctx
    monkeypatch.setattr(host, '_known', contexts)
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    monkeypatch.setattr(host, '_unsettled', set())
    monkeypatch.setattr(host, '_settle_deadlines', {})
    monkeypatch.setattr(host, '_coverage_stale', set())
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(host._disk, '_path', lambda scope: tmp_path / scope[1])
    monkeypatch.setattr(ksa, '_origin_scope', current.get)
    monkeypatch.setattr(host, '_asleep', lambda scope: False)
    monkeypatch.setattr(host._parts, 'plan', lambda *a, **kw: ({'counts'}, {}))
    monkeypatch.setattr(host._parts, 'done', lambda *a: None)
    monkeypatch.setattr(host._parts, 'failed', lambda *a: None)
    now = [1000.0]
    reads = {scope: 100.0 + (7 - i) * 10 for i, scope in enumerate(scopes)}
    reads[scopes[3]] = None  # never published: first among demanded scopes
    monkeypatch.setattr(host.time, 'time', lambda: now[0])

    def copy(scope, **kwargs):
        assert current.get() == scope, 'read publication outside its profile context'
        return ('fingerprint', reads[scope], ()) if reads[scope] is not None else None

    monkeypatch.setattr(host._disk, 'published_read_at', lambda scope: (copy(scope) or (None, None))[1])
    monkeypatch.setattr(host._disk, 'demand_age', lambda scope, **kw: None if scope == scopes[7] else 0)
    calls = []

    def refresh(scope):
        demanded = [s for s in scopes if s != scopes[7] and s not in calls]
        expected = min(demanded, key=lambda s: float('-inf') if reads[s] is None else reads[s])
        assert scope == expected
        calls.append(scope)
        reads[scope] = now[0]
        now[0] += seconds  # deterministic wall time, not a real sleep
        return True

    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    monkeypatch.setattr(host, '_holder_refresh', lambda scope: refresh(scope) if scope != scopes[7] else False)
    if native:
        host._event_refresh(SimpleNamespace(replace=lambda *a, **kw: None, control_only=False))
    else:
        assert host._holder_pass() == 7
    assert calls == [scopes[i] for i in [3, 6, 5, 4, 2, 1, 0]]


def test_priority_read_never_resurrects_retired_profile(monkeypatch, tmp_path):
    from hermes_cli.kanban_annotation_fairness import stalest_first

    home = tmp_path / 'profiles' / 'retired'
    scope = (str(home), 'default')
    monkeypatch.setattr(host._disk, 'enabled', lambda: True)
    monkeypatch.setattr(host._disk, '_path', lambda scope: home / 'cache' / 'annotation.json')
    assert stalest_first([(scope, contextvars.copy_context())])
    assert not home.exists(), 'sorting a retired scope must never recreate its home'


@pytest.mark.parametrize('invalid', ['scope', 'version', 'future', 'malformed'])
def test_priority_header_is_advisory_readonly_metadata(monkeypatch, tmp_path, invalid):
    import json

    disk = host._disk
    scope = ('home', 'p')
    folder = tmp_path / 'publication'
    folder.mkdir()
    path = folder / 'annotation.json'
    header = path.with_suffix('.host.json')
    monkeypatch.setattr(disk, 'enabled', lambda: True)
    monkeypatch.setattr(disk, '_path', lambda scope: path)
    monkeypatch.setattr(disk.time, 'time', lambda: 1000)
    body = {'version': disk._HOST_VERSION, 'scope': list(scope), 'read_at': 100}
    header.write_text(json.dumps(body))
    assert disk.published_read_at(scope) == 100
    assert sorted(p.name for p in folder.iterdir()) == [header.name]
    if invalid == 'scope':
        body['scope'] = ['another']
    elif invalid == 'version':
        body['version'] = -1
    elif invalid == 'future':
        body['read_at'] = 2000
    header.write_text('broken' if invalid == 'malformed' else json.dumps(body))
    assert disk.published_read_at(scope) is None
    assert sorted(p.name for p in folder.iterdir()) == [header.name]
