"""A worker journal entry is acknowledged only by its own committed receipt (t_01a1a8fa8be4217f).

Run 158945 died with a sticky ``invalid_params`` (``expected 273, got 274``): the worker journal had
dropped sequence 273 on a success reply although the owner never committed it (``last_sequence``
272, no receipt row 273). The worker correlated replies by RPC id alone, so any success frame with
that id (another write's reply, a duplicate or late frame) advanced the journal past a write the owner
never made, and the next write hit the owner's sequence fence forever.

Now the owner stamps ``_receipt`` = (execution, sequence) after the commit, the worker re-sends an
entry whose reply is not its receipt, and a divergence that happens anyway re-sends the gap from the
retained acknowledged entries or fails ``lost_receipt``. The owner's sequence fence is unchanged.
"""
import json
import queue
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed

import hermes_state_runtime as rt
from tests.hermes_state.test_runtime_worker import append, setup_worker

SCOPE = {'session_id': 's', 'execution_id': 'w', 'epoch': 1, 'generation': 1}


class _Owner:
    """An owner over a scripted socket: commits in sequence order behind the real fence, stamps
    receipts like ``apply_worker_receipt``, and can answer a write with a frame that is not its receipt."""

    def __init__(self, misdeliver=()):
        self.last_sequence = 0
        self.committed = {}
        self.misdeliver = set(misdeliver)
        self.sent = []

    def handle(self, params):
        sequence = params['sequence']
        self.sent.append(sequence)
        if sequence in self.misdeliver:
            self.misdeliver.discard(sequence)
            # Another write's success, delivered under this request's id; nothing is committed.
            return {'result': {'count': 1, 'annotations': [{}], '_receipt': {'execution_id': 'other-worker', 'sequence': sequence}}}
        if sequence in self.committed:
            return {'result': {**self.committed[sequence], '_receipt': {'execution_id': 'w', 'sequence': sequence}}}
        if sequence != self.last_sequence + 1:
            return {'error': {'code': 4001, 'message': 'invalid_params', 'data': {
                'reason': 'invalid_params', 'field': 'sequence',
                'detail': f'expected {self.last_sequence + 1}, got {sequence}',
                'expected_sequence': self.last_sequence + 1}}}
        self.last_sequence = sequence
        self.committed[sequence] = {'count': 1, 'annotations': [{}]}
        return {'result': {'count': 1, 'annotations': [{}], '_receipt': {'execution_id': 'w', 'sequence': sequence}}}


def _wire(monkeypatch, owner, extra_frames=None):
    endpoint = SimpleNamespace(api_origin='http://127.0.0.1:1234')
    monkeypatch.setattr('hermes_cli.gateway_runtime.discover_gateway_endpoint',
                        lambda *a, **k: SimpleNamespace(state='ready', endpoint=endpoint))
    monkeypatch.setattr('hermes_cli.gateway_runtime.control_home_for', lambda home, endpoint: home)
    monkeypatch.setattr('hermes_cli.gateway_runtime_discovery.query_identify', lambda *a, **k: {'pid': -1})
    monkeypatch.setattr('hermes_cli.gateway_client._session_ticket', lambda *a, **k: 'ticket')

    def connect(url, **kwargs):
        frames = queue.SimpleQueue()

        def recv():
            frame = frames.get()
            if frame is None:
                raise ConnectionClosed(None, None)
            return frame

        def send(value):
            request = json.loads(value)
            reply = owner.handle(request['params'])
            frames.put(json.dumps({'jsonrpc': '2.0', 'id': request['id'], **reply}))
            for frame in (extra_frames or (lambda request: []))(request):
                frames.put(json.dumps(frame))
        return SimpleNamespace(send=send, recv=recv, close=lambda: frames.put(None))
    monkeypatch.setattr('websockets.sync.client.connect', connect)


def test_a_reply_that_is_not_the_writes_receipt_does_not_skip_the_sequence(tmp_path, monkeypatch):
    from agent.runtime_session_store import RuntimeSessionStore, WorkerRPC
    owner = _Owner(misdeliver={2})
    _wire(monkeypatch, owner)
    rpc = WorkerRPC(tmp_path / 'home')
    store = RuntimeSessionStore(rpc, SCOPE, tmp_path / 'outbox')
    try:
        for content in ('one', 'two', 'three'):
            store.append_messages_batch('s', [{'role': 'user', 'content': content}])
        assert owner.last_sequence == 3 and sorted(owner.committed) == [1, 2, 3]
        assert owner.sent == [1, 2, 2, 3]  # the misdelivered write was re-sent, never skipped
        assert store.failure is None
        assert json.loads(store.path.read_text())['pending'] == []
    finally:
        store.close()
        rpc.close()


def test_a_duplicate_reply_frame_is_not_the_next_calls_reply(tmp_path, monkeypatch):
    from agent.runtime_session_store import WorkerRPC
    owner = _Owner()
    # The owner's reply to the first call arrives twice; the copy must not answer the second call.
    _wire(monkeypatch, owner, extra_frames=lambda request: (
        [{'jsonrpc': '2.0', 'id': request['id'], 'result': {'count': 9, 'annotations': [{}], '_receipt': {
            'execution_id': 'w', 'sequence': request['params']['sequence']}}}]
        if request['params']['sequence'] == 1 else []))
    rpc = WorkerRPC(tmp_path)
    try:
        assert rpc('worker.persist', **SCOPE, sequence=1, operation='transcript.append', payload={}) == {'count': 1, 'annotations': [{}]}
        assert rpc('worker.persist', **SCOPE, sequence=2, operation='transcript.append', payload={}) == {'count': 1, 'annotations': [{}]}
        assert owner.last_sequence == 2
    finally:
        rpc.close()


def _store_over(tmp_path, owner, *, forge=()):
    """A store whose rpc acknowledges ``forge`` sequences with a matching receipt but no commit: a
    divergence the receipt check cannot see, which recovery must still survive."""
    from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
    forged = set(forge)

    def rpc(method, **params):
        if params['sequence'] in forged:
            forged.discard(params['sequence'])
            owner.sent.append(params['sequence'])
            return {'count': 1, 'annotations': [{}]}
        reply = owner.handle(params)
        if 'error' in reply:
            failure = WorkerPersistenceError(reply['error']['message'])
            failure.data = reply['error']['data']
            raise failure
        return {key: value for key, value in reply['result'].items() if key != '_receipt'}
    return RuntimeSessionStore(rpc, SCOPE, tmp_path / 'outbox')


def test_a_gap_the_journal_still_holds_is_re_sent_instead_of_dying(tmp_path):
    owner = _Owner()
    store = _store_over(tmp_path, owner, forge={3})
    try:
        for index in range(5):
            store.append_messages_batch('s', [{'role': 'user', 'content': f'm{index}'}])
        assert owner.last_sequence == 5 and sorted(owner.committed) == [1, 2, 3, 4, 5]
        assert owner.sent == [1, 2, 3, 4, 3, 4, 5]
        assert store.failure is None
    finally:
        store.close()


def test_a_gap_the_journal_no_longer_holds_fails_lost_receipt(tmp_path, monkeypatch):
    import agent.runtime_session_store as store_module
    from agent.runtime_session_store import WorkerPersistenceError
    monkeypatch.setattr(store_module, '_ACKED_KEEP', 1)
    owner = _Owner()
    store = _store_over(tmp_path, owner, forge={2, 3})
    try:
        for index in range(3):
            store.append_messages_batch('s', [{'role': 'user', 'content': f'm{index}'}])
        with pytest.raises(WorkerPersistenceError, match='lost_receipt'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'm3'}])
        assert store.failure == 'lost_receipt'
        assert owner.last_sequence == 1  # nothing re-numbered or forced past the fence
        assert [entry['sequence'] for entry in json.loads(store.path.read_text())['pending']] == [4]
    finally:
        with pytest.raises(WorkerPersistenceError):
            store.close()


def test_owner_stamps_the_committed_receipt_and_names_the_expected_sequence(tmp_path):
    from gateway.session_controls import _refusal_data
    from gateway.session_worker import stamp_worker_receipt
    db, epoch, assignment = setup_worker(tmp_path)
    try:
        first = append(db, epoch=epoch, **assignment, sequence=1, content='first')
        params = {**assignment, 'sequence': 1}
        assert stamp_worker_receipt(first, params)['_receipt'] == {
            'execution_id': assignment['execution_id'], 'sequence': 1}
        stored = db._conn.execute("SELECT result_json FROM worker_receipts WHERE sequence=1").fetchone()[0]
        assert '_receipt' not in json.loads(stored)
        with pytest.raises(rt.RuntimeStoreError) as caught:
            append(db, epoch=epoch, **assignment, sequence=3, content='skipped')
        assert _refusal_data(caught.value)['expected_sequence'] == 2
    finally:
        db.close()
