"""Alarm urgency is source-owned metadata, not a title or stored mutation."""
import pytest

from hermes_cli import kanban_admission_score as score
from tests.hermes_cli import test_kanban_score_sources as helpers

sources = helpers.sources


def alarm(conn, tid, key, priority=0):
    row = helpers.add(conn, tid, 1000, priority)
    columns = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
    if 'idempotency_key' not in columns:
        conn.execute('ALTER TABLE tasks ADD COLUMN idempotency_key TEXT')
    conn.execute('UPDATE tasks SET idempotency_key=? WHERE id=?', (key, tid))
    return row


@pytest.mark.parametrize('key', ['worker-claim-watch:worker-3', 'pickup-stall:hub:t_x:12',
                                 'mods-suite-red:guard:abcdef123456'])
def test_alarm_floor_without_stored_mutation(sources, key):
    local, remote = sources
    row = alarm(remote, 'alarm', key)
    (_, parts), = score.rank(local, [row], now_ms=1000000, weights={})
    assert parts.effective_priority == 900
    assert score.effective_priorities(local, ['alarm']) == {'alarm': 900}
    assert remote.execute('SELECT priority FROM tasks').fetchone()[0] == 0


@pytest.mark.parametrize('key', [None, 'worker-claim-watchdog:x', 'pickup-stall',
                                 'not:mods-suite-red:x', 'ordinary'])
def test_non_alarm_has_no_floor(sources, key):
    local, _ = sources
    row = alarm(local, 'ordinary', key)
    (_, parts), = score.rank(local, [row], now_ms=1000000, weights={})
    assert parts.effective_priority == 0


def test_local_metadata_revokes_remote_alarm_floor(sources):
    local, remote = sources
    row = alarm(remote, 'same', 'pickup-stall:hub:x:1')
    helpers.add(local, 'same', 1000)
    (_, parts), = score.rank(local, [row], now_ms=1000000, weights={})
    assert parts.effective_priority == 0


@pytest.mark.parametrize("stored,expected", [(0, 900), (950, 950), (60000, 1000)])
def test_alarm_floor_donates_to_dependency_and_preserves_higher_priority(sources, stored, expected):
    local, _ = sources
    parent = helpers.add(local, 'parent', 1000)
    alarm(local, 'alarm', 'mods-suite-red:suite:sha', stored)
    local.execute("INSERT INTO task_links VALUES ('parent', 'alarm')")
    (_, parts), = score.rank(local, [parent], now_ms=1000000, weights={})
    assert parts.effective_priority == expected
    assert score.effective_priorities(local, ['parent']) == {'parent': expected}


def test_terminal_alarm_does_not_donate(sources):
    local, _ = sources
    helpers.add(local, 'parent', 1000)
    alarm(local, 'alarm', 'pickup-stall:hub:x:1')
    local.execute("UPDATE tasks SET status='done' WHERE id='alarm'")
    local.execute("INSERT INTO task_links VALUES ('parent', 'alarm')")
    assert score.effective_priorities(local, ['parent']) == {'parent': 0}
