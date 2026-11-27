"""A park's reason is reported apart from when it is next checked. [daveey]

Real disposable boards through the entry points that read a park: ``kb.schedule_task`` (the writer), the
actionability ``report`` / ``describe`` / ``fields`` / ``render`` readers, and the wait-info readers the session
list and the panel use (``_local_wait_info``, ``describe_replica``)."""
import json

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wait_info as kwi
from hermes_cli.kanban_actionability import project
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_evidence import fields, render
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_session_wait_rows import _local_wait_info

T0 = 1_800_000_000
WAKE = 1_800_086_500  # 2027-01-16T08:01:40Z
UNTIL = 'SCHEDULED_UNTIL=2027-01-16T08:01:40Z'
WHY = 'Event watch on hub-hosted original owners; backstop at nightly mini rollout 10:30Z'
WATCHED = ['t_01a1fc2649b02cc6', 't_01a20286f9f3f262', 't_01a2018f71f6939a', 't_01a201a66c560c19']
# The shape of the second screenshot row: a clock park whose probe watches four named owner cards.
CHECK = ('test "$(ssh hermes-hub "sqlite kanban \\"select exists(select 1 from task_comments where id>258561 '
         "and task_id in ('" + "','".join(WATCHED) + "'))\\\"\")\" = 1")


def _row(conn, tid, now=T0 + 10):
    return next(r for r in report(conn, now=now)['rows'] if r['task_id'] == tid)


def _park(conn, monkeypatch, awaiting, *, parents=(), **kw):
    monkeypatch.setattr('time.time', lambda: T0)
    tid = kb.create_task(conn, title='parked', assignee='default', parents=list(parents))
    kw.setdefault('clock_only', 'wake_check' not in kw)
    assert kb.schedule_task(conn, tid, reason=f'{UNTIL} awaiting natural hub update then live measurements',
                            awaiting=awaiting, **kw)
    return tid


def _described(status, **kw):
    wait = kwi.describe(status, **kw)
    assert wait is not None
    return wait


def _intent(conn, tid):
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='scheduled' ORDER BY id DESC",
                           (tid,)).fetchone()[0]
    return json.loads(payload)['actionability_intent']


def test_clock_park_names_its_reason_and_keeps_the_timer_as_next_check(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY})
        row = _row(conn, tid)
        assert row['instruction'] == f'Waiting on: {WHY}. Next check: Recorded clock reaches {WAKE}'
        assert row['blocker'] == {**row['blocker'], 'prerequisite': WHY, 'trigger': f'Recorded clock reaches {WAKE}'}
        assert row['action'] == 'wait_for_named_producer' and row['violations'] == []
        assert describe(conn, tid, now=T0 + 10)['blocker']['prerequisite'] == WHY
        shown = fields(conn, kb.get_task(conn, tid))['actionability']
        assert f'Waiting on: {WHY}. Next check: Recorded clock reaches' in render(shown)
        wait = _local_wait_info(conn, [tid], now=T0 + 10)[tid]
        assert wait['prerequisite'] == WHY and wait['next_actor'] == 'the clock (timed check)'
        assert 'backstop_at' not in wait and 'watching' not in wait
        assert wait['waiting_on'].startswith('the clock until 2027-01-16 08:01 UTC (Event watch')


def test_event_watch_with_timed_backstop_names_the_watched_cards(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY}, wake_check=CHECK, check_every=900)
        row = _row(conn, tid)
        assert row['instruction'] == (f'Waiting on: {WHY}. Next check: Recorded condition probe succeeds: {CHECK}'
                                      f'; backstop: recorded clock reaches {WAKE}')
        assert row['blocker']['prerequisite'] == WHY
        assert row['blocker']['trigger'] == f'Recorded condition probe succeeds: {CHECK}'
        wait = _local_wait_info(conn, [tid], now=T0 + 10)[tid]
        assert wait['prerequisite'] == WHY
        assert wait['watching'] == WATCHED
        assert wait['next_actor'] == 'cards ' + ', '.join(WATCHED) + ' (event watch)'
        assert wait['backstop_at'] == WAKE
        assert wait['wait_kind'] == 'clock' and wait['expected_s'] == WAKE - T0  # the timer is carried unchanged


def test_scheduled_card_behind_parents_still_names_its_recorded_reason(tmp_path, monkeypatch):
    """The first screenshot row: a clock park on a card that also has open parents (action satisfy_dependency)."""
    with connect(tmp_path / 'kanban.db') as conn:
        monkeypatch.setattr('time.time', lambda: T0)
        parent = kb.create_task(conn, title='owner still measuring', assignee='default')
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY}, parents=[parent])
        row = _row(conn, tid)
        assert row['action'] == 'satisfy_dependency'
        assert row['instruction'].startswith(f'Waiting on: {WHY}. Next check: ')
        assert parent in row['instruction']
        assert row['blocker']['prerequisite'].startswith(f'{WHY}; parents: {parent} (')
        assert parent in row['blocker']['trigger'] and WHY not in row['blocker']['trigger']
        assert _intent(conn, tid)['action'] == 'satisfy_dependency'


def test_stored_intent_keeps_trigger_only_bytes_so_older_parks_still_validate(tmp_path, monkeypatch):
    """The frozen intent is compared byte-for-byte with its recomputation on every read: the reason wording is
    read-time only, so a park stamped before this change (trigger-only instruction) stays valid."""
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY})
        intent = _intent(conn, tid)
        trigger = f'Recorded clock reaches {WAKE}'
        assert intent['instruction'] == trigger
        assert intent['blocker']['prerequisite'] == trigger == intent['blocker']['trigger']
        row = _row(conn, tid)
        assert 'creation_intent_invalid' not in row['violations']
        assert row['escalation']['at'] == WAKE + 600


@pytest.mark.parametrize('frozen', [True, False])
def test_projection_frozen_form_omits_the_reason(frozen):
    facts = {'task': {'id': 'f', 'status': 'scheduled', 'assignee': 'default'}, 'wake_at': 1000,
             'awaiting': {'kind': 'clock', 'why': 'Natural hub update'}, 'probe': 'probe-ready'}
    row = project(facts, now=1000, frozen=frozen)
    trigger = 'Recorded condition probe succeeds: probe-ready'
    assert row['blocker']['trigger'] == trigger
    if frozen:
        assert row['instruction'] == trigger == row['blocker']['prerequisite']
    else:
        assert row['instruction'] == f'Waiting on: Natural hub update. Next check: {trigger}; backstop: recorded clock reaches 1000'
        assert row['blocker']['prerequisite'] == 'Natural hub update'


@pytest.mark.parametrize('awaiting,trigger,actor', [
    ({'kind': 'ci', 'ref': 'https://example.com/runs/17', 'why': 'exact-head checks'}, 'Recorded ci producer https://example.com/runs/17', 'ci https://example.com/runs/17'),
    ({'kind': 'deploy', 'ref': 'https://example.com/deploys/9', 'why': 'hub picks up the merge'}, 'Recorded deploy producer https://example.com/deploys/9',
     'deploy https://example.com/deploys/9'),
])
def test_ci_and_deploy_parks_keep_their_producer_and_add_the_reason(tmp_path, monkeypatch, awaiting, trigger, actor):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, awaiting, wake_check='false', clock_only=False)
        wait = _local_wait_info(conn, [tid], now=T0 + 10)[tid]
        assert wait['waiting_on'] == actor and wait['next_actor'] == actor  # the label and its precedence are unchanged
        assert wait['prerequisite'] == awaiting['why']
        assert 'backstop_at' not in wait
    row = project({'task': {'id': 'f', 'status': 'scheduled', 'assignee': 'default'}, 'wake_at': 1000,
                   'awaiting': awaiting}, now=1000)
    assert row['instruction'] == f"Waiting on: {awaiting['why']}. Next check: {trigger}"
    assert row['blocker'] == {**row['blocker'], 'prerequisite': awaiting['why'], 'trigger': trigger}


def test_card_park_names_the_card_as_next_actor(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        monkeypatch.setattr('time.time', lambda: T0)
        producer = kb.create_task(conn, title='producer', assignee='default')
        tid = _park(conn, monkeypatch, {'kind': 'card', 'ref': producer, 'why': 'its measurement lands'},
                    clock_only=False)
        wait = _local_wait_info(conn, [tid], now=T0 + 10)[tid]
        assert wait['waiting_on'] == f'card {producer}' == wait['next_actor']
        assert wait['prerequisite'] == 'its measurement lands'


@pytest.mark.parametrize('awaiting', [{'kind': 'clock'}, {'kind': 'clock', 'why': 'guess', 'inferred': True}])
def test_park_without_a_reason_reports_unavailable_and_invents_none(awaiting):
    """The writer refuses a new clock park with no why; a legacy or replicated one still reads without a guess."""
    trigger = 'Recorded clock reaches 1000'
    row = project({'task': {'id': 'f', 'status': 'scheduled', 'assignee': 'default'}, 'wake_at': 1000,
                   'awaiting': awaiting}, now=1000)
    assert row['instruction'] == f'Waiting on: waiting reason unavailable. Next check: {trigger}'
    assert row['blocker']['prerequisite'] == 'waiting reason unavailable'
    assert row['blocker']['trigger'] == trigger
    for park in ({'awaiting': {'kind': 'clock'}, 'wake_at': WAKE}, {'wake_at': WAKE}):
        wait = _described('scheduled', park=park, since=T0, now=T0 + 10)
        assert 'prerequisite' not in wait and wait['next_actor'] == 'the clock (timed check)'
        assert wait['waiting_on'] == 'the clock until 2027-01-16 08:01 UTC' and 'backstop_at' not in wait


@pytest.mark.parametrize('awaiting', [
    {'kind': 'clock'},
    {'kind': 'clock', 'why': 'guess', 'inferred': True},
])
@pytest.mark.parametrize('probe', [None, 'test -f result'])
def test_legacy_clock_report_and_show_separate_unavailable_reason(tmp_path, monkeypatch, awaiting, probe):
    """Seed historical metadata only in a disposable store; do not relax today's writer validation."""
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY},
                    **({'wake_check': probe} if probe else {}))
        event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='scheduled' ORDER BY id DESC",
                             (tid,)).fetchone()
        payload = json.loads(event['payload'])
        payload['awaiting'] = awaiting
        # Historical parks predate creation-intent stamping.
        payload.pop('actionability_intent', None)
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
        conn.commit()
        before = list(conn.iterdump())
        for row in (_row(conn, tid), describe(conn, tid, now=T0 + 10),
                    fields(conn, kb.get_task(conn, tid))['actionability']):
            assert row['blocker']['prerequisite'] == 'waiting reason unavailable'
            trigger = f'Recorded condition probe succeeds: {probe}' if probe else f'Recorded clock reaches {WAKE}'
            assert row['blocker']['trigger'] == trigger
            assert row['instruction'].startswith(f'Waiting on: waiting reason unavailable. Next check: {trigger}')
            assert row['action'] == 'wait_for_named_producer'
            assert row['owner'] == {'kind': 'assignee', 'refs': ['default']}
            assert row['violations'] == []
            assert row['escalation']['at'] == WAKE + 600
            assert 'waiting reason unavailable' in render(row)
        assert list(conn.iterdump()) == before


def test_writer_refuses_a_clock_park_with_no_reason(tmp_path, monkeypatch):
    """The creation side already requires the reason: nothing to extend there."""
    from hermes_cli.kanban_awaiting import AwaitingError
    with connect(tmp_path / 'kanban.db') as conn, pytest.raises(AwaitingError, match='needs why='):
        _park(conn, monkeypatch, {'kind': 'clock'})


def test_wake_into_todo_clears_the_old_reason(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY})
        parent = kb.create_task(conn, title='late prerequisite', assignee='default')
        kb.link_tasks(conn, parent, tid)
        assert WHY in _row(conn, tid)['instruction']
        assert kc.wake_due_continuations(conn, now=5_000_000_000) == [tid]
        assert kb.get_task(conn, tid).status == 'todo'
        row = _row(conn, tid, T0 + 500)
        assert WHY not in row['instruction'] and WHY not in str(row['blocker'])
        # The stale clock reason is gone; what remains is the real prerequisite, the late parent.
        wait = _local_wait_info(conn, [tid], now=T0 + 500)
        assert set(wait) == {tid} and wait[tid]['waiting_on'] == f'card {parent}', wait
        assert WHY not in str(wait) and 'prerequisite' not in wait[tid] and 'next_actor' not in wait[tid]
        kb.unlink_tasks(conn, parent, tid)
        assert _local_wait_info(conn, [tid], now=T0 + 600) == {}  # no parent left: no wait, no reason


def test_core_inferred_awaiting_is_never_reported_as_the_parkers_reason():
    park = {'awaiting': {'kind': 'clock', 'why': 'guessed from the park text', 'inferred': True}, 'wake_at': T0 + 600}
    wait = _described('scheduled', park=park, since=T0, now=T0 + 10)
    assert 'prerequisite' not in wait and wait['next_actor'] == 'the clock (timed check)'


@pytest.mark.parametrize('park', [
    {'awaiting': {'kind': 'human', 'why': 'approve the spend'}, 'wake_at': T0 + 600},  # a hidden ask
    {'awaiting': {'kind': 'ci', 'why': 'no ref named'}, 'wake_at': T0 + 600},          # no named producer
    {},                                                                               # nothing named
])
def test_agent_owned_parks_with_no_producer_get_no_actor_or_reason(park):
    wait = _described('scheduled', park=park, since=T0, now=T0 + 10)
    assert wait['agent_owned'] is True
    assert not {'prerequisite', 'next_actor', 'watching', 'backstop_at'} & set(wait)


def test_opaque_probe_with_clock_is_a_condition_check_with_backstop():
    park = {'awaiting': {'kind': 'clock', 'why': 'disk pressure clears'}, 'wake_at': T0 + 600,
            'wake_check': 'python3 /opt/probes/disk_free.py --min 20'}
    wait = _described('scheduled', park=park, since=T0, now=T0 + 10)
    assert wait['next_actor'] == 'probe disk_free.py (condition check)' and wait['backstop_at'] == T0 + 600
    assert 'watching' not in wait and wait['prerequisite'] == 'disk pressure clears'


def test_replica_card_carries_the_owners_published_reason_and_watch():
    card = {'id': 't_0123456789ab', 'status': 'scheduled', 'awaiting': {'kind': 'clock', 'why': WHY},
            'wake_check': CHECK, 'parked_at': T0, 'park': {'wake_at': WAKE, 'reason': 'x', 'outcome': 'scheduled'}}
    wait = kwi.describe_replica(card, now=T0 + 10) or {}
    assert wait['prerequisite'] == WHY and wait['watching'] == WATCHED and wait['backstop_at'] == WAKE
    unpublished = kwi.describe_replica({**card, 'awaiting': None, 'wake_check': None}, now=T0 + 10) or {}
    assert 'prerequisite' not in unpublished and 'watching' not in unpublished


@pytest.mark.parametrize('why', ['ci', 'x' * 900])
def test_actionability_keeps_short_reasons_and_bounds_long_ones(why):
    row = project({'task': {'id': 'f', 'status': 'scheduled', 'assignee': 'default'}, 'wake_at': WAKE,
                   'awaiting': {'kind': 'ci', 'ref': 'https://example.com/run/1', 'why': why}}, now=T0)
    assert row['blocker']['prerequisite'] == why[:kwi.PREREQUISITE_MAX]
    assert row['instruction'].startswith('Waiting on: ')


def test_actionability_does_not_promote_inferred_reason():
    row = project({'task': {'id': 'f', 'status': 'scheduled', 'assignee': 'default'}, 'wake_at': WAKE,
                   'awaiting': {'kind': 'chat', 'ref': 'owning orchestrator', 'why': 'inferred wording', 'inferred': True},
                   'probe': 'test -f result'}, now=T0)
    assert 'inferred wording' not in row['instruction']
    assert 'inferred wording' not in row['blocker']['prerequisite']


def test_reason_is_flattened_and_bounded():
    park = {'awaiting': {'kind': 'clock', 'why': 'a\n  b ' + 'x' * 900}, 'wake_at': T0 + 600}
    wait = _described('scheduled', park=park, since=T0, now=T0 + 10)
    assert wait['prerequisite'].startswith('a b x') and len(wait['prerequisite']) == kwi.PREREQUISITE_MAX
    assert kwi.watched_cards(' '.join(f't_{i:012x}' for i in range(20))) == [f't_{i:012x}' for i in range(8)]
    assert kwi.watched_cards(None) == [] and kwi.watched_cards('t_abcdef01 t_abcdef01') == ['t_abcdef01']
