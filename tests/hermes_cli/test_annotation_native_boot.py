"""Bootstrap selector discovery bypasses body caches and legacy producers."""
from functools import wraps
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_boot import seeds
from hermes_cli.fact_contract import Scope


def test_seed_discovery_is_scoped_discards_bodies_and_never_annotates(monkeypatch):
    from hermes_cli.goal_session_activity import annotate_goal_activity
    from hermes_cli.cron_session_activity import annotate_cron_activity
    runtime = SimpleNamespace(scopes={'default': Scope('install', 'default'), 'named': Scope('install', 'named')},
                              registry=SimpleNamespace(fallback_profile='default'))
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy annotation admitted during native bootstrap')
    monkeypatch.setattr(annotations, '_legacy_call', forbidden)
    def reader(limit=20):
        assert limit == 20
        rows = [{'id': 's', 'profile': name, '_lineage_ids': ['root', 's'], 'title': 'not retained'}
                for name in runtime.scopes]
        annotate_goal_activity(rows)
        annotate_cron_activity(rows)
        return {'discarded': rows}
    @wraps(reader)
    def cached(*args, **kwargs):
        raise AssertionError('would poison request body cache')
    actual = seeds(runtime, readers=(cached,))
    assert actual == {name: [{'id': 's', 'profile': name, '_lineage_ids': ['root', 's']}]
                      for name in runtime.scopes}
    with pytest.raises(AssertionError, match='legacy annotation admitted'):
        annotate_goal_activity([{'id': 'after'}])


def test_seed_failure_cannot_leak_bootstrap_context(monkeypatch):
    runtime = SimpleNamespace(scopes={'default': Scope('install', 'default')},
                              registry=SimpleNamespace(fallback_profile='default'))
    def fail():
        raise RuntimeError('bootstrap reader failed')
    with pytest.raises(RuntimeError, match='bootstrap reader failed'):
        seeds(runtime, readers=(fail,))
    assert annotations._seed_collector.get() is None
