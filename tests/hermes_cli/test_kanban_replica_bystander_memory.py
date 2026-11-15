"""Bystanders retain only derived evidence, never task payloads. [daveey]"""
import importlib
import importlib.util
import sqlite3

import pytest

from tests.hermes_cli import test_kanban_replica_read_memo as _memo_tests

hub = _memo_tests.hub


def _relevance():
    name = 'hermes_cli.kanban_replica_relevance'
    assert importlib.util.find_spec(name) is not None, 'shared replica relevance definition is missing'
    return importlib.import_module(name)


def test_bystander_definition_is_gateway_wide_and_conservative():
    is_bystander = _relevance().is_bystander
    key = 'worker/build/t_one'
    assert is_bystander({}, key, 'mini', set())
    assert is_bystander({'sessions': [{'gw': 'other'}]}, key, 'mini', set())
    # Any local gateway link matters, even another profile or an unrecognized rel.
    assert not is_bystander({'sessions': [{'gw': 'mini', 'profile': 'other', 'rel': 'new'}]},
                            key, 'mini', set())
    assert not is_bystander({'origin_bindings': [{'binding': 'opaque'}]}, key, 'mini', set())
    assert not is_bystander({}, key, 'mini', {'build/t_one'})
    assert is_bystander({}, key, 'mini', {'build/t_other'})
    for bindings in (None, [], {}):
        assert is_bystander({'origin_bindings': bindings}, key, 'mini', set())
    for sessions in (None, {}, 'mini', [None, 'mini', 7]):
        assert is_bystander({'sessions': sessions}, key, 'mini', set())
    for malformed in ('build/t_one', 'worker/build/t_one/extra', '/build/t_one', 'worker//t_one',
                      'worker/build/', ''):
        assert not is_bystander({}, malformed, 'mini', set())


def test_payload_refetch_is_paged_parameterized_and_uses_callers_snapshot(tmp_path):
    relevance = _relevance()
    assert hasattr(relevance, 'fetch_payloads'), 'same-snapshot payload refetch is missing'
    path = tmp_path / 'replica.db'
    with sqlite3.connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('CREATE TABLE replica (gw, coll, key, data, updated_at, version, seq)')
        identities = [('owner', f"p/b/t_{i}'") for i in range(5)]
        writer.executemany('INSERT INTO replica VALUES (?, ?, ?, ?, ?, ?, ?)',
                           [(gw, 'tasks', key, 'original 😀', 1000, 4, i)
                            for i, (gw, key) in enumerate(identities)])
    reader = sqlite3.connect(path)
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM replica').fetchone()
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE replica SET data='new'")
        statements = []
        reader.set_trace_callback(statements.append)
        rows = list(relevance.fetch_payloads(reader, identities, 'version', page_size=2))
        assert len(rows) == 5
        assert {(r[0], r[1]) for r in rows} == set(identities)
        assert all(r[2:5] == ('original 😀', 1000, 4) for r in rows)
        assert all(r[6] == len('original 😀'.encode()) for r in rows)
        assert len([s for s in statements if s.startswith('SELECT')]) == 3
        assert reader.in_transaction
        assert list(relevance.fetch_payloads(reader, [], 'version')) == []
        with pytest.raises(ValueError):
            list(relevance.fetch_payloads(reader, identities, 'version; DROP TABLE replica'))
        with pytest.raises(ValueError):
            list(relevance.fetch_payloads(reader, identities, 'version', page_size=0))
    finally:
        reader.close()


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16le', 'utf-16be'])
def test_page_decoder_streams_one_row_without_widening_whole_page(encoding):
    import gc
    import json
    import tracemalloc

    name = 'hermes_cli.kanban_replica_transport'
    assert importlib.util.find_spec(name) is not None, 'bounded replica page decoder is missing'
    transport = importlib.import_module(name)
    row = ['owner', 'p/b/t', ('x' * 20000) + '\\"😀[]', 1000, 1, 1, 20000]
    packed = json.dumps([row] * 100, ensure_ascii=False).encode(encoding)
    gc.collect()
    tracemalloc.start()
    try:
        count = 0
        for decoded in transport.iter_page(packed, encoding):
            assert decoded == tuple(row)
            count += 1
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert count == 100
    assert peak < 400_000, (current, peak)


def test_refetch_transport_has_one_page_peak(tmp_path):
    import gc
    import tracemalloc
    path = tmp_path / 'refetch.db'
    payload = 'x' * 20000 + '😀'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE replica (gw, coll, key, data, updated_at, version, seq)')
        identities = [('owner', f'p/b/t_{i}') for i in range(100)]
        conn.executemany('INSERT INTO replica VALUES (?,?,?,?,?,?,?)',
                         [(gw, 'tasks', key, payload, 1000, 1, i)
                          for i, (gw, key) in enumerate(identities)])
        fetch = _relevance().fetch_payloads
        gc.collect()
        tracemalloc.start()
        try:
            count = sum(1 for _ in fetch(conn, identities, 'version'))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert count == 100
    assert peak < 1.25 * 100 * len(payload.encode()), peak


def test_cold_row_transport_has_one_page_peak(tmp_path):
    import gc
    import json
    import tracemalloc
    from hermes_cli import kanban_replica_rows as rows

    path = tmp_path / 'pages.db'
    payload = json.dumps({'body': 'x' * 20000 + '😀'}, ensure_ascii=False)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE replica (gw, coll, key, data, updated_at, version, seq)')
        conn.executemany('INSERT INTO replica VALUES (?, ?, ?, ?, ?, ?, ?)',
                         [('owner', 'tasks', f'p/b/t_{i}', payload, 1000, 1, i) for i in range(200)])
        probe = (200, 200, 199, 19900, 200, 200 * len(payload.encode()))
        page_bytes = rows.page_rows(probe) * len(payload.encode())
        gc.collect()
        tracemalloc.start()
        try:
            count = sum(1 for _ in rows._paged(conn, rows._FULL_SQL.format(version='version'), probe))
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert count == 200
    assert peak < 1.25 * page_bytes, (current, peak, page_bytes)


def test_snapshot_reader_retains_only_scalars_for_bystanders(tmp_path):
    import gc
    import json
    import tracemalloc
    from hermes_cli import kanban_replica_rows as rows
    from tests.hermes_cli.test_kanban_replica_read_memo import card

    path = tmp_path / 'scalars.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE replica (gw, coll, key, data, updated_at, version, seq)')
        for i in range(2020):
            task = card(f't_{i}', body='x' * 20000)
            task['sessions'] = [{'gw': 'mini' if i < 20 else 'elsewhere'}]
            conn.execute('INSERT INTO replica VALUES (?,?,?,?,?,?,?)',
                         ('owner', 'tasks', f'work/build/t_{i}', json.dumps(task), 1000000, 1, i + 1))
        probe = conn.execute("SELECT count(*),count(seq),max(seq),total(seq),total(version),"
                             "total(octet_length(data)) FROM replica").fetchone()
        rows.reset()
        gc.collect()
        tracemalloc.start()
        try:
            snapshot = rows.read(conn, path, 'version', probe, rows.file_ident(path), me='mini')
            view = snapshot.consume()
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(view.bystanders) == 2000
        assert len(view.relevant) == 20
        assert len(view.by_key) == 2020
        assert all(row[2] == b'' for rk, row in view.by_key.items() if rk in view.bystanders)
        assert current < 400 * 2000 + 100_000, (current, peak)
        assert peak < current + 1.25 * rows.page_rows(probe) * (probe[5] / probe[0]), (current, peak)
        assert snapshot.conn is None
        with pytest.raises(RuntimeError, match='consumed'):
            snapshot.consume()
    rows.reset()


def test_live_projection_bystander_retention_is_below_400_bytes_each(hub, monkeypatch):
    import gc
    import json
    import tracemalloc
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_replica_projection as projection
    from hermes_cli import kanban_replica_rows as rows
    from tests.hermes_cli.test_kanban_replica_rows_memory import _task

    path, _, statements, _ = hub
    traced_open = activity._open_ro
    def untraced_open(*args, **kwargs):
        conn = traced_open(*args, **kwargs)
        conn.set_trace_callback(None)
        return conn
    monkeypatch.setattr(activity, '_open_ro', untraced_open)
    def measure():
        statements.clear()
        gc.collect()
        tracemalloc.start()
        try:
            cards = activity._read_remote_links(path)
            statements.clear()  # observer SQL strings are not replica state
            gc.collect()
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return retained, peak, len(cards)

    with sqlite3.connect(path) as conn:
        for i in range(20):
            conn.execute("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)",
                         ('owner', f'work/build/t_{i * 100}', i + 1, json.dumps(_task(i * 100))))
    baseline, _, count = measure()
    assert count == 20
    activity._remote_read_memo = None
    projection.reset()
    rows.reset()
    with sqlite3.connect(path) as conn:
        for i in range(2000):
            task = _task(i * 100 + 1)
            conn.execute("INSERT INTO replica VALUES (?,'tasks',?,1,?,?,1000000)",
                         ('owner', f"work/build/{task['id']}", i + 21, json.dumps(task)))
    retained, peak, count = measure()
    assert count == 20
    assert retained - baseline < 400 * 2000, (retained, baseline, peak)
    with sqlite3.connect(path) as conn:
        probe = conn.execute(activity._REPLICA_PROBE).fetchone()
    assert peak < retained + 1.25 * rows.page_rows(probe) * (probe[5] / probe[0]), (retained, peak)
    view = rows._views[str(path)]
    assert len(view.bystanders) == 2000
    for state in projection._states.values():
        assert not (state.stages.keys() & view.bystanders.keys())
    shared = projection._shared.get(str(path), {})
    assert all(isinstance(shared[rk], bytes) for rk in view.bystanders)
    forbidden = {_task(i * 100 + 1)['body'] for i in range(2000)}
    _assert_payload_unreachable((rows._views, projection._states, projection._shared,
                                 activity._remote_read_memo), forbidden)


def _assert_payload_unreachable(roots, forbidden):
    import zlib
    stack, seen = list(roots), set()
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, str):
            assert value not in forbidden
        elif isinstance(value, bytes):
            try:
                text = zlib.decompress(value).decode('utf-8', 'surrogatepass')
            except (zlib.error, UnicodeError):
                continue
            import json
            assert json.loads(text).get('body') not in forbidden
        elif isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (tuple, list, set, frozenset)):
            stack.extend(value)
        elif type(value).__module__.startswith(('hermes_cli.', 'collections')):
            stack.extend(vars(value).values() if hasattr(value, '__dict__') else ())
            stack.extend(getattr(value, name) for name in getattr(type(value), '__slots__', ())
                         if hasattr(value, name))


def test_unfiltered_read_after_filtered_snapshot_restores_real_payloads(hub):
    from hermes_cli import kanban_replica_rows as rows
    from hermes_cli import kanban_session_activity as activity
    path, put, _, _ = hub
    put('t_one', sessions=[])
    activity._read_remote_links(path)
    with sqlite3.connect(path) as conn:
        conn.execute('BEGIN')
        probe = conn.execute(activity._REPLICA_PROBE).fetchone()
        view = rows.read(conn, path, 'version', probe, rows.file_ident(path))
        assert 't_one' in view.rows()[0][2]


def test_alias_insert_and_delete_reclassify_unchanged_seq(hub):
    import json
    from hermes_cli import kanban_replica_rows as rows
    from hermes_cli import kanban_session_activity as activity
    from tests.hermes_cli.test_kanban_replica_read_memo import card, live_and_fresh
    path, put, _, clock = hub
    rk = ('owner', 'work/build/t_one')
    from tests.hermes_cli.test_kanban_remote_terminal_release import observed
    from tests.hermes_cli.test_kanban_replica_read_memo_heartbeat import set_info, ok
    put('t_one', **{k: v for k, v in observed(card('t_one', sessions=[], created_at=10,
                                                  status='done')).items() if k != 'id'})
    set_info(path, [ok(1000.0)])
    assert activity._read_remote_links(path) == []
    assert rk in rows._views[str(path)].bystanders
    with sqlite3.connect(path) as conn:
        seq = conn.execute('SELECT seq FROM replica WHERE key=?', (rk[1],)).fetchone()[0]
        conn.execute("INSERT INTO replica VALUES ('owner','tasks','build/t_one',1,102,?,1000000)",
                     (json.dumps({k: v for k, v in card('t_one', status='done', created_at=10).items()
                                  if k not in ('gateway', 'profile')}),))
    live, full, owners, full_owners = live_and_fresh(path, clock)
    assert live == full and owners == full_owners
    assert [(c['key'], c['legacy_key'], c['released']) for c in live] == [
        ('owner/work/build/t_one', 'owner/build/t_one', True)]
    assert rk in rows._views[str(path)].relevant
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT seq FROM replica WHERE key=?', (rk[1],)).fetchone()[0] == seq
        conn.execute("DELETE FROM replica WHERE key='build/t_one'")
    live, full, owners, full_owners = live_and_fresh(path, clock)
    assert live == full == [] and owners == full_owners
    assert rk in rows._views[str(path)].bystanders


def test_scalar_closed_membership_does_not_stringify_identity_parts():
    import marshal
    from hermes_cli.kanban_replica_aggregates import EvidenceSet
    packed = marshal.dumps((1, 1, 1, 1000, 10, b'digest', 3))
    closed = EvidenceSet((), {('owner', 'None/build/t_one'): packed}, 'closed', False,
                         frozenset({('owner', 'None', 'build')}))
    assert ('owner', 'None', 'build', 't_one') in closed
    assert ('owner', None, 'build', 't_one') not in closed


def test_filtered_rows_raw_access_is_explicitly_rejected(hub):
    from hermes_cli import kanban_replica_rows as rows
    from hermes_cli import kanban_session_activity as activity
    path, put, _, _ = hub
    put('t_one', sessions=[])
    activity._read_remote_links(path)
    with pytest.raises(RuntimeError, match='snapshot'):
        rows._views[str(path)].rows()


def test_unchanged_relevant_rows_are_not_decoded_again(hub, monkeypatch):
    from hermes_cli import kanban_replica_scalar_rows as scalar
    from hermes_cli import kanban_session_activity as activity
    path, put, _, _ = hub
    put('t_one')
    assert len(activity._read_remote_links(path)) == 1
    original = scalar.derive
    decoded = []
    def tracked(raw, *args):
        decoded.append(raw[1])
        return original(raw, *args)
    monkeypatch.setattr(scalar, 'derive', tracked)
    put('t_two', sessions=[])
    assert len(activity._read_remote_links(path)) == 1
    assert decoded == ['work/build/t_two']


def test_last_link_demotion_releases_all_profiles_payloads(hub, monkeypatch):
    from hermes_cli import kanban_replica_projection as projection
    from hermes_cli import kanban_replica_rows as rows
    from hermes_cli import kanban_session_activity as activity
    path, put, _, _ = hub
    rk = ('owner', 'work/build/t_one')
    put('t_one', body='payload-sentinel' * 2000)
    for profile in ('default', 'other'):
        monkeypatch.setattr('hermes_cli.profiles.get_active_profile_name', lambda: profile)
        activity._read_remote_links(path)
    put('t_one', sessions=[], body='payload-sentinel' * 2000)
    assert activity._read_remote_links(path) == []
    assert rk in rows._views[str(path)].bystanders
    assert all(rk not in state.stages for state in projection._states.values())


def test_legacy_reader_borrows_closed_and_bulk_resolves_publications(hub, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_replica_aggregates as aggregates
    path, put, _, _ = hub
    put('t_one', sessions=[], created_at=10)
    activity._read_remote_links(path)
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda *args: [])
    def no_scan(*args):
        pytest.fail('remembered-card path scanned an all-row aggregate')
    monkeypatch.setattr(aggregates.EvidenceSet, '__iter__', no_scan)
    monkeypatch.setattr(aggregates.Published, '__getitem__', no_scan)
    activity._remote_last = [{'id': 't_one', 'key': 'other/work/build/t_one', 'gateway': 'other',
                              'profile': 'work', 'owner_profile': 'work', 'board': 'build',
                              'session': 's1', 'created_at': 10, 'ownership_state': 'qualified'}]
    activity._remote_scope = activity._origin_scope()
    activity._legacy_remote_links()


@pytest.mark.parametrize('overrides,live', [
    ({'ownership_state': 'unknown'}, True),
    ({'created_at': None}, True),
    ({}, False),
    ({'created_at': []}, True),
    ({'created_at': {}}, True),
], ids=['ownership', 'missing-created', 'dead-scope', 'list-created', 'dict-created'])
def test_remembered_publication_filters_each_ineligible_key(overrides, live):
    from hermes_cli.kanban_replica_aggregates import Published, remembered_publications

    eligible = {'id': 't_good', 'gateway': 'owner', 'owner_profile': 'work',
                'board': 'build', 'created_at': 10, 'ownership_state': 'qualified'}
    rejected = dict(eligible, id='t_bad', **overrides)
    # A separate live scope keeps the positive control eligible in every case.
    eligible['gateway'] = 'control'
    scopes = {('control', 'work', 'build')}
    if live:
        scopes.add(('owner', 'work', 'build'))
    good_key = ('build', 't_good', 10)
    calls = []

    class RecordingPublished(Published):
        def lookup_many(self, keys):
            calls.append(set(keys))
            return super().lookup_many(keys)

    moved = {'published': RecordingPublished({good_key: {'control'}}, {}),
             'live_scopes': scopes}
    remembered_publications(moved, [rejected, eligible])
    assert calls == [{good_key}]
    assert moved['published'] == {good_key: frozenset({'control'})}


@pytest.mark.parametrize('ownership,created', [('unknown', []), ('unknown', {}), ('qualified', [])])
def test_ineligible_remembered_publication_does_not_poison_reads(hub, monkeypatch, ownership, created):
    from hermes_cli import kanban_session_activity as activity
    path, put, _, _ = hub
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda *args: [])
    put('t_current')
    previous = [{'id': 't_old', 'key': 'absent/build/t_old', 'gateway': 'absent',
                 'owner_profile': 'work', 'board': 'build', 'session': 's1',
                 'created_at': created, 'ownership_state': ownership}]
    activity._remote_last = previous
    activity._remote_scope = activity._origin_scope()
    first = activity._legacy_remote_links()
    assert any(c['id'] == 't_current' and not c.get('stale') for c in first)
    put('t_next')
    second = activity._legacy_remote_links()
    assert any(c['id'] == 't_next' and not c.get('stale') for c in second)


@pytest.mark.parametrize('outcome', ['closed', 'moved', 'same-gateway-profile'])
def test_remembered_bystander_retirement_matches_full_reader(hub, monkeypatch, outcome):
    import json
    from hermes_cli import kanban_session_activity as activity
    from tests.hermes_cli.test_kanban_replica_read_memo import card
    from tests.hermes_cli.test_kanban_remote_terminal_release import observed
    from tests.hermes_cli.test_kanban_replica_read_memo_heartbeat import set_info, ok
    path, put, _, clock = hub
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    monkeypatch.setattr(activity, '_remote_snapshot', lambda *args: [])
    put('t_one', created_at=10)
    previous = activity._legacy_remote_links()
    assert len(previous) == 1
    set_info(path, [ok(1000.0)])
    target_gw = 'elsewhere' if outcome == 'moved' else 'owner'
    profile = 'new' if outcome == 'same-gateway-profile' else 'work'
    task = card('t_one', gateway=target_gw, profile=profile, sessions=[], created_at=10,
                status='done' if outcome == 'closed' else 'running')
    task = observed(task)
    task['origin_observation']['verified'] = True
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM replica WHERE coll='tasks'")
        conn.execute("INSERT INTO replica VALUES (?,'tasks',?,2,103,?,1000000)",
                     (target_gw, f'{profile}/build/t_one', json.dumps(task)))
    scope = activity._origin_scope()
    activity._remote_last, activity._remote_scope = previous, scope
    live = activity._legacy_remote_links()
    original = activity._read_remote_links
    def full(path, **kwargs):
        return original(path, now=clock['now'], **kwargs)
    monkeypatch.setattr(activity, '_read_remote_links', full)
    activity._remote_last, activity._remote_scope = previous, scope
    explicit = activity._legacy_remote_links()
    assert live == explicit
    assert len(live) == (1 if outcome == 'same-gateway-profile' else 0)


