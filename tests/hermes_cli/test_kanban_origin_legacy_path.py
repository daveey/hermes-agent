"""Task-graph aliases require exact publication evidence, not inferred owners."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_ledger as ledger, kanban_origin_legacy as legacy
from tests.hermes_cli.test_kanban_origin_legacy import world as _world_fixture

world = _world_fixture


def graph(world, depth=3):
    nodes = []
    with sqlite3.connect(world['replica']) as db:
        leaf = json.loads(db.execute("SELECT data FROM replica WHERE key='build/t_one'").fetchone()[0])
        links = leaf.pop('sessions')
        for index in range(depth):
            task_id = 't_one' if index == depth - 1 else f't_root{index}'
            child = 't_one' if index == depth - 2 else f't_root{index + 1}'
            task = leaf if index == depth - 1 else {'id': task_id, 'board': 'build', 'children': [child]}
            if index == 0:
                task['sessions'] = links
            key = f'build/{task_id}'
            db.execute('DELETE FROM replica WHERE key=?', (key,))
            db.execute("INSERT INTO replica VALUES ('owner','tasks',?,2,?,100000)", (key, json.dumps(task)))
            nodes.append({'key': key, 'version': 2, 'digest': legacy.record_digest(task)})
    world['legacy_alias'].update(nodes[-1])
    world['legacy_alias']['path'] = nodes
    return nodes


@pytest.mark.parametrize('contradiction', ['origin', 'edge'])
def test_known_contradiction_is_not_masked_by_missing_descendant(world, contradiction):
    nodes = graph(world)
    with sqlite3.connect(world['replica']) as db:
        root = json.loads(db.execute('SELECT data FROM replica WHERE key=?', (nodes[0]['key'],)).fetchone()[0])
        root['sessions' if contradiction == 'origin' else 'children'] = []
        nodes[0]['digest'] = legacy.record_digest(root)
        db.execute('UPDATE replica SET data=? WHERE key=?', (json.dumps(root), nodes[0]['key']))
        db.execute('DELETE FROM replica WHERE key=?', (nodes[1]['key'],))
    with pytest.raises(ValueError, match='origin_legacy_association_mismatch'):
        ledger.accept_binding(**world)
    assert ledger.receipt(world['path'], world['request_id']) is None


@pytest.mark.parametrize('depth', [2, 3, 32])
def test_inherited_alias_receipt_and_projection_preserve_obligation(world, depth):
    nodes = graph(world, depth)
    from hermes_cli.kanban_session_activity import _read_remote_links
    before = _read_remote_links(world['replica'], now=100)
    leaf = next(c for c in before if c.get('legacy_record', {}).get('key') == 'build/t_one')
    ack = ledger.accept_binding(**world)
    assert ack['legacy_alias']['path'] == nodes
    rows = ledger.project(world['path'], replica=world['replica'], gateway='origin', profile='default', now=100)
    assert rows[0]['unfinished'] and legacy.supersedes(leaf, rows[0])
    root = next(c for c in before if c.get('legacy_record', {}).get('key') == nodes[0]['key'])
    assert not legacy.supersedes(root, rows[0])
    world['replica'].unlink()
    assert ledger.accept_binding(**world) == ack
    world['legacy_alias']['path'][0]['version'] += 1
    with pytest.raises(ValueError, match='origin_binding_conflict'):
        ledger.accept_binding(**world)


@pytest.mark.parametrize('change', ['missing', 'behind', 'digest', 'edge', 'origin', 'profile', 'gateway', 'board'])
def test_path_publication_is_checked_at_every_hop(world, change):
    nodes = graph(world)
    key = nodes[0]['key']
    with sqlite3.connect(world['replica']) as db:
        task = json.loads(db.execute('SELECT data FROM replica WHERE key=?', (key,)).fetchone()[0])
        if change == 'missing':
            db.execute('DELETE FROM replica WHERE key=?', (key,))
        elif change == 'behind':
            db.execute('UPDATE replica SET version=1 WHERE key=?', (key,))
        else:
            updates = {'digest': {'title': 'changed'}, 'edge': {'children': []},
                       'origin': {'sessions': []}, 'profile': {'profile': 'work'},
                       'gateway': {'gateway': 'foreign'}, 'board': {'board': 'foreign'}}
            task.update(updates[change])
            db.execute('UPDATE replica SET data=? WHERE key=?', (json.dumps(task), key))
            if change != 'digest':
                nodes[0]['digest'] = legacy.record_digest(task)
    if change in ('missing', 'behind'):
        assert ledger.accept_binding(**world)['status'] == 'pending'
    else:
        with pytest.raises(ValueError, match='origin_legacy_'):
            ledger.accept_binding(**world)
    assert ledger.receipt(world['path'], world['request_id']) is None


@pytest.mark.parametrize('change', ['cycle', 'short', 'long', 'namespace', 'boolean', 'extra', 'leaf', 'null', 'bad_key'])
def test_malformed_path_never_creates_ledger(world, change):
    nodes = graph(world)
    mutations = {
        'bad_key': [{**nodes[0], 'key': 'build/..'}, *nodes[1:]],
        'cycle': [nodes[0], nodes[0], nodes[-1]], 'short': [nodes[-1]],
        'long': nodes * 11, 'namespace': [{**nodes[0], 'key': 'other/t_root0'}, *nodes[1:]],
        'boolean': [{**nodes[0], 'version': True}, *nodes[1:]],
        'extra': [{**nodes[0], 'extra': 1}, *nodes[1:]], 'leaf': nodes[:-1], 'null': None,
    }
    world['legacy_alias']['path'] = mutations[change]
    with pytest.raises(ValueError, match='origin_legacy_invalid'):
        ledger.accept_binding(**world)
    assert not world['path'].exists()


@pytest.mark.parametrize('action,has_path,accepted', [
    ('origin-reconcile-legacy-path', True, True),
    ('origin-reconcile-legacy-path', False, False),
    ('origin-reconcile-legacy', True, False),
    ('origin-bind', True, False),
])
def test_path_requires_distinct_wire_action(world, tmp_path, monkeypatch, action, has_path, accepted):
    from hermes_cli import kanban_origin_adapter as adapter, profiles
    import hermes_constants
    home = tmp_path / 'home'
    home.mkdir()
    world['path'] = home / 'kanban/origin-ledger.db'
    cfg = tmp_path / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'origin'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_HUB_STATE', str(tmp_path))
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(adapter, '_resolve_session', lambda path, sid: 's_tip')
    if has_path:
        graph(world)
    request = {'id': 'request-one', 'from': 'gw:owner', 'to_gw': 'origin',
               'coll': 'sessions', 'key': 'default/s_old', 'action': action,
               'args': {'binding': world['binding'], 'legacy_alias': world['legacy_alias']}}
    if accepted:
        assert adapter.receive(request)['legacy_alias'] == world['legacy_alias']
    else:
        with pytest.raises(ValueError, match='origin_legacy_invalid'):
            adapter.receive(request)
        assert not world['path'].exists()
