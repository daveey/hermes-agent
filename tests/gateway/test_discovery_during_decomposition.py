"""Background board selection must not change the interactive discovery environment."""
from concurrent.futures import ThreadPoolExecutor
import os
import threading

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_db as kb


def test_discovery_stays_equal_while_decomposer_selects_board(tmp_path, monkeypatch):
    from agent.skill_commands import reload_skills
    from hermes_cli import kanban_decompose as decomp
    from tui_gateway import server

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    for name in ('HERMES_KANBAN_DB', 'HERMES_KANBAN_BOARD', 'HERMES_KANBAN_TASK'):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / 'config.yaml').write_text('{}')
    for name, extra in (('ordinary-probe', ''), ('review-probe', 'environments: [kanban]\n')):
        skill = tmp_path / 'skills' / name
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Probe\n{extra}---\nBody\n')
    from hermes_cli import kanban_db_connect as kbc

    kb.create_board('selected')
    with kbc.connect_closing(board='selected') as conn:
        task_id = kb.create_task(conn, title='Discovery probe', triage=True)
    dispatched = []

    def decompose(task_id, **kwargs):
        dispatched.append((task_id, kb.get_current_board()))
        return decomp.DecomposeOutcome(task_id, True, 'probe')

    monkeypatch.setattr(decomp, 'decompose_task', decompose)
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, '_board_slugs', lambda: ['selected'])
    entered, release = threading.Event(), threading.Event()
    seen = []
    original = decomp.list_triage_ids

    def held_list(**kwargs):
        seen.append(kb.get_current_board())
        entered.set()
        assert release.wait(20)
        return original(**kwargs)

    monkeypatch.setattr(decomp, 'list_triage_ids', held_list)

    def snapshot():
        reload_skills()
        return [server._methods[method](i, params)['result'] for i, (method, params) in enumerate([
            ('commands.catalog', {}), ('complete.slash', {'text': '/'}),
        ])]

    baseline = snapshot()
    assert '/ordinary-probe' in dict(baseline[0]['pairs'])
    assert '/review-probe' not in dict(baseline[0]['pairs'])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(dispatcher.auto_decompose_tick, 1, min_age_seconds=0)
        try:
            assert entered.wait(20)
            assert snapshot() == baseline
            assert 'HERMES_KANBAN_BOARD' not in os.environ
            assert kb.get_current_board() == 'default'
        finally:
            release.set()
        assert future.result(timeout=20) == 1
        assert executor.submit(kb.get_current_board).result(timeout=20) == 'default'
    assert dispatched == [(task_id, 'selected')]
    assert seen == ['selected']
    assert snapshot() == baseline
