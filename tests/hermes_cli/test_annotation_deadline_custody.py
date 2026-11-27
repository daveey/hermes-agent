"""A renewed replica observation supersedes only its own earlier clock boundaries."""
import contextvars
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_parts as parts
from hermes_cli import kanban_annotation_counts as counts
from hermes_cli import kanban_session_activity as ksa


@pytest.fixture
def replica_holder(monkeypatch, tmp_path):
    scope = ('replica-deadline',)
    path = tmp_path / 'replica.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE replica (coll TEXT, key TEXT, gw TEXT, data TEXT, updated_at INTEGER, seq INTEGER, version INTEGER)')
        conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
        conn.execute("INSERT INTO meta VALUES ('last_ok','100')")
    now = [100.0]
    revision = [0]
    monkeypatch.setattr(host.time, 'time', lambda: now[0])
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    monkeypatch.setattr(host, '_coverage_stale', set())
    monkeypatch.setattr(host, '_settle_deadlines', {})
    monkeypatch.setattr(host, '_unsettled', set())
    monkeypatch.setattr(host, 'SETTLE_S', ())
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(host, '_scope_interested', lambda scope: True)
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / 'copy')
    monkeypatch.setattr(disk, 'demand_age', lambda scope: 0)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_self_gateway', lambda conn: 'local')
    monkeypatch.setattr(ksa, '_remote_read_memo', {})
    monkeypatch.setattr(ksa, '_note_known_gateways', lambda *args: None)
    monkeypatch.setattr(parts, '_seen', {})
    monkeypatch.setattr(parts, 'keys', lambda scope: {name: revision[0] for name in parts.ALL})
    monkeypatch.setattr(counts, 'begin', lambda *args: None)
    monkeypatch.setattr(counts, 'needs_read', lambda scope: False)
    monkeypatch.setattr(counts, 'read_at', lambda scope: host.time.monotonic())
    calls = []
    def refresh(scope):
        calls.append(now[0])
        assert ksa._read_remote_links(path) == []
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    events = SimpleNamespace(replace=lambda *a, **kw: None, control_only=False)
    return SimpleNamespace(scope=scope, path=path, now=now, revision=revision, calls=calls, events=events, refresh=refresh)


def test_successful_replica_refresh_replaces_superseded_staleness_deadline(replica_holder):
    scope, path, now, revision, calls, events = (replica_holder.scope, replica_holder.path, replica_holder.now,
                                               replica_holder.revision, replica_holder.calls, replica_holder.events)
    for observed in (100.0, 120.0, 140.0, 160.0):
        now[0] = observed
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE meta SET v=? WHERE k='last_ok'", (str(observed),))
        revision[0] += 1
        host._event_refresh(events)
    expected = 160.0 + ksa.HUB_STALE_AFTER_S + .001
    assert host._clock_deadlines[scope] == [expected], 'superseded healthy observations left clock chains'
    events.control_only = True
    now[0] = 100.0 + ksa.HUB_STALE_AFTER_S + .001
    assert host._event_refresh(events) == expected
    assert calls == [100.0, 120.0, 140.0, 160.0], 'old freshness boundary forced another read'
    now[0] = expected
    host._event_refresh(events)
    assert calls[-1] == expected, 'the final real stale transition was lost'


@pytest.mark.parametrize('failure', ['outer', 'reader'])
def test_failed_refresh_cannot_replace_published_clock(replica_holder, monkeypatch, failure):
    h = replica_holder
    host._event_refresh(h.events)
    old = 100.0 + ksa.HUB_STALE_AFTER_S + .001
    h.now[0] = 120.0
    h.revision[0] += 1
    with sqlite3.connect(h.path) as conn:
        conn.execute("UPDATE meta SET v='120' WHERE k='last_ok'")
    if failure == 'reader':
        from hermes_cli import kanban_replica_memo
        monkeypatch.setattr(ksa, '_remote_read_memo', {})
        def broken(*args, **kwargs):
            raise sqlite3.OperationalError('controlled detached projection failure')
        monkeypatch.setattr(kanban_replica_memo, 'finish', broken)
        def refresh(scope):
            try:
                h.refresh(scope)
            except sqlite3.OperationalError:
                pass  # display reader fallback; outer projection still succeeds
    else:
        def refresh(scope):
            h.refresh(scope)
            raise ValueError('controlled outer publication failure')
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    host._event_refresh(h.events)
    schedule = host._clock_deadlines[h.scope]
    assert old in schedule, 'failed observation discarded the published clock'
    assert 120.0 + ksa.HUB_STALE_AFTER_S + .001 not in schedule


def test_unread_replica_keeps_clock_while_other_source_changes(replica_holder, monkeypatch):
    h = replica_holder
    host._event_refresh(h.events)
    old = list(host._clock_deadlines[h.scope])
    h.now[0] = 120.0
    h.revision[0] += 1
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', lambda scope: None)
    host._event_refresh(h.events)
    assert host._clock_deadlines[h.scope] == old


def test_key_probe_without_output_publication_cannot_replace_clock(replica_holder, monkeypatch):
    h = replica_holder
    host._event_refresh(h.events)
    old = list(host._clock_deadlines[h.scope])
    h.now[0] = 120.0
    with sqlite3.connect(h.path) as conn:
        conn.execute("UPDATE meta SET v='120' WHERE k='last_ok'")
    def probe(scope):
        assert ksa._read_remote_links(h.path) == []
        return {name: 0 for name in parts.ALL}  # unchanged semantic keys; no projection
    monkeypatch.setattr(parts, 'keys', probe)
    host._event_refresh(h.events)  # filesystem wake actually probes, unlike a skipped control-only tick
    assert h.calls == [100.0]
    assert host._clock_deadlines[h.scope] == old, 'input probe borrowed a newer output clock'


def test_deadline_owners_legacy_clocks_and_confirmations_do_not_alias():
    from hermes_cli.kanban_annotation_deadlines import Plan
    plan = Plan([], (), 100.0)
    plan.append(150.0)  # legacy recovery deadline must survive same-valued ownership
    plan.replace(('replica', 'a'), [150.0], 100.0)
    plan.replace(('replica', 'b'), [150.0, 170.0], 100.0)
    first = plan.finish((150.0,))
    next_plan = Plan(first, (150.0,), 120.0)
    next_plan.replace(('replica', 'a'), [200.0], 120.0)
    assert next_plan.finish() == [150.0, 170.0, 200.0], 'peer owner was mistaken for a settle confirmation'
    assert not next_plan.expired
    expired = Plan(next_plan.finish(), (), 150.0)
    assert expired.expired, 'same-valued confirmation swallowed the real peer clock'
    assert expired.finish() == [170.0, 200.0]


def test_retry_preserves_owner_and_scope_independence():
    from hermes_cli.kanban_annotation_deadlines import Plan, retry
    first = Plan([], (), 100.0)
    first.replace(('replica', 'a'), [150.0], 100.0)
    schedule = first.finish()
    second = Plan(retry(schedule, 122.0, 120.0), (), 121.0)
    second.replace(('replica', 'a'), [200.0], 121.0)
    assert second.finish() == [122.0, 200.0], 'retry flattened owned clocks into unowned chains'
    assert schedule == [150.0], 'another scope mutated retained source evidence'


def test_successful_removal_and_earlier_observation_replace_only_own_clocks():
    from hermes_cli.kanban_annotation_deadlines import Plan
    first = Plan([], (), 100.0)
    first.replace(('replica', 'a'), [200.0], 100.0)
    first.replace(('replica', 'b'), [180.0], 100.0)
    next_plan = Plan(first.finish(), (), 110.0)
    next_plan.replace(('replica', 'a'), [150.0], 110.0)
    assert next_plan.finish() == [150.0, 180.0], 'older source evidence must not inherit a later expiry'
    next_plan.replace(('replica', 'a'), [], 110.0)
    assert next_plan.finish() == [180.0], 'removed/offline source cleared a peer deadline'


def test_legacy_holder_retains_conservative_deadlines(monkeypatch):
    pending = []
    monkeypatch.setattr(host._holder_ctx, 'deadlines', pending, raising=False)
    now = host.time.time()
    host.replace_deadlines(('replica', 'a'), [now + 10.0, now - 1.0])
    host.replace_deadlines(('replica', 'a'), [now + 20.0])
    assert pending == [now + 10.0, now + 20.0]


def test_real_heartbeat_renewal_replaces_only_previous_heartbeat_boundaries(replica_holder):
    h = replica_holder
    for observed in (100.0, 120.0):
        h.now[0] = observed
        h.revision[0] += 1
        info = {'gateway': 'remote', 'scope_heartbeat_period_s': 60,
                'task_scopes': [{'board': 'main', 'profile': 'default', 'state': 'ok', 'observed_at': observed}]}
        with sqlite3.connect(h.path) as conn:
            conn.execute('DELETE FROM meta')  # isolate the heartbeat from transport freshness
            conn.execute('DELETE FROM replica')
            conn.execute("INSERT INTO replica VALUES ('gateway','info','remote',?,1,?,?)",
                         (json.dumps(info), h.revision[0], h.revision[0]))
        host._event_refresh(h.events)
    beat, window = next(iter(ksa._scope_heartbeats([('remote', json.dumps(info))]).values()))
    assert host._clock_deadlines[h.scope] == [beat + window + .001]
    h.events.control_only = True
    h.now[0] = 100.0 + window + .001
    host._event_refresh(h.events)
    assert h.calls == [100.0, 120.0]
    h.now[0] = beat + window + .001
    host._event_refresh(h.events)
    assert h.calls[-1] == h.now[0], 'actual heartbeat expiry did not force a read'

