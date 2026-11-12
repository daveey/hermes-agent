"""Normal queue churn is accepted without weakening migration CAS."""
import copy

import pytest

from hermes_cli import kanban_priority_executor as executor
from hermes_cli import kanban_priority_prepare as prepare
from hermes_cli import kanban_priority_migration as migration
from tests.hermes_cli import test_kanban_priority_executor as fixtures
from tests.hermes_cli import test_kanban_priority_verify as canonical

kanban_home = fixtures.kanban_home
conn = fixtures.conn


@pytest.mark.parametrize('undo', [False, True])
@pytest.mark.parametrize('status', ['done', 'archived'])
def test_departure_preserves_value_and_updates_remaining(conn, undo, status):
    ids, attachment, digest = fixtures.setup_plan(conn)
    if undo:
        executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME)
    before = fixtures.helpers.priorities(conn, ids)
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, ids[2]))
    conn.commit()
    result = executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME,
                                     undo=undo, allow_legacy_rollback=True)
    assert result['changed'] == 1
    assert result['skipped'] == [{'id': ids[2], 'status': status, 'priority': before[2]}]
    assert fixtures.helpers.priorities(conn, ids) == [10000 if undo else 450, 10000, before[2]]
    assert executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME,
                                  undo=undo, allow_legacy_rollback=True)['changed'] == 0


@pytest.mark.parametrize('undo', [False, True])
@pytest.mark.parametrize('priority', [-1, 0, 1000, 1001])
def test_arrival_cap_and_atomicity(conn, undo, priority):
    ids, attachment, digest = fixtures.setup_plan(conn)
    if undo:
        executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME)
    extra = fixtures.helpers.legacy(conn, [priority])[0]
    conn.execute('UPDATE tasks SET created_on=? WHERE id=?', (fixtures.sync_helpers.ME, extra))
    conn.commit()
    before = fixtures.helpers.priorities(conn, ids)
    if priority > 1000:
        with pytest.raises(ValueError, match='exceeds cap'):
            executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME,
                                    undo=undo, allow_legacy_rollback=True)
        assert fixtures.helpers.priorities(conn, ids) == before
    else:
        result = executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME,
                                         undo=undo, allow_legacy_rollback=True)
        assert result['arrivals'] == [{'id': extra, 'origin': fixtures.sync_helpers.ME, 'priority': priority}]
        assert fixtures.helpers.priorities(conn, ids)[0] == (10000 if undo else 450)
    assert fixtures.helpers.priorities(conn, [extra]) == [priority]


@pytest.mark.parametrize('undo', [False, True])
@pytest.mark.parametrize('change', ['owner', 'incoming-owner', 'terminal-owner', 'value'])
def test_owner_and_open_value_remain_fenced(conn, undo, change):
    ids, attachment, digest = fixtures.setup_plan(conn)
    if undo:
        executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME)
    if change == 'incoming-owner':
        conn.execute('UPDATE tasks SET created_on=?,priority=0 WHERE id=?', (fixtures.sync_helpers.ME, ids[1]))
    elif change == 'value':
        conn.execute('UPDATE tasks SET priority=777 WHERE id=?', (ids[2],))
    else:
        conn.execute("UPDATE tasks SET created_on='other' WHERE id=?", (ids[2],))
        if change == 'terminal-owner':
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (ids[2],))
    conn.commit()
    before = fixtures.helpers.priorities(conn, ids)
    with pytest.raises(ValueError, match='changed'):
        executor.apply_attached(conn, attachment, digest, writer=fixtures.sync_helpers.ME,
                                undo=undo, allow_legacy_rollback=True)
    assert fixtures.helpers.priorities(conn, ids) == before


@pytest.mark.parametrize('undo', [False, True])
@pytest.mark.parametrize('change', ['done', 'archived', 'arrival', 'above-cap', 'owner', 'value', 'missing'])
def test_canonical_churn_both_directions(undo, change):
    rows = canonical.inventory()
    manifest = migration.plan_global(rows, board_id='board', exceptions=[])
    if not undo:
        for row, planned in zip(rows, manifest['rows']):
            row['priority'] = planned['new']
    if change in ('done', 'archived'):
        rows[0]['status'] = change
    elif change in ('arrival', 'above-cap'):
        rows.append({'id': 'd', 'created_on': 'new-origin', 'status': 'ready',
                     'priority': 1000 if change == 'arrival' else 1001})
    else:
        actions = {
            'owner': lambda: rows[0].update(created_on='other'),
            'value': lambda: rows[0].update(priority=777),
            'missing': lambda: rows.pop(0),
        }
        actions[change]()
    before = copy.deepcopy(rows)
    if change in ('above-cap', 'owner', 'value', 'missing'):
        with pytest.raises(ValueError, match='canonical'):
            prepare.verify(canonical.Canonical(rows), manifest, undo=undo)
    else:
        result = prepare.verify(canonical.Canonical(rows), manifest, undo=undo)
        if change == 'arrival':
            assert result['arrivals'] == [{'id': 'd', 'origin': 'new-origin', 'priority': 1000}]
            assert result['verified'] == 2
        else:
            assert result['skipped'] == [{'id': 'a', 'status': change, 'priority': rows[0]['priority']}]
            assert result['verified'] == 1
    assert rows == before
