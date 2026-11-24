"""Recovery limits through production Hub HTTP; fixture Board, no deployment proof."""
import importlib
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replay_recovery as recovery
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_http_session as http

live = http.live


@contextmanager
def clocks(monkeypatch):
    clock = dict(mono=1000.0, wall=100000.0)
    with monkeypatch.context() as patch:
        patch.setattr(time, 'time', lambda: clock['wall'])
        patch.setattr(time, 'monotonic', lambda: clock['mono'])
        importlib.reload(recovery)
        try:
            yield clock
        finally:
            # Restore the real functions before restoring the module's bindings.
            patch.undo()
            importlib.reload(recovery)


class Failing(http.AckReceiver):
    def __init__(self, clock, *, duration=0, wall_jump=0):
        super().__init__()
        self.clock = clock
        self.duration = duration
        self.wall_jump = wall_jump
        self.fail = True

    def _call(self, method, path, body=None):
        if path.endswith('/ops') and self.fail:
            self.calls.append('ops')
            self.clock['mono'] += self.duration
            self.clock['wall'] += self.wall_jump
            # Simulate a reachable serving peer dropping the unaccepted reply.
            # The fixture returns normally so its HTTP layer closes deliberately.
            return {}
        return super()._call(method, path, body)


@pytest.mark.parametrize('legacy', [False, True])
def test_foreign_process_clock_rearms_bounded_delay_without_aging(live, monkeypatch, legacy):
    kb.create_task(live, title='foreign monotonic clock', assignee='worker')
    with clocks(monkeypatch) as clock:
        peer = Failing(clock)
        with http.serving(live, peer, lose_ops_reply=True) as (url, requests, _):
            hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
            try:
                with pytest.raises(OSError):
                    push(live, hub)
                held = recovery.fault(live)
                frozen = journal.pending_wire(live)
                if legacy:
                    with kbc.write_txn(live):
                        live.execute('ALTER TABLE kanban_replay_transport_fault1 DROP COLUMN retry_clock')
                        live.execute('UPDATE kanban_replay_transport_fault1 SET retry_after=10000000')
                # A fresh interpreter/boot has no authority to compare the old
                # monotonic timestamp. Simulate that boundary, not offline age.
                importlib.reload(recovery)
                clock['mono'] = 10.0
                path = Path(live.execute('PRAGMA database_list').fetchone()[2])
                with kbc.connect(path) as reopened:
                    before = list(requests)
                    with pytest.raises(client.SyncUnavailable, match='backoff'):
                        push(reopened, hub)
                    current = recovery.fault(reopened)
                    assert current['retry_after'] == 12.0, 'foreign clock deadline was not reanchored'
                    assert current['attempts'] == held['attempts'] == 1
                    assert current['reachable_s'] == held['reachable_s'] == 0
                    assert journal.pending_wire(reopened) == frozen
                    assert not any(p.endswith('/ops') for _, p, _, _ in requests[len(before):])
                    clock['mono'] = 12.0
                    with pytest.raises(OSError):
                        push(reopened, hub)
                    assert recovery.fault(reopened)['attempts'] == 2
            finally:
                hub.close()


@pytest.mark.parametrize('limit', ['attempts', 'elapsed'])
def test_http_limit_pause_isolates_active_healthy_stream(live, tmp_path, monkeypatch, limit):
    from hermes_cli import kanban_db_sync as sync, kanban_source_clones
    from tests.hermes_cli.test_kanban_replay_journal import binding
    from tests.hermes_cli.test_kanban_replay_state_model import custody
    sync.set_mode(live, 'shadow')
    kb.create_task(live, title='paused HTTP stream', assignee='worker')
    with clocks(monkeypatch) as clock:
        peer = Failing(clock, duration=150 if limit == 'elapsed' else 0)
        attempts = 2 if limit == 'elapsed' else 8
        with http.serving(live, peer, lose_ops_reply=True) as (url, requests, _):
            hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
            try:
                for attempt in range(1, attempts + 1):
                    with pytest.raises(OSError):
                        push(live, hub)
                    held = recovery.fault(live)
                    assert held['attempts'] == attempt
                    assert bool(held['paused']) == (attempt == attempts)
                    assert peer.calls[-2:] == ['ops', 'inspect']
                    clock['mono'] = held['retry_after']
                assert held['reachable_s'] == (300 if limit == 'elapsed' else 0)
                assert len([p for _, p, _, _ in requests if p.endswith('/ops')]) == attempts
            finally:
                hub.close()
        frozen = custody(live)
        broken_path = Path(live.execute('PRAGMA database_list').fetchone()[2])
        healthy_path = tmp_path / 'healthy.db'
        with kbc.connect(healthy_path) as healthy:
            client.ensure_identity(healthy, 'healthy', 'macbook')
            sync.set_mode(healthy, 'shadow')
            with kbc.write_txn(healthy):
                journal.initialize(healthy, binding(), board_id=healthy.execute(
                    'SELECT board_id FROM kanban_sync_state').fetchone()[0])
            kb.create_task(healthy, title='active healthy HTTP stream', assignee='worker')
            expected = healthy.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
            good = http.AckReceiver()

            class Multiplex:
                slot = good.slot

                def _call(self, method, path, body=None):
                    if '/changes?' in path:
                        from urllib.parse import parse_qs, urlsplit
                        since = int(parse_qs(urlsplit(path).query)['since'][0])
                        return dict(changes=[], next=since, head=since, more=False)
                    assert '/boards/healthy/' in path, 'paused stream performed replay HTTP I/O'
                    return good._call(method, path, body)

            monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda hub: None)
            monkeypatch.setattr(kanban_source_clones, 'attest_if_due', lambda hub: None)
            with http.serving(healthy, Multiplex()) as (url, requests, observations):
                hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
                try:
                    results = client.sync_all_once(hub, boards=[('default', broken_path), ('healthy', healthy_path)])
                finally:
                    hub.close()
            assert len(results) == 2 and 'replay-resume' in results[0].error
            assert results[1].error is None, results[1].error
            assert custody(live) == frozen
            assert recovery.fault(live) == held
            assert journal.snapshot(healthy)['settled'] == good.slot['a'] == str(expected)
            assert not healthy.execute('SELECT 1 FROM kanban_sync_outbox').fetchone()
            assert len(observations) == expected
        with pytest.raises(ValueError, match='identity'):
            recovery.resume(live, 'wrong', 'checked repair')
        assert custody(live) == frozen
        recovery.resume(live, held['op_id'], 'checked repair')
        peer.fail = False
        with http.serving(live, peer) as (url, _, _):
            hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
            try:
                assert push(live, hub).applied == 1
            finally:
                hub.close()
        assert peer.sent[0]['client_op_id'] == held['op_id']
        assert recovery.fault(live) is None


def test_http_fault_write_failure_rolls_back_without_losing_custody(live, monkeypatch):
    import sqlite3
    from tests.hermes_cli.test_kanban_replay_state_model import custody
    kb.create_task(live, title='fault metadata commit failure', assignee='worker')
    with clocks(monkeypatch) as clock:
        peer = Failing(clock)
        with http.serving(live, peer, lose_ops_reply=True) as (url, _, _):
            hub = client.Hub(client.Identity(url, 'fixture-key', 'macbook'))
            try:
                for _ in range(7):
                    with pytest.raises(OSError):
                        push(live, hub)
                    clock['mono'] = recovery.fault(live)['retry_after']
                held = recovery.fault(live)
                frozen = custody(live)
                real_txn = recovery.write_txn

                @contextmanager
                def fail_commit(conn):
                    with real_txn(conn):
                        yield
                        raise sqlite3.OperationalError('fixture fault metadata commit failed')

                with monkeypatch.context() as patch:
                    patch.setattr(recovery, 'write_txn', fail_commit)
                    with pytest.raises(sqlite3.OperationalError, match='fault metadata commit failed'):
                        push(live, hub)
                assert recovery.fault(live) == held and held['attempts'] == 7
                assert custody(live) == frozen
                path = Path(live.execute('PRAGMA database_list').fetchone()[2])
                with kbc.connect(path) as reopened:
                    assert recovery.fault(reopened) == held
                    assert custody(reopened) == frozen
                    with pytest.raises(OSError):
                        push(reopened, hub)
                    stopped = recovery.fault(reopened)
                    assert stopped['paused'] == 1 and stopped['attempts'] == 8
                    assert stopped['op_id'] == held['op_id']
                    after = custody(reopened)
                    assert after[:2] == frozen[:2] and after[3] == frozen[3]
                    exposure_index = next(row[0] for row in reopened.execute(
                        'PRAGMA table_info(kanban_sync_outbox)') if row[1] == 'exposure_history')
                    # Retry commits a new session exposure, not a new intent.
                    assert [row[:exposure_index] + row[exposure_index + 1:] for row in after[2]] == [
                        row[:exposure_index] + row[exposure_index + 1:] for row in frozen[2]]
                    import json
                    previous = json.loads(next(row for row in frozen[2] if row[0] == held['op_id'])[exposure_index])
                    current = json.loads(next(row for row in after[2] if row[0] == held['op_id'])[exposure_index])
                    assert current['exposures'][:-1] == previous['exposures']
                    assert current['exposures'][-1]['wire']['client_op_id'] == held['op_id']
                assert not peer.sent
            finally:
                hub.close()


def push(conn, hub):
    return client.push(conn, hub, 'default', 'macbook', primary=False, max_batches=1)


@pytest.mark.parametrize('wall_jump', [-10000, 10000])
def test_http_reachable_elapsed_ignores_wall_clock_jumps(live, monkeypatch, wall_jump):
    kb.create_task(live, title='clock-safe HTTP recovery', assignee='worker')
    with clocks(monkeypatch) as clock:
        peer = Failing(clock, duration=100, wall_jump=wall_jump)
        with http.serving(live, peer, lose_ops_reply=True) as (url, requests, _):
            hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
            try:
                for attempt in range(1, 4):
                    with pytest.raises(OSError):
                        push(live, hub)
                    held = recovery.fault(live)
                    assert held['reachable_s'] == attempt * 100, 'wall-clock jump changed reachable elapsed'
                    assert bool(held['paused']) == (attempt == 3)
                    assert held['retry_after'] == clock['mono'] + min(60, 2 ** attempt)
                    assert peer.calls[-1] == 'inspect'
                    clock['mono'] = held['retry_after']
                before = list(requests)
                with pytest.raises(client.SyncUnavailable, match='replay-resume'):
                    push(live, hub)
                assert requests == before
                assert journal.pending_wire(live) is not None
            finally:
                hub.close()
