"""Independent real forwarding producer to common creation boundary."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_placement_forward import remote_args
from tests.hermes_cli import test_kanban_model_tier as fixtures
board = fixtures.board

@pytest.mark.parametrize('where', ['control', 'title', 'parent_title', 'parent_summary', 'parent_handoff'])
@pytest.mark.parametrize('legacy', ['Independent primary Opus review required',
                                  'Must use "claude-opus-5-5" only\nrequire_model: true'])
def test_forwarded_history_remains_creatable(board, where, legacy):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title='source work', assignee='default')
        tid = kb.create_task(conn, title='ordinary forwarded work', assignee='default', parents=[parent])
        if where == 'title':
            conn.execute('UPDATE tasks SET title=? WHERE id=?', (legacy, tid))
        elif where == 'parent_title':
            conn.execute('UPDATE tasks SET title=? WHERE id=?', (legacy, parent))
        elif where == 'parent_summary':
            conn.execute('UPDATE tasks SET result=? WHERE id=?', (legacy, parent))
        elif where == 'parent_handoff':
            kb.complete_task(conn, parent, summary=legacy, force=True)
        conn.commit()
        args = remote_args(conn, kb.get_task(conn, tid), self_gw='source')
        created = kb.create_task(conn, title=args['title'], body=args['body'], assignee=args['assignee'],
                                 model_override=args['model'], provider_override=args['provider'])
        assert kb.get_task(conn, created) is not None
        assert (args['model'], args['provider']) == (None, None)
        source = kb.get_task(conn, tid)
        assert source is not None
        if where == 'title':
            assert args['title'] == f'Continue forwarded work {tid}'
            assert source.title == legacy
        else:
            assert args['title'] == source.title == 'ordinary forwarded work'
        if where != 'control':
            from hermes_cli.kanban_model_neutral import _EVIDENCE
            import json
            restored = '\n'.join(json.loads(line.lstrip()[len(_EVIDENCE):])
                                 if line.lstrip().startswith(_EVIDENCE) else line
                                 for line in args['body'].splitlines())
            assert legacy in restored
            parent_source = kb.get_task(conn, parent)
            assert parent_source is not None
            preserved = (source.title if where == 'title' else parent_source.title if where == 'parent_title'
                         else kb.latest_summaries(conn, [parent])[parent] if where == 'parent_handoff'
                         else parent_source.result)
            assert legacy == preserved
        # Serialized history does not exempt newly appended active instructions.
        before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
        for field in ('title', 'body'):
            with pytest.raises(ValueError, match='model-neutral'):
                kb.create_task(conn, title=legacy if field == 'title' else args['title'],
                               body=args['body'] + ('\nMust use claude-opus-5-5 only' if field == 'body' else ''),
                               assignee=args['assignee'])
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
