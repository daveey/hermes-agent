"""A stored pin the routing registry refuses must not strand a card when no supervisor exists. [daveey]

MacBook card t_01a181752fc6cae0 was pinned to the retired ``claude-sonnet-4-6`` before the
create-time registry check existed. Under ``model_routing.enforce_subscription_only`` the dispatch
guard returned ``model_pin_not_in_routing_registry`` and tried an assignment block, which raised
``AssignmentRoutingError: no_capable_supervisor`` (no ``kanban.orchestrator_profile``). The card
stayed ``ready``, the hold is cap-exempt, so it sat there for four hours until a manual
``set-model none``. A stored pin is history, not a request: like ``stale_managed_pin`` on resume,
the dispatcher drops what the policy refuses and runs the card on the profile default.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml


@pytest.fixture
def board(tmp_path, monkeypatch):
    """Enforced assignee home and NO ``kanban.orchestrator_profile`` anywhere."""
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
    # create/set-model refuse these pins, exactly as the stranded card's pin predates that check.
    conn.execute('UPDATE tasks SET model_override=?, provider_override=? WHERE id=?', (model, provider, tid))
    conn.commit()
    return tid


def _events(conn, tid, kind):
    return [kb.json.loads(r[0]) if r[0] else None for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


def test_refused_pin_with_no_supervisor_is_cleared_and_dispatched_same_tick(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn = board
    tid = _seed(conn, 'claude-sonnet-4-6', 'anthropic')
    spawned = []
    dispatch_once(conn, spawn_fn=lambda task, ws, **kw: spawned.append(task) or 4242, max_spawn=1)

    task = kb.get_task(conn, tid)
    assert (task.model_override, task.provider_override) == (None, None)
    assert task.block_kind is None
    cleared = _events(conn, tid, 'model_override_cleared')
    assert len(cleared) == 1
    assert cleared[0]['model'] == 'claude-sonnet-4-6'
    assert cleared[0]['provider'] == 'anthropic'
    assert cleared[0]['reason'] == 'model_pin_not_in_routing_registry'
    assert 'claude-sonnet-5-5' in cleared[0]['allowed']
    bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert any('claude-sonnet-4-6' in b and 'routing registry' in b and 'profile default' in b for b in bodies)
    assert [t.id for t in spawned] == [tid]
    assert spawned[0].model_override is None and spawned[0].provider_override is None
    assert not _events(conn, tid, 'respawn_guarded')


def test_family_mismatch_with_no_supervisor_keeps_existing_path(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn = board
    tid = _seed(conn, 'gpt-6-astra', 'anthropic')
    spawned = []
    result = dispatch_once(conn, spawn_fn=lambda task, ws, **kw: spawned.append(task), max_spawn=1)
    assert spawned == []
    task = kb.get_task(conn, tid)
    assert task.status == 'ready'
    assert (task.model_override, task.provider_override) == ('gpt-6-astra', 'anthropic')
    assert not _events(conn, tid, 'model_override_cleared')
    assert [code for t, code in result.respawn_guarded if t == tid] in (
        ['model_provider_mismatch'], ['model_pin_not_in_routing_registry'])


def test_family_mismatch_pin_that_registry_also_refuses_is_not_cleared(board):
    """``gpt-6-astra`` on anthropic is refused by both checks; the registry one runs first. It
    must still keep the family-mismatch path (never silently drop a pin whose family is wrong)."""
    from hermes_cli.kanban_model_assignment import route_model_mismatch
    conn = board
    tid = _seed(conn, 'gpt-6-astra', 'anthropic')
    route_model_mismatch(conn, kb.get_task(conn, tid))
    assert kb.get_task(conn, tid).model_override == 'gpt-6-astra'


def test_dry_run_tick_changes_nothing(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn = board
    tid = _seed(conn, 'claude-sonnet-4-6', 'anthropic')
    before = list(conn.iterdump())
    result = dispatch_once(conn, dry_run=True)
    assert (tid, 'model_pin_not_in_routing_registry') in result.respawn_guarded
    assert list(conn.iterdump()) == before


def test_registered_pin_is_untouched(board):
    from hermes_cli.kanban_db_dispatch import dispatch_once
    conn = board
    tid = _seed(conn, 'claude-sonnet-5-5', 'anthropic')
    spawned = []
    dispatch_once(conn, spawn_fn=lambda task, ws, **kw: spawned.append(task) or 4242, max_spawn=1)
    assert spawned[0].model_override == 'claude-sonnet-5-5'
    assert not _events(conn, tid, 'model_override_cleared')
