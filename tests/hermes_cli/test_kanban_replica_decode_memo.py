"""Replica rows are decoded (JSON parse, legacy digest, owner-evidence hash) once per distinct row text. [daveey]

The remote-links projection rebuilds whenever kanban.db or hub.db changes (every few seconds on a busy host),
and every rebuild re-parsed and re-hashed the whole replica: ~0.3 s of each sidebar refresh on a 6k-row hub.db.
A row's decode is a pure function of its stored text, so only new or edited rows may pay for it again.
"""
import pytest

from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_remote_terminal_release import NOW, card, counted, observed, replica, update


@pytest.fixture(autouse=True)
def _origin_profile(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')


@pytest.fixture()
def decodes(monkeypatch):
    """Count the expensive per-row work: owner-evidence hashes and legacy record digests."""
    from hermes_cli import kanban_origin_ledger, kanban_origin_legacy

    calls = []
    real_evidence, real_digest = kanban_origin_ledger._owner_evidence, kanban_origin_legacy.record_digest
    monkeypatch.setattr(kanban_origin_ledger, '_owner_evidence', lambda t: calls.append(t['id']) or real_evidence(t))
    monkeypatch.setattr(kanban_origin_legacy, 'record_digest', lambda t: calls.append(t['id']) or real_digest(t))
    return calls


def _records():
    return [('default/default/t_1', observed(card('done', tid='t_1'))),
            ('default/default/t_2', observed(card('running', tid='t_2'))),
            ('default/t_3', card('running', tid='t_3'))]


def test_unchanged_replica_is_not_decoded_again(tmp_path, decodes):
    path = replica(tmp_path, _records())
    first = activity._read_remote_links(path, now=NOW)
    assert decodes, 'the guard is no longer observing the per-row decode'

    decodes.clear()
    assert activity._read_remote_links(path, now=NOW) == first
    assert decodes == []


def test_edited_row_is_decoded_again_and_changes_the_answer(tmp_path, decodes):
    path = replica(tmp_path, _records())
    assert {r['id'] for r in counted(activity._read_remote_links(path, now=NOW))} == {'t_2', 't_3'}

    decodes.clear()
    update(path, 'default/default/t_2', observed(card('done', tid='t_2')))
    assert {r['id'] for r in counted(activity._read_remote_links(path, now=NOW))} == {'t_3'}
    assert decodes == ['t_2']


def test_tampered_row_never_reuses_its_old_evidence(tmp_path):
    path = replica(tmp_path, _records())
    assert {r['id'] for r in counted(activity._read_remote_links(path, now=NOW))} == {'t_2', 't_3'}

    # Same evidence block, different payload: the hash no longer binds, so t_1 is not released.
    update(path, 'default/default/t_1', {**observed(card('done', tid='t_1')), 'title': 'edited in transit'})
    assert {r['id'] for r in counted(activity._read_remote_links(path, now=NOW))} == {'t_1', 't_2', 't_3'}
