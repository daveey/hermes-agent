"""Updater holds must not disappear from claim/stuck diagnostics."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_runs_on_dispatch as helpers

svc = helpers.svc
gws = helpers.gws


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_updater_hold_records_each_unclaimed_card_and_recovers(gws, monkeypatch, lane):
    from hermes_cli import admission_fence
    from hermes_constants import get_default_hermes_root
    from pathlib import Path

    hub, worker = gws
    hub.enter()
    from tests.hermes_cli.kanban_review_fixture import completed_gateway_source, create_review
    if lane == 'review':
        completed_gateway_source(hub)
    create = create_review if lane == 'review' else kb.create_task
    ids = [create(hub.conn, title=f'held {n}', assignee='default',
                          runs_on=['machine:worker-1']) for n in range(3)]
    hub.round()
    worker.round()
    worker.enter()
    root = Path(get_default_hermes_root()) / 'core-update'
    marker = root / 'holds' / 'hermes-sync'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"pid": 570900, "created_at": 1}')
    assert admission_fence.is_held(root)
    result = worker.tick()
    assert not result.spawned
    assert set(result.claim_deferred) == {(tid, 'update_admission_hold') for tid in ids}
    message = kbd.stuck_warning(300, [result])
    assert all(tid in message for tid in ids)
    assert 'update_admission_hold' in message
    assert 'Board lease refused' not in message
    marker.unlink()  # operator recovery, not automatic stale-hold reclamation
    result = worker.tick()
    assert {t for t, *_ in result.spawned} == set(ids)
    assert not result.claim_deferred


def test_warning_names_all_recorded_holds():
    result = kbd.DispatchResult(
        skipped_unassigned=['unassigned'], skipped_nonspawnable=['nonspawnable'],
        skipped_per_profile_capped=[('capped', 'default', 2)],
        owner_down=[('owner', 'default', 'service_failed')],
        capsule_refused=[('capsule', 'transport_absent')],
        respawn_guarded=[('guarded', 'recent_success')], rate_limited=['limited'],
        skipped_ineligible=[('ineligible', 'labels_mismatch')],
        claim_deferred=[('deferred', 'offline')],
    )
    message = kbd.stuck_warning(8, [result])
    for tid in ('unassigned', 'nonspawnable', 'capped', 'owner', 'capsule', 'guarded', 'limited',
                'ineligible', 'deferred'):
        assert f'{tid}=' in message
    for reason in ('service_failed', 'transport_absent', 'recent_success', 'labels_mismatch', 'offline'):
        assert reason in message
