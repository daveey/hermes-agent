"""Test processes must never write the live ``<root>/origin-work.db``.

hermes-hub, 2026-11-14 (t_01a1dadf165f8c53): the live origin-work.db had grown to 23 MB. Two thirds of its
``scoped_snapshot`` rows were keyed by pytest tmp homes (``.../pytest-of-ubuntu/pytest-4237/test_session_cron_other_runtim0``),
each holding a ~1 MB projection of the hub replica. A background snapshot save resolved ``kanban_home()`` twice: once
for the file path, once inside ``_origin_scope()``. A suite whose per-test fixture swaps ``HERMES_KANBAN_HOME`` between
those reads saved the next test's scope into the session root's file (here the live one). No guard refused a test
process writing the account's live file either. [daveey]
"""
import json
import sqlite3

import pytest

import hermes_state_guard
from hermes_cli import kanban_db, profiles
from hermes_cli import kanban_session_activity as activity


def card(n=0):
    return {'session': f's{n}', 'id': f't{n}', 'key': f'owner/work/default/t{n}',
            'status': 'running', 'gateway': 'owner', 'profile': 'work'}


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    activity._reset_cache_for_tests()
    yield
    activity._reset_cache_for_tests()


def rows(root):
    path = root / 'origin-work.db'
    if not path.exists():
        return []
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        return [json.loads(s) for (s,) in db.execute('SELECT scope FROM scoped_snapshot')]


def test_save_never_files_one_roots_scope_in_another_roots_file(tmp_path, monkeypatch):
    """The live symptom: a row whose scope names kanban home B inside A's origin-work.db."""
    session_root, test_root = tmp_path / 'session', tmp_path / 'test'
    session_root.mkdir()
    test_root.mkdir()
    # A per-test env fixture runs between the path read and the scope read of a background save.
    homes = iter([session_root])
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: next(homes, test_root))
    try:
        activity._remote_snapshot([card()])
    except RuntimeError:
        pass  # refusing the save is fine; the caller retries on the next change
    for root in (session_root, test_root):
        assert [scope[2] for scope in rows(root)] in ([], [str(root)]), f'foreign scope filed under {root}'


def test_test_process_cannot_write_the_live_origin_work_db(tmp_path, monkeypatch):
    live = tmp_path / 'live-root'
    live.mkdir()
    monkeypatch.setattr(hermes_state_guard, '_account_hermes_root', lambda: live.resolve())
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: live)
    with pytest.raises(RuntimeError, match='live-system guard'):
        activity._remote_snapshot([card()])
    assert not (live / 'origin-work.db').exists()


def test_production_process_still_writes_its_own_root(tmp_path, monkeypatch):
    live = tmp_path / 'live-root'
    live.mkdir()
    monkeypatch.setattr(hermes_state_guard, '_account_hermes_root', lambda: live.resolve())
    monkeypatch.setattr(hermes_state_guard, '_in_test_context', lambda: False)
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: live)
    assert activity._remote_snapshot([card()]) == [card()]
    assert activity._remote_snapshot() == [card()]
    assert [scope[2] for scope in rows(live)] == [str(live)]


def test_test_process_may_write_a_sandboxed_root(tmp_path, monkeypatch):
    live, sandbox = tmp_path / 'live-root', tmp_path / 'sandbox'
    live.mkdir()
    sandbox.mkdir()
    monkeypatch.setattr(hermes_state_guard, '_account_hermes_root', lambda: live.resolve())
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: sandbox)
    assert activity._remote_snapshot([card()]) == [card()]
    assert not (live / 'origin-work.db').exists()
