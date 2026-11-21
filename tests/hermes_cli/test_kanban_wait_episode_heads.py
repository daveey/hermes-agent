"""Changed commit head starts a new CI/deploy wait episode; unchanged head keeps it."""
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wait_episode as episode

@pytest.mark.parametrize('kind', ['ci', 'deploy'])
@pytest.mark.parametrize('changed_head', [False, True])
def test_actual_schedule_head_identity(tmp_path, monkeypatch, kind, changed_head):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    awaiting = {'kind': kind, 'ref': 'https://github.com/example/repo/pull/123'}
    with kbc.connect_closing() as db:
        tid = kb.create_task(db, title='Review wait fixture', assignee='alice')
        for at, sha in [(100, 'a' * 40), (200, ('b' if changed_head else 'a') * 40)]:
            if at == 200:
                assert kb.unblock_task(db, tid)
            assert kb.schedule_task(db, tid, reason='CI pending', awaiting=awaiting,
                                    wake_check=f'test -f {home}/{sha}')
            # Deterministic times on real lifecycle-emitted events.
            db.execute("UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='scheduled')", (at, tid))
        rows = db.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scheduled' ORDER BY id", (tid,)).fetchall()
        premises = [json.loads(row[0])['awaiting']['premise'] for row in rows]
        assert (premises[0] != premises[1]) == changed_head
        actual = episode.read(db, tid)
        assert actual == {'kind': kind, 'since': 200.0 if changed_head else 100.0}, (premises, actual)
