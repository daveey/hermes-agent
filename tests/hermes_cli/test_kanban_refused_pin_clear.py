"""Legacy restrictions cannot strand work or rewrite its authenticated history. [daveey]

The old policy cleared retired pins but held vendor mismatches. Model-neutral admission
instead projects every legacy concrete selector to an eligible class, including registered
selectors. No supervisor, pin-clear comment, or historical selector rewrite is needed.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Real isolated assignee registry, with no orchestrator profile."""
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('model:\n  default: claude-opus-5-5\n  provider: anthropic\n')
    profile = home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text(
        'model:\n  default: claude-sonnet-5-5\n  provider: anthropic\n'
        'platform_toolsets:\n  cli: [terminal, file, kanban]\nterminal:\n  backend: local\n'
        + _managed_yaml(True))
    kb.init_db()
    from hermes_cli.kanban_db_connect import connect_closing
    with connect_closing() as conn:
        yield conn


def _seed(conn, model, provider):
    tid = kb.create_task(conn, title='Legacy pin', assignee='worker')
    # Historical data only: public ingress refuses these selectors.
    conn.execute('UPDATE tasks SET model_override=?, provider_override=? WHERE id=?', (model, provider, tid))
    conn.commit()
    return tid


def _events(conn, tid, kind):
    return [kb.json.loads(r[0]) if r[0] else None for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


@pytest.mark.parametrize('model,provider', [
    ('claude-sonnet-4-6', 'anthropic'),
    ('gpt-6-astra', 'anthropic'),
    ('claude-sonnet-5-5', 'anthropic'),
])
def test_legacy_selector_is_projected_and_dispatched_without_supervisor_or_history_rewrite(board, model, provider):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    from hermes_cli.kanban_model_assignment import _assignee_config
    from agent.model_routing import eligible_routes

    conn = board
    tid = _seed(conn, model, provider)
    original_events = [(e.id, e.kind, e.payload) for e in kb.list_events(conn, tid)]
    original_comments = kb.list_comments(conn, tid)
    config = _assignee_config('worker')
    assert config['model_routing']['enabled']
    candidates = eligible_routes(config['model_routing'], 'standard')
    assert {(r['provider'], r['model']) for r in candidates} == {
        ('anthropic', 'claude-sonnet-5-5'), ('openai-codex', 'gpt-6.1-sol')}
    spawned = []
    result = dispatch_once(conn, spawn_fn=lambda task, ws, **kw: spawned.append(task) or 4242, max_spawn=1)

    task = kb.get_task(conn, tid)
    assert task.status == 'running' and task.block_kind is None
    assert (task.model_override, task.provider_override) == (None, None)
    assert [t.id for t in spawned] == [tid]
    assert (spawned[0].model_override, spawned[0].provider_override) == ('class:standard', 'auto')
    assert not result.respawn_guarded
    cleared = _events(conn, tid, 'model_override_cleared')
    assert len(cleared) == 1
    assert cleared[0]['reason'] == 'model_policy_no_pins'
    assert cleared[0]['previous']['model_override'] == model
    assert cleared[0]['previous']['provider_override'] == provider
    assert [(e.id, e.kind, e.payload) for e in kb.list_events(conn, tid)][:len(original_events)] == original_events
    comments = kb.list_comments(conn, tid)
    assert comments[:len(original_comments)] == original_comments
    assert len(comments) == len(original_comments) + 1
    assert comments[-1].author == 'kanban-dispatcher'
    runs = conn.execute('SELECT id, worker_pid FROM task_runs WHERE task_id=?', (tid,)).fetchall()
    assert [(r['id'], r['worker_pid']) for r in runs] == [(task.current_run_id, 4242)]


def test_family_mismatch_pin_that_registry_also_refuses_is_not_cleared(board):
    """The legacy diagnostic helper itself still cannot erase mismatched source evidence."""
    from hermes_cli.kanban_model_assignment import route_model_mismatch
    conn = board
    tid = _seed(conn, 'gpt-6-astra', 'anthropic')
    route_model_mismatch(conn, kb.get_task(conn, tid))
    assert kb.get_task(conn, tid).model_override == 'gpt-6-astra'


def test_dry_run_projects_legacy_selector_without_any_write_or_launch(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn = board
    tid = _seed(conn, 'claude-sonnet-4-6', 'anthropic')
    before = list(conn.iterdump())
    spawned = []
    result = dispatch_once(conn, dry_run=True, spawn_fn=lambda *a, **kw: spawned.append(a), max_spawn=1)
    assert [item[0] for item in result.spawned] == [tid]
    assert not result.respawn_guarded
    assert spawned == []
    assert list(conn.iterdump()) == before
