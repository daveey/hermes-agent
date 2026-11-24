"""HTTP 410 on strict endpoints is bounded failure, not feed-cursor authority."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli.test_kanban_replay_current_row import issued_parent
from tests.hermes_cli.test_kanban_replay_http_session import serving
from tests.hermes_cli.test_kanban_replay_sender import Receiver, live, reopen

__all__ = ['live']


def hub_at(url):
    return client.Hub(client.Identity(url, 'fixture-key', 'display'))


@pytest.mark.parametrize('status', [410, 503])
def test_reachable_inspect_failure_pauses_at_eight(live, status):
    kb.create_task(live, title='bounded inspect', assignee='worker')
    receiver = Receiver()
    with serving(live, receiver, response_status=lambda p: status if p.endswith('/inspect') else 200) as (url, requests, _):
        hub = hub_at(url)
        try:
            for expected in range(1, 9):
                with pytest.raises(client.HubError):
                    client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
                held = recovery.fault(live)
                assert held['attempts'] == expected
                assert bool(held['paused']) is (expected == 8)
                assert held['journal_id'] == journal.snapshot(live)['journal_id']
            before = len(requests)
            with pytest.raises(client.SyncUnavailable):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            assert len(requests) == before
            assert not receiver.sent and journal.pending_wire(live) is None
            restarted = reopen(live)
            try:
                assert recovery.fault(restarted) == held
            finally:
                restarted.close()
        finally:
            hub.close()


@pytest.mark.parametrize('inspect_status', [200, 410])
def test_ops_410_inspects_immediately_without_resend(live, inspect_status):
    kb.create_task(live, title='uncertain mutation', assignee='worker')
    receiver = Receiver()

    def status(path):
        if path.endswith('/ops'):
            return 410
        return inspect_status if path.endswith('/inspect') and receiver.sent else 200

    with serving(live, receiver, response_status=status) as (url, requests, _):
        hub = hub_at(url)
        try:
            with pytest.raises(client.HubError):
                client.push(live, hub, 'default', 'macbook', primary=False, max_batches=1)
            actions = [p.rsplit('/', 1)[-1] for _, p, _, _ in requests]
            assert actions[-2:] == ['ops', 'inspect']
            frozen = journal.pending_wire(live)
            state = journal.snapshot(live)
            held = recovery.fault(live)
            assert held['attempts'] == 1 and held['op_id'] == state['pending']['op_id']
            assert state['settled'] == '0' and len(receiver.sent) == 1
            restarted = reopen(live)
            try:
                assert journal.pending_wire(restarted) == frozen
                assert recovery.fault(restarted) == held
                # Repair the endpoint, not the original intent; inspect settles it.
                inspect_status = 200
                result = client.push(restarted, hub, 'default', 'macbook', primary=False, max_batches=1)
                assert result.sent == 0 and result.applied == 1
                assert len(receiver.sent) == 1 and journal.pending_wire(restarted) is None
            finally:
                restarted.close()
        finally:
            hub.close()


@pytest.mark.parametrize('primary,source', [(False, 'parent'), (True, 'parent'), (True, 'row')])
def test_mandatory_evidence_410_holds_exact_custody(live, monkeypatch, primary, source):
    if source == 'parent':
        tid, receiver = issued_parent(live)
    else:
        tid = kb.create_task(live, title='primary row', assignee='worker')
        receiver = Receiver()
        receiver.lose_reply = True
        with pytest.raises(OSError, match='reply lost'):
            client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
        receiver.lose_reply = False
        receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=91,
                                 row=dict(uid=tid, id=tid, title='primary row', status='todo', version=7))
    original = receiver._call
    receiver._call = lambda method, path, body=None: receiver.evidence if '/evidence' in path else original(method, path, body)
    clock = [100.0]
    monkeypatch.setattr(existence, 'time', lambda: clock[0])
    frozen = journal.pending_wire(live)
    before = journal.snapshot(live)
    with serving(live, receiver, response_status=lambda p: 410 if '/evidence' in p else 200) as (url, requests, _):
        hub = hub_at(url)
        try:
            for expected in range(1, 9):
                with pytest.raises(existence.CurrentRowHold):
                    client.push(live, hub, 'default', 'macbook', primary=primary, max_batches=1)
                held = existence.fault(live)
                assert held['attempts'] == expected and bool(held['paused']) is (expected == 8)
                assert held['op_id'] == before['pending']['op_id']
                assert journal.pending_wire(live) == frozen
                assert journal.snapshot(live)['settled'] == before['settled']
                clock[0] += 61
            count = len(requests)
            with pytest.raises(existence.CurrentRowHold):
                client.push(live, hub, 'default', 'macbook', primary=primary, max_batches=1)
            assert len(requests) == count and len(receiver.sent) == 1
            restarted = reopen(live)
            try:
                assert existence.fault(restarted) == held
                assert journal.pending_wire(restarted) == frozen
            finally:
                restarted.close()
        finally:
            hub.close()


@pytest.mark.parametrize('status', [410, 503])
def test_strict_send_failure_does_not_starve_pull(live, status):
    kb.create_task(live, title='pull despite failed send', assignee='worker')
    receiver = Receiver()
    original = receiver._call

    def reply(method, path, body=None):
        if '/changes' in path or '/snapshot' in path:
            return {'events': [], 'cursor': 0, 'more': False}
        return original(method, path, body)

    receiver._call = reply
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    with serving(live, receiver, response_status=lambda p: status if p.endswith('/inspect') else 200) as (url, requests, _):
        hub = hub_at(url)
        try:
            with pytest.raises(client.HubError):
                client.sync_board('default', path, hub, 'macbook')
            assert any('/changes' in p or '/snapshot' in p for _, p, _, _ in requests)
            assert recovery.fault(live) is not None
        finally:
            hub.close()


def test_paused_send_does_not_starve_pull(live):
    kb.create_task(live, title='healthy feed during sender pause', assignee='worker')
    receiver = Receiver()
    original = receiver._call

    def reply(method, path, body=None):
        if '/changes' in path or '/snapshot' in path:
            return {'events': [], 'cursor': 0, 'more': False}
        return original(method, path, body)

    receiver._call = reply
    broken = [True]
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    frozen = journal.pending_wire(live)
    with serving(live, receiver, response_status=lambda p: 410 if broken[0] and p.endswith('/inspect') else 200) as (url, requests, _):
        hub = hub_at(url)
        try:
            for _ in range(4):
                with pytest.raises(client.HubError):
                    client.sync_board('default', path, hub, 'macbook')
            assert any('/changes' in p for _, p, _, _ in requests)
            assert recovery.fault(live)['paused'] == 1
            before = len(requests)
            broken[0] = False
            with pytest.raises(client.SyncUnavailable):
                client.sync_board('default', path, hub, 'macbook')
            assert any('/changes' in p or '/snapshot' in p for _, p, _, _ in requests[before:]), 'sender pause starved the healthy feed'
            assert journal.pending_wire(live) == frozen
        finally:
            hub.close()


def test_current_row_hold_does_not_starve_pull(live):
    _tid, receiver = issued_parent(live)
    original = receiver._call

    def reply(method, path, body=None):
        if '/changes' in path or '/snapshot' in path:
            return {'events': [], 'cursor': 0, 'more': False}
        if '/evidence' in path:
            return receiver.evidence
        return original(method, path, body)

    receiver._call = reply
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    frozen = journal.pending_wire(live)
    with serving(live, receiver, response_status=lambda p: 410 if '/evidence' in p else 200) as (url, requests, _):
        hub = hub_at(url)
        try:
            with pytest.raises(existence.CurrentRowHold):
                client.sync_board('default', path, hub, 'macbook')
            assert any('/changes' in p or '/snapshot' in p for _, p, _, _ in requests)
            assert journal.pending_wire(live) == frozen
        finally:
            hub.close()


def test_malformed_strict_envelope_does_not_starve_pull(live):
    kb.create_task(live, title='malformed capability still pulls', assignee='worker')
    receiver = Receiver()
    original = receiver._call

    def reply(method, path, body=None):
        if '/changes' in path or '/snapshot' in path:
            return {'events': [], 'cursor': 0, 'more': False}
        if path.endswith('/capability'):
            return []
        return original(method, path, body)

    receiver._call = reply
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    frozen = journal.pending_wire(live)
    with serving(live, receiver) as (url, requests, _):
        hub = hub_at(url)
        try:
            with pytest.raises(client.ReplayFault):
                client.sync_board('default', path, hub, 'macbook')
            assert any('/changes' in p or '/snapshot' in p for _, p, _, _ in requests)
            assert journal.pending_wire(live) == frozen
        finally:
            hub.close()
