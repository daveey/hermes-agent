"""Real holder part reads, not only equality of counter values."""
import pytest

from hermes_cli import kanban_annotation_revision as revision
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_parts as helpers

world = helpers.world


def _install(world):
    world.conn.execute('PRAGMA journal_mode=WAL')
    world.conn.execute('ALTER TABLE tasks ADD COLUMN last_heartbeat_at REAL')
    world.conn.execute('CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT)')
    revision.ensure_revisions(world.conn)
    # Custody requires the inner reader's certificate too, as normal board init installs.
    ksa._revision.ensure_revision(world.conn, ksa._revision.LOCAL_TABLES)


def test_holder_batches_local_parts_with_identical_keys(world, monkeypatch):
    from hermes_cli import kanban_annotation_parts as parts

    _install(world)
    expected = {p: parts._part_fingerprint(p) for p in ('counts', 'links', 'attention')}
    calls = []
    original = revision.content_keys

    def counted(path, requested=('counts', 'links', 'attention')):
        calls.append((path, tuple(requested)))
        return original(path, requested)

    monkeypatch.setattr(revision, 'content_keys', counted)
    result = parts.keys(helpers.SCOPE)
    assert result['counts'][0] == expected['counts']
    assert result['links'][0] == expected['links']
    assert result['attention'][0] == expected['attention']
    assert calls == [(world.board, ('counts', 'links', 'attention'))]


def test_holder_keys_carry_the_hub_signature_for_every_local_part(world, monkeypatch, tmp_path):
    """Every part covers actual task movement; only COUNTS uses the custody-backed hub key."""
    import sqlite3
    from hermes_cli import kanban_annotation_parts as parts
    from hermes_cli import kanban_annotation_hub_key as hub_keys

    _install(world)
    hub = tmp_path / 'hub.db'
    with sqlite3.connect(hub) as conn:
        conn.execute('CREATE TABLE replica (gw TEXT,coll TEXT,key TEXT,seq INTEGER,data TEXT)')
        conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY,v TEXT)')
        conn.execute("INSERT INTO replica VALUES ('fixture','tasks','default/t1',1,'{}')")
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: hub)
    before = parts.keys(helpers.SCOPE)
    for part in ('links', 'attention'):
        assert before[part][0] == parts._part_fingerprint(part)
    assert before['counts'][0][-1] == hub_keys.capture(hub)[0]
    assert before.receipt.valid()
    assert parts.keys(helpers.SCOPE) == before
    with sqlite3.connect(hub) as conn:
        conn.execute('UPDATE replica SET seq=2')
    assert not before.receipt.valid()
    after = parts.keys(helpers.SCOPE)
    for part in ('counts', 'links', 'attention'):
        assert after[part][0] != before[part][0]
        assert after[part][0][:-1] == before[part][0][:-1]
    for part in ('links', 'attention'):
        assert after[part][0] == parts._part_fingerprint(part)
    assert after['counts'][0][-1] == hub_keys.capture(hub)[0]


def test_irrelevant_board_storm_reads_no_part(world):
    _install(world)
    # Complete declaration: this check reads only its card and the explicit file.
    dispose = ksa.register_done_check('fixture', ksa._done_checks['fixture'],
        watch_paths=lambda: [world.declared], next_deadline=lambda: None,
        inputs=lambda: world.declared.read_text())
    try:
        helpers._refresh()
        before = helpers._reads(world)
        for n in range(12):
            world.conn.execute("UPDATE tasks SET last_heartbeat_at=? WHERE id='t1'", (n,))
            world.conn.execute("INSERT INTO task_comments(task_id,body) VALUES('t1',?)", (str(n),))
            helpers._refresh()
        assert helpers._reads(world) == before
        # No source notification: the confirmation/control probe still discovers a relevant commit.
        world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
        host._clock_deadlines[helpers.SCOPE] = [0.0]
        host._settle_deadlines[helpers.SCOPE] = (0.0,)
        helpers._refresh(source=False)
        assert helpers._reads(world)[0] == before[0] + 1
        assert helpers._published()[0] == {}
    finally:
        dispose()


def test_undeclared_comment_reader_is_never_certified(world):
    _install(world)
    def check(card):
        world.checks += 1
        return bool(world.conn.execute('SELECT 1 FROM task_comments').fetchone())
    dispose = ksa.register_done_check('fixture', check,
        watch_paths=lambda: [world.board], next_deadline=lambda: None)
    try:
        helpers._refresh()
        before = world.checks
        world.conn.execute("INSERT INTO task_comments(task_id,body) VALUES('t1','new')")
        helpers._refresh()
        assert world.checks > before
        assert helpers._published()[10] == {'s1': 1}
    finally:
        dispose()


@pytest.mark.parametrize('wrap', [lambda v: v, lambda v: {'nested': [v]}], ids=['array', 'nested-array'])
def test_complete_done_inputs_preserve_stat_tagged_arrays(world, wrap):
    dispose = ksa.register_done_check('fixture', ksa._done_checks['fixture'],
        watch_paths=lambda: [world.declared], next_deadline=lambda: None,
        inputs=lambda: wrap(['stat', 'dependency', world.flagged, 0]))
    try:
        helpers._refresh()
        assert helpers._published()[10] == {'s1': 1}
        before = world.checks
        world.flagged = False
        world.declared.write_text('changed')
        helpers._refresh()
        assert world.checks > before
        assert helpers._published()[10] == {}
    finally:
        dispose()


@pytest.mark.parametrize('before,after', [(True, 1), (False, 0), (1, 1.0)],
                         ids=['bool-int-true', 'bool-int-false', 'int-float'])
def test_done_inputs_scalar_types_refresh_real_holder(world, before, after):
    box = [before]

    def check(card):
        world.checks += 1
        return type(box[0]) is type(before)

    dispose = ksa.register_done_check('fixture', check,
        watch_paths=lambda: [world.declared], next_deadline=lambda: None,
        inputs=lambda: {'nested': [box[0]]})
    try:
        helpers._refresh()
        assert helpers._published()[10] == {'s1': 1}
        reads = world.checks
        box[0] = after
        world.declared.write_text('changed type')
        helpers._refresh()
        assert world.checks > reads
        assert helpers._published()[10] == {}
    finally:
        dispose()


def test_missing_revision_forces_fresh_read(world):
    _install(world)
    helpers._refresh()
    before = world.counts
    world.conn.execute('DROP TRIGGER kanban_annotation_counts_tasks_update')
    helpers._refresh()
    assert world.counts == before + 1
    world.conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
    host._clock_deadlines[helpers.SCOPE] = [0.0]
    host._settle_deadlines[helpers.SCOPE] = (0.0,)
    helpers._refresh(source=False)
    assert helpers._published()[0] == {}
