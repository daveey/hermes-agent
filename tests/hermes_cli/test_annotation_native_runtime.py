"""Executable annotation composition with two real authorized profile stores."""
import json
import time

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_runtime import AnnotationRuntime
from hermes_cli.fact_annotation_startup import binding_for
from tests.hermes_cli import test_kanban_session_activity as helpers
from tests.hermes_cli.annotation_fact_processes import request_guard

kanban_root = helpers.kanban_root


def test_runtime_two_profile_readiness_canonical_entries_and_rollback(kanban_root, tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from hermes_cli import kanban_session_activity as kanban
    from hermes_cli import goal_session_activity as goal
    from hermes_cli import cron_session_activity as cron
    from hermes_cli import process_session_activity as background
    from hermes_cli import turn_session_activity as turn
    from hermes_cli import subagent_session_activity as child
    from hermes_cli import review_session_activity as review
    homes = {'default': tmp_path / 'default', 'named': tmp_path / 'profiles/named'}
    dbs = []
    for profile, home in homes.items():
        home.mkdir(parents=True)
        db = SessionDB(db_path=home / 'state.db')
        dbs.append(db)
        db.create_session('same-id', 'cli')
        if profile == 'named':
            db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES (?,?)',
                ('goal:same-id', json.dumps({'status': 'active', 'goal': 'scope proof'}))))
            (home / 'cron').mkdir()
            (home / 'cron/jobs.json').write_text(json.dumps([{'id': 'job', 'enabled': True,
                'owner_session_id': 'same-id', 'next_run_at': '2030-01-01T00:00:00Z'}]))
    monkeypatch.setattr(kanban, '_remote_cards', lambda *args: [])
    counts = {'stop': 0, 'start': 0}
    def stop():
        counts['stop'] += 1
    def start():
        assert all(binding._mode == 'legacy' for binding in runtime.bindings.values())
        counts['start'] += 1
    runtime = AnnotationRuntime(homes, fallback_profile='default', gateway='fixture',
                                stop_legacy=stop, start_legacy=start)
    try:
        assert binding_for() is None
        assert counts == {'stop': 0, 'start': 0}
        seeds = {name: [{'id': 'same-id', 'profile': name}] for name in homes}
        runtime.start(seeds, timeout=15)
        assert counts == {'stop': 1, 'start': 0}
        assert binding_for('named').scope.profile_id == 'named'
        rows = [{'id': 'same-id', 'profile': name} for name in homes]
        with request_guard():
            for entry in (goal.annotate_goal_activity, cron.annotate_cron_activity,
                          background.annotate_background_activity, turn.annotate_turn_activity,
                          child.annotate_subagent_activity, review.annotate_review_wait):
                entry(rows)
            kanban.annotate_kanban_activity(rows, display=True)
        assert not rows[0].get('goal_active')
        assert rows[1]['goal_active'] is True
        assert not rows[0].get('cron_active')
        assert rows[1]['cron_active'] is True
        runtime.close()
        assert binding_for() is None
        assert counts == {'stop': 1, 'start': 1}
    finally:
        if binding_for() is not None:
            runtime.close()
        for db in dbs:
            db.close()


def test_unready_runtime_never_drains_or_installs(kanban_root, tmp_path):
    calls = []
    runtime = AnnotationRuntime({'default': tmp_path / 'absent'}, fallback_profile='default', gateway='fixture',
        stop_legacy=lambda: calls.append('stop'), start_legacy=lambda: calls.append('start'))
    try:
        with pytest.raises(TimeoutError):
            runtime.start({'default': [{'id': 'consumer'}]}, timeout=.05)
        assert calls == []
        assert binding_for() is None
    finally:
        runtime.close()
