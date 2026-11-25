"""Real holder part reads, not only equality of counter values."""
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
