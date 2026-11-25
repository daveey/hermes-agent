"""Resolved private history may be observed; history still in force stays an explicit hold."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_actionability_private import PrivateHold, held
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli import kanban_actionability_observation as source
from tests.hermes_cli.test_kanban_actionability_observation import Hub


def ev(uid, ord_, kind, **payload):
    return {'uid': uid, 'task_id': 't_root', 'kind': kind, 'version': 1, 'ord': ord_,
            'created_at': 100 + ord_, 'deleted_at': None, 'payload': json.dumps(payload)}


STOP = ev('stop', 2, 'spend_budget_triaged')
SCOPE = ev('scope', 2, 'spend_budget_triaged', cause='scope_stopped')
RENEW = ev('renew', 3, 'scope_renewed', episode='scope', covered_restriction_uids=['scope'])


@pytest.mark.parametrize('status,events', [
    ('ready', []),
    ('ready', [STOP, ev('go', 3, 'promoted_manual')]),
    ('todo', [STOP, ev('gone', 3, 'archived')]),
])
def test_absent_or_resolved_ordinary_history_is_not_held(status, events):
    assert held(status, events) is None


def test_verified_scope_cover_resolves_only_after_renewal_and_decision():
    events = [SCOPE, RENEW, ev('go', 4, 'promoted_manual')]
    assert held('ready', events) == 'mandate_unverifiable'
    assert held('ready', events, verified_cover=frozenset({'scope'})) is None


@pytest.mark.parametrize('status,events,reason', [
    ('ready', [STOP], 'stop_undecided'),
    ('ready', [ev('go', 1, 'promoted_manual'), STOP], 'stop_undecided'),
    ('triage', [STOP, ev('go', 3, 'promoted_manual')], 'card_in_triage'),
    ('ready', [STOP, ev('go', 3, 'promoted_manual'), ev('again', 4, 'spend_budget_triaged')], 'stop_undecided'),
    ('ready', [ev('safe', 2, 'spend_budget_triaged', cause='safety_refusal'),
               ev('go', 3, 'promoted_manual')], 'safety_restriction'),
    ('ready', [ev('safe', 2, 'decision_hold_restricted', superseded_by_safety_stop=True),
               ev('go', 3, 'promoted_manual')], 'safety_restriction'),
    ('ready', [SCOPE, ev('go', 3, 'promoted_manual')], 'scope_stop_not_renewed'),
    ('ready', [SCOPE, RENEW, ev('go', 4, 'promoted_manual')], 'mandate_unverifiable'),
    ('ready', [SCOPE, ev('renew', 3, 'scope_renewed', covered_restriction_uids=['other']),
               ev('go', 4, 'promoted_manual')], 'scope_stop_not_renewed'),
    ('ready', [SCOPE, ev('renew', 3, 'scope_renewed'), ev('go', 4, 'promoted_manual')], 'renewal_unverifiable'),
    ('ready', [ev('hold', 2, 'decision_hold_restricted'), ev('go', 3, 'promoted_manual')],
     'restriction_unattributable'),
    ('ready', [ev('odd', 2, 'spend_budget_triaged', cause='capability_unavailable'),
               ev('go', 3, 'promoted_manual')], 'restriction_unattributable'),
    ('ready', [ev('odd', 2, 'spend_budget_triaged', cause='decision_pending'),
               ev('go', 3, 'promoted_manual')], 'restriction_unattributable'),
    ('ready', [SCOPE, ev('renew', 3, 'scope_renewed', covered_restriction_uids=[]),
               ev('go', 4, 'promoted_manual')], 'renewal_unverifiable'),
    ('ready', [ev('renew', 3, 'scope_renewed', covered_restriction_uids=['elsewhere'])], 'mandate_unverifiable'),
    ('ready', [STOP, ev('go', 3, 'promoted_manual'),
               ev('renew', 4, 'scope_renewed', covered_restriction_uids=['elsewhere'])], 'mandate_unverifiable'),
    ('ready', [dict(STOP, payload='{'), ev('go', 3, 'promoted_manual')], 'private_payload_unverifiable'),
    ('ready', [dict(STOP, payload=None), ev('go', 3, 'promoted_manual')], 'private_payload_unverifiable'),
])
def test_history_in_force_names_its_hold(status, events, reason):
    assert held(status, events) == reason


def test_observation_records_resolved_and_refuses_held_card_and_parent():
    hub = Hub()
    hub.data['t_root']['events'] += [STOP, ev('go', 3, 'promoted_manual')]
    assert source.observe(hub, 'default', 't_root')['facts']['status'] == 'ready'
    hub.data['t_root']['events'].append(ev('again', 4, 'spend_budget_triaged'))
    with pytest.raises(PrivateHold) as caught:
        source.observe(hub, 'default', 't_root')
    assert caught.value.reason == 'stop_undecided' and isinstance(caught.value, ValueError)
    hub = Hub()
    hub.pages[0]['rows'] = [{'uid': 'l1', 'child_id': 't_root', 'parent_id': 't_parent', 'deleted_at': None}]
    hub.pages[0]['next'] = 'l1'
    hub.data['t_parent']['events'].append(dict(STOP, task_id='t_parent'))
    with pytest.raises(PrivateHold) as caught:
        source.observe(hub, 'default', 't_root')
    assert caught.value.task_id == 't_parent'
    hub.data['t_parent']['events'].append(dict(ev('go', 3, 'promoted_manual'), task_id='t_parent'))
    assert source.observe(hub, 'default', 't_root')['facts']['dependencies'] == [{'id': 't_parent', 'status': 'done'}]


def test_census_names_current_hold_instead_of_missing_deadline(tmp_path):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='held budget stop', assignee='default')
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'spend_budget_triaged', {'note': 'ordinary stop'})
        row = describe(conn, tid, now=1_700_000_000)
        assert 'private_hold' in row['violations']
        assert 'escalation_deadline_missing' not in row['violations']
        assert 'Private hold: stop_undecided' in row['instruction']
        assert row['blocker']['prerequisite'] == 'spend_budget'


def test_private_hold_only_replaces_a_missing_deadline_never_the_next_step():
    from hermes_cli.kanban_actionability import project
    parked = {'task': {'id': 'p', 'status': 'scheduled', 'assignee': 'default'},
              'wake_at': 1000, 'awaiting': {'kind': 'clock'}, 'probe': None}
    plain = project(parked, now=1000)
    held_row = project({**parked, 'private_hold': 'restriction_unattributable'}, now=1000)
    assert held_row == plain and 'private_hold' not in held_row['violations']
    running = {'task': {'id': 'r', 'status': 'running', 'assignee': 'default', 'current_run_id': 1},
               'run_evidence_known': True, 'run': {'id': 1, 'status': 'running', 'ended_at': None}}
    plain = project(running, now=1000)
    named = project({**running, 'private_hold': 'parent t_p stop_undecided'}, now=1000)
    # A live executor needs no administrative deadline, but a named private hold still shows.
    assert plain['violations'] == [] and plain['escalation']['state'] == 'not_required'
    assert named['violations'] == ['private_hold'] and named['escalation']['state'] == 'unavailable'
    assert (named['action'], named['blocker'], named['release'], named['owner']) == (
        plain['action'], plain['blocker'], plain['release'], plain['owner'])
    assert named['instruction'].endswith('Private hold: parent t_p stop_undecided')


def test_census_names_parent_hold_and_safety_never_resolves(tmp_path):
    with connect(tmp_path / 'kanban.db') as conn:
        parent = kb.create_task(conn, title='stopped parent', assignee='default')
        kb._append_event(conn, parent, 'spend_budget_triaged', {'cause': 'safety_refusal'})
        kb._append_event(conn, parent, 'promoted_manual', {})
        child = kb.create_task(conn, title='child', assignee='default', parents=[parent])
        row = describe(conn, child, now=1_700_000_000)
        # Existing parent-stop precedence is untouched; a safety stop never reads as resolved or releasable.
        assert row['action'] != 'execute_current_scope' and parent in row['instruction']
        assert row['release']['authority'] in ('unavailable', 'existing_lifecycle_gates')
        assert row['release'].get('command') in (None, '')
        assert held('done', [dict(ev('safe', 2, 'spend_budget_triaged', cause='safety_refusal'), task_id=parent),
                             dict(ev('go', 3, 'promoted_manual'), task_id=parent)]) == 'safety_restriction'


def test_uidless_private_history_stays_unknown_not_resolved(tmp_path, monkeypatch):
    from hermes_cli import kanban_actionability_sources as sources
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='synced history', assignee='default')
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'spend_budget_triaged', {})
        monkeypatch.setattr(sources.Reader, 'uid', lambda self, event: None)
        assert sources._private_reason(conn, {'id': tid, 'status': 'ready'}, sources.Reader(conn)) is None


def test_verified_capability_release_requires_exact_episode_and_later_decision():
    stop = ev('capability', 2, 'spend_budget_triaged', cause='capability_unavailable')
    release = ev('released', 3, 'stop_released', version=1, episode='capability')
    go = ev('go', 4, 'promoted_manual')
    verified = frozenset({'released'})
    assert held('ready', [stop, release, go], verified_releases=verified) is None
    assert held('ready', [stop, release, go]) == 'restriction_unattributable'
    assert held('triage', [stop, release, go], verified_releases=verified) == 'card_in_triage'
    for bad in (dict(release, ord=1),
                ev('released', 3, 'stop_released', version=1, episode='other'),
                ev('released', 3, 'stop_released', version=True, episode='capability')):
        assert held('ready', [stop, bad, go], verified_releases=verified) == 'restriction_unattributable'
    assert held('ready', [stop, release, dict(go, ord=2)], verified_releases=verified) == 'stop_undecided'
    for cause in ('safety_refusal', 'unknown', 'decision_pending', 'scope_stopped'):
        other = ev('capability', 2, 'spend_budget_triaged', cause=cause)
        assert held('ready', [other, release, go], verified_releases=verified) is not None
    annotation = ev('annotation', 5, 'decision_hold_restricted', cause='capability_unavailable')
    assert held('ready', [stop, release, go, annotation], verified_releases=verified) == 'restriction_unattributable'
    # Only a spend stop is releasable this way: a restriction annotation named by a verified release stays held.
    restricted = ev('capability', 2, 'decision_hold_restricted', cause='capability_unavailable')
    assert held('ready', [restricted, release, go], verified_releases=verified) == 'restriction_unattributable'


def test_capability_release_then_scope_renewal_history_keeps_each_stop_on_its_own_proof():
    """Capability stop -> release -> scope stop -> renewal -> release -> capability stop -> release."""
    events = [
        ev('cap1', 2, 'spend_budget_triaged', cause='capability_unavailable'),
        ev('rel1', 3, 'stop_released', version=1, episode='cap1'), ev('go1', 4, 'promoted_manual'),
        ev('scope', 5, 'spend_budget_triaged', cause='scope_stopped'),
        ev('renew', 6, 'scope_renewed', episode='scope', covered_restriction_uids=['scope']),
        ev('rel2', 7, 'stop_released', version=1, episode='scope'), ev('go2', 8, 'promoted_manual'),
        ev('cap2', 9, 'spend_budget_triaged', cause='capability_unavailable')]
    releases, cover = frozenset({'rel1', 'rel2', 'rel3'}), frozenset({'scope'})
    assert held('ready', events[:3], verified_releases=releases) is None
    assert held('ready', events[:7], verified_cover=cover, verified_releases=releases) is None
    # A release never substitutes for the scope stop's own mandate digest.
    assert held('ready', events[:7], verified_releases=releases) == 'mandate_unverifiable'
    assert held('triage', events, verified_cover=cover, verified_releases=releases) == 'card_in_triage'
    # The first episode's release does not release a later capability stop.
    assert held('ready', events + [ev('go3', 10, 'promoted_manual')], verified_cover=cover,
                verified_releases=releases) == 'restriction_unattributable'
    done = events + [ev('rel3', 10, 'stop_released', version=1, episode='cap2'), ev('go3', 11, 'promoted_manual')]
    assert held('ready', done, verified_cover=cover, verified_releases=releases) is None
    assert held('ready', done, verified_cover=cover, verified_releases=releases - {'rel3'}) == 'restriction_unattributable'


@pytest.mark.parametrize('origin', ['home', 'foreign', None, ''])
def test_local_synced_capability_release_requires_home_envelope(tmp_path, origin):
    from hermes_cli import kanban_actionability_sources as sources
    with connect(tmp_path / 'kanban.db') as conn:
        from hermes_cli.kanban_db_sync import apply_sync_schema_steps
        apply_sync_schema_steps(conn)
        tid = kb.create_task(conn, title='Synced capability stop', assignee='default')
        kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': 'capability_unavailable'})
        conn.execute("UPDATE task_events SET uid='capability' WHERE task_id=? AND kind='spend_budget_triaged'", (tid,))
        stop = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'", (tid,)).fetchone()[0]
        kb._append_event(conn, tid, 'stop_released', {'version': 1, 'episode': stop})
        conn.execute("UPDATE task_events SET uid='released',origin_gateway=? WHERE task_id=? AND kind='stop_released'", (origin, tid))
        kb._append_event(conn, tid, 'promoted_manual', {})
        reader = sources.Reader(conn)
        reader.synced = True
        task = {'id': tid, 'status': 'ready', 'created_on': 'home'}
        reason = sources._private_reason(conn, task, reader)
        assert reason == (None if origin == 'home' else 'restriction_unattributable')


@pytest.mark.parametrize('origin', ['home', 'foreign', None, ''])
def test_board_capability_release_uses_attested_home_not_actor(origin):
    hub = Hub()
    hub.data['t_root']['events'] += [
        ev('capability', 2, 'spend_budget_triaged', cause='capability_unavailable'),
        dict(ev('released', 3, 'stop_released', version=1, episode='capability',
                authority_kind='human', actor_kind='human'), origin_gateway=origin),
        ev('go', 4, 'promoted_manual')]
    if origin == 'home':
        assert source.observe(hub, 'default', 't_root')['facts']['status'] == 'ready'
    else:
        with pytest.raises(PrivateHold, match='restriction_unattributable'):
            source.observe(hub, 'default', 't_root')


def test_lane_gated_triage_is_lane_capacity_not_an_owner_decision(tmp_path):
    now = 1_700_000_000
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='lane held', assignee='default')
        conn.execute("UPDATE tasks SET status='triage', block_kind='lane_gate' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'lane_gated', {'lane': 'hermes', 'reason': 'limit:cards_per_day'})
        row = describe(conn, tid, now=now)
        assert row['action'] == 'wait_for_lane_admission'
        assert row['owner'] == {'kind': 'assignee', 'refs': ['lane-planner']}
        assert row['blocker']['prerequisite'] == 'lane capacity'
        assert row['blocker']['trigger'] == 'lane admission'
        gate_at = conn.execute("SELECT created_at FROM task_events WHERE task_id=? AND kind='lane_gated'", (tid,)).fetchone()[0]
        assert row['escalation']['at'] == gate_at + 86400
        later = describe(conn, tid, now=now + 172800)
        assert later['escalation']['at'] == row['escalation']['at']
        assert 'escalation_deadline_missing' not in row['violations']
        assert row['release']['authority'] == 'unavailable'
        kb._append_event(conn, tid, 'lane_admitted', {'lane': 'hermes'})
        released = describe(conn, tid, now=now)
        assert released['action'] != 'wait_for_lane_admission'
