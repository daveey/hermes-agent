"""Actual preclaim producers must not overstate task/fleet startup failures. [daveey]"""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_admission as admission
from hermes_cli import kanban_nonrunning_reason as reasons
from tests.hermes_cli import test_kanban_nonrunning_reason as helpers

kanban_home = helpers.kanban_home


def test_actual_nonprofile_lane_is_not_a_task_startup_failure(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='external execution lane', assignee='external-lane')
        result = dispatch.DispatchResult()
        assert admission.lane_admission_refused(
            conn, tid, 'external-lane', result, profile_exists=lambda _: False,
            spawn_fn=None, dry_run=False)
        assert result.skipped_nonspawnable == [tid]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == 'ready' and task.current_run_id is None
        reason = reasons.from_result(result, tid)
        assert reason is not None
        # The predicate also covers expected external lanes and per-home
        # allowlists, not just broken profiles. It proves no fleet-wide failure.
        assert reason['disposition'] == 'unknown'
        assert reason['scope'] == 'observing_dispatcher'
        assert 'startup' not in reason['summary'].lower()
        assert reason['actor'] != 'human'


def test_actual_allowlist_exclusion_does_not_mean_profile_is_broken(kanban_home, monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'profile_exists', lambda _: True)
    (kanban_home / 'config.yaml').write_text('kanban:\n  dispatch_profiles:\n    - other\n')
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='belongs to another home', assignee='default')
        result = dispatch.dispatch_once(conn, dry_run=True)
        assert result.skipped_nonspawnable == [tid]
        assert not result.spawned
        reason = reasons.from_result(result, tid)
        assert reason is not None
        assert reason['disposition'] == 'unknown'
        assert reason['scope'] == 'observing_dispatcher'
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == 'ready'
