"""A recovered completion delivered by a Desktop consumer is not offered again at the next start.

Live (David-MBP, deleg_e757744d, admission ca1d8f79): the start-up reconcile reopened a failed
admission, the gateway's recovery note met the chat's turn lease, and the Desktop poller delivered the
row instead. Its transcript row stamps ``display_metadata.delegation_id`` (no admission id) and its
content lacks the gateway's INTERNAL NOTIFICATION footer, so ``_noted`` never matched it and every
later start reopened the same result until the attempt budget or the replay cap.

- A delegation with an ``async_delegation_complete`` row stamped with its id counts as delivered (per
  delegation id, so a coalesced admission reopens only its undelivered ids).
- A reconcile reopen records the admission it was made for; a (delegation, admission) pair is reopened
  once, which also covers a batch consumer whose row stamps no delegation id.
"""
import queue

import pytest

from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER
from tests.gateway.test_completion_admission import pending
from tests.gateway.test_completion_admission_reconcile import (
    _admit, _fail_next_note, _ledger, _offered, _target)
from tests.gateway.test_session_end_delegations import _admissions, _local
from tools import async_delegation as delegation


@pytest.fixture(autouse=True)
def _no_delegations():
    delegation._reset_for_tests()
    yield
    delegation._reset_for_tests()


def _desktop_row(authority, target, delegation_id, content):
    """What the "tui-poller" consumer writes: its own content, the delegation id, no admission id."""
    authority.db.append_message(
        target, 'user', content=content, display_kind='async_delegation_complete',
        display_metadata={'display_text': f'{delegation_id} finished', 'delegation_id': delegation_id,
                          'task_count': 1, 'completed_count': 1, 'failed_count': 0})


def _batch_row(authority, target):
    """What the "tui-completion-batch" consumer writes: process_complete with display_text only."""
    authority.db.append_message(target, 'user', content='Background work finished (batch).',
                                display_kind='process_complete',
                                display_metadata={'display_text': 'Background work finished'})


def _deliver_by_batch(events):
    for evt in events:
        claim = delegation.claim_event_delivery(evt, 'tui-completion-batch')
        assert claim
        delegation.complete_event_delivery(evt, claim)


@pytest.mark.asyncio
async def test_desktop_delivered_failed_admission_is_not_reopened(tmp_path, monkeypatch):
    """(a) Live shape: payload text ends with the INTERNAL NOTIFICATION footer, the ledger says
    delivered, and the Desktop row carries the text without it plus the delegation id."""
    from gateway.completion_admission_recovery import (
        reconcile_failed_completion_admissions, unnoted_failed_completions)
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        target = _target(runner, authority, ref)
        await _admit(runner, authority, ref, 'deleg_desktop')
        await _fail_next_note(authority, ref, monkeypatch)
        [failed] = _admissions(authority, ref)
        assert failed['outcome'] == 'failed'
        text = failed['payload']['text']
        assert text.rstrip().endswith(INTERNAL_NOTIFICATION_FOOTER)
        _desktop_row(authority, target, 'deleg_desktop', text.replace(INTERNAL_NOTIFICATION_FOOTER, '').rstrip())
        assert _ledger('deleg_desktop')[0] == 'delivered'

        assert unnoted_failed_completions(authority) == []
        offers = queue.Queue()
        assert reconcile_failed_completion_admissions(authority, offers) == 0
        assert _offered(offers) == []
        assert _ledger('deleg_desktop')[0] == 'delivered'
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_reopened_admission_delivered_by_batch_is_reopened_once(tmp_path, monkeypatch):
    """(b) No transcript row: one reopen. A batch consumer then delivers it (no delegation id, no
    admission id, different text); the next start reopens nothing."""
    from gateway.completion_admission_recovery import reconcile_failed_completion_admissions
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        target = _target(runner, authority, ref)
        await _admit(runner, authority, ref, 'deleg_batch')
        await _fail_next_note(authority, ref, monkeypatch)
        offers = queue.Queue()
        assert reconcile_failed_completion_admissions(authority, offers) == 1
        events = _offered(offers)
        assert [e['delegation_id'] for e in events] == ['deleg_batch']
        assert _ledger('deleg_batch')[0] == 'pending'

        _deliver_by_batch(events)
        _batch_row(authority, target)
        assert _ledger('deleg_batch')[0] == 'delivered'

        assert reconcile_failed_completion_admissions(authority, offers) == 0
        assert _offered(offers) == []
        assert _ledger('deleg_batch')[0] == 'delivered'
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_coalesced_admission_reopens_only_undelivered_delegation(tmp_path, monkeypatch):
    """(c) One failed admission carrying two delegations; only one has its Desktop row."""
    from gateway.completion_admission_recovery import reconcile_failed_completion_admissions
    runner, authority, ref, _actor = await _local(tmp_path, monkeypatch)
    try:
        target = _target(runner, authority, ref)
        route = authority.sessions[ref.session_id].route
        evts = [dict(pending(route, name), parent_session_id=target) for name in ('deleg_one', 'deleg_two')]
        assert await runner._deliver_async_delegation_group(evts) is True
        await _fail_next_note(authority, ref, monkeypatch)
        [failed] = _admissions(authority, ref)
        assert failed['outcome'] == 'failed'
        assert {_ledger(n)[0] for n in ('deleg_one', 'deleg_two')} == {'delivered'}
        _desktop_row(authority, target, 'deleg_one', 'deleg_one finished')

        offers = queue.Queue()
        assert reconcile_failed_completion_admissions(authority, offers) == 1
        assert [e['delegation_id'] for e in _offered(offers)] == ['deleg_two']
        assert _ledger('deleg_one')[0] == 'delivered' and _ledger('deleg_two')[0] == 'pending'
    finally:
        authority.db.close()
