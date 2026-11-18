"""Explicit foreign archives settle outcomes, without claiming delivery."""
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414
from tests.hermes_cli.test_kanban_origin_ledger import world as world, accept  # noqa: PLC0414
from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed, _quiet_owner


def test_quiet_archived_foreign_obligation_finishes_only_after_actual_work(world, board, monkeypatch):
    import hermes_constants
    import hermes_state_outcome as outcome
    from hermes_state import SessionDB
    from hermes_cli import kanban_session_activity as activity, kanban_origin_adapter as adapter, profiles

    path, replica, binding = world
    destination = path.parent / 'kanban' / 'origin-ledger.db'
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: path.parent)
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: replica)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    monkeypatch.setattr(activity.time, 'time', lambda: 1001)
    outcome._reset_hooks_for_tests()

    with kbc.connect_closing() as conn, SessionDB(db_path=path.parent / 'outcome.db') as db:
        db.create_session('s_current', source='desktop')
        db.append_message('s_current', role='user', content='Finish the remaining work.')
        db.append_message('s_current', role='assistant', content='The remaining work is finished and checked.')
        db.create_session('actual-work', source='subagent', parent_session_id='s_current')
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        destination.parent.mkdir(exist_ok=True)
        path.rename(destination)

        def derived():
            activity._reset_cache_for_tests()
            return outcome.derive_outcome('s_current', db=db, ignore_live_turn=True)

        assert derived()['kind'] == 'waiting'
        assert kb.archive_task(conn, tid)
        publish_observed(conn, replica, binding, version=4)
        assert derived()['kind'] == 'waiting'  # the real child still runs
        db.end_session('actual-work', end_reason='completed')
        assert derived()['kind'] == 'done'
        monkeypatch.setattr(activity.time, 'time', lambda: 1501)
        _quiet_owner(replica, 1500)
        assert derived()['kind'] == 'done'
        from hermes_cli import kanban_origin_ledger as ledger
        row = ledger.project(destination, replica=replica, gateway='origin', profile='default', now=1501)[0]
        assert row['closed_uncompleted']
        import sqlite3
        with sqlite3.connect(destination) as origin_db:
            assert origin_db.execute('SELECT released FROM origin_obligations').fetchone()[0] == 0
        assert not row['unfinished']
    outcome._reset_hooks_for_tests()
