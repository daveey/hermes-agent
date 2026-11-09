"""A managed worker's agent gets a RuntimeSessionStore as its session_db. Every SessionDB method
the agent core calls must exist on the store, accept the same keywords, and answer like the
owner's SessionDB (Adolanium reg-1 was one missing keyword; the sweep found two missing reads:
the failed-turn boundary's tail role and in-place compaction's held-row liveness)."""
import ast
import inspect
import json
from pathlib import Path
import re

import pytest

from agent.runtime_session_store import RuntimeSessionStore
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution

ROOT = Path(__file__).resolve().parents[2]
# The agent core: everything a worker's AIAgent runs (tools open their own handles, or refuse).
CORE = [ROOT / 'run_agent.py', *sorted((ROOT / 'agent').glob('*.py'))]


def _core_session_db_calls():
    """Names called on the agent's store: ``x._session_db`` / ``x.db`` / ``session_db``, and a local
    ``db`` where the module binds it from ``_session_db`` (a module opening its own SessionDB is not)."""
    names = set()
    for path in CORE:
        text = path.read_text(encoding='utf-8')
        local_db = re.search(r'\bdb\b[^=\n]*=\s*getattr\([^)\n]*_session_db', text) is not None
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if ((isinstance(owner, ast.Attribute) and owner.attr in ('_session_db', 'db'))
                        or (isinstance(owner, ast.Name) and (owner.id == 'session_db' or (owner.id == 'db' and local_db)))):
                    names.add(node.func.attr)
    return sorted(n for n in names if hasattr(SessionDB, n))


@pytest.mark.parametrize('name', _core_session_db_calls())
def test_store_offers_every_core_session_db_call_with_its_keywords(name):
    assert hasattr(RuntimeSessionStore, name), f'RuntimeSessionStore lacks {name}'
    store = inspect.signature(getattr(RuntimeSessionStore, name)).parameters
    if any(p.kind is p.VAR_KEYWORD for p in store.values()):
        return
    owner = inspect.signature(getattr(SessionDB, name)).parameters
    missing = [k for k, p in owner.items() if k not in store and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
    assert missing == [], f'RuntimeSessionStore.{name} rejects {missing}'


def test_tail_reads_answer_like_the_owner(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli')
        db.append_messages_batch('s', [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'},
                                       {'role': 'user', 'content': 'failed turn'}])
        db.create_session('other', 'cli')
        db.append_messages_batch('other', [{'role': 'assistant', 'content': 'x'}])
        rows = [r[0] for r in db._read_all('SELECT id FROM messages ORDER BY id')]
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='w', session_id='s', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
        try:
            assert store.latest_conversation_role('s') == db.latest_conversation_role('s') == 'user'
            for row_id in [*rows, 10**6]:  # another transcript's row is not this session's
                assert store.get_message_role('s', row_id) == db.get_message_role('s', row_id)
        finally:
            store.close()
    finally:
        db.close()


def _worker_store(db, tmp_path, session_id='s'):
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='w', session_id=session_id, generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
    return RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')


def test_update_session_model_writes_like_the_owner(tmp_path):
    """[daveey] Class-metadata persistence calls update_session_model on the worker's store."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', model='old', model_config={'browser_model_lock': 'x', 'keep': 1,
                                                                  'model_routing': {'class': 'c'}})
        store = _worker_store(db, tmp_path)
        try:
            store.update_session_model('s', 'new-model')
            row = db.get_session('s')
            assert row['model'] == 'new-model'
            config = json.loads(row['model_config'])
            assert config == {'keep': 1, 'model': 'new-model', 'model_routing': {'class': 'c'}}
            store.update_session_model('s', 'm2', 'prov', base_url='https://x', clear_model_routing=True)
            config = json.loads(db.get_session('s')['model_config'])
            # Same merge as the owner's SessionDB.update_session_model (None deletes a key).
            db.create_session('o', 'cli', model='old', model_config={'browser_model_lock': 'x', 'keep': 1,
                                                                      'model_routing': {'class': 'c'}})
            db.update_session_model('o', 'new-model')
            db.update_session_model('o', 'm2', 'prov', base_url='https://x', clear_model_routing=True)
            assert config == json.loads(db.get_session('o')['model_config'])
            assert config['gateway_runtime']['provider'] == 'prov' and config['model'] == 'm2'
            assert 'model_routing' not in config and 'browser_model_lock' not in config
            with pytest.raises(Exception):
                store.update_session_model('other', 'm')
        finally:
            store.close()
    finally:
        db.close()


def test_lease_key_answers_only_inside_the_workers_lineage(tmp_path):
    """[daveey] session_was_taken_over compares lineage keys through the worker's store."""
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('root', 'cli')
        db.end_session('root', 'compression')
        db.create_session('tip', 'cli', parent_session_id='root')
        db.create_session('a', 'cli')
        db.create_session('b', 'cli')
        assert db._session_turn_lease_key('tip') == 'root'
        store = _worker_store(db, tmp_path, session_id='tip')
        try:
            assert store._session_turn_lease_key('tip') == store._session_turn_lease_key('root') == 'root'
            assert store._session_turn_lease_key('a') != store._session_turn_lease_key('b')
            assert store._session_turn_lease_key('a') != 'root'
            assert store._session_turn_lease_key('a') != db._session_turn_lease_key('a')
        finally:
            store.close()
    finally:
        db.close()


def test_seed_is_an_owner_write_the_worker_never_journals(tmp_path, caplog):
    """[daveey] ``seed_session_model_config`` fills keys the row lacks, which on a worker's row means
    owner-reserved routing/lineage config: the store writes nothing, journals nothing, latches no failure,
    and refuses another session's row. The adopted-row path that probes it stays silent."""
    import logging
    import types

    from agent.runtime_session_store import WorkerPersistenceError
    from agent.turn_facade_lease import _seed_adopted_row_model_config

    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('s', 'cli', model_config={'model_routing': {'class': 'c'}})
        db.create_session('other', 'cli')
        store = _worker_store(db, tmp_path)
        try:
            before = json.loads(db.get_session('s')['model_config'])
            sequence = store.journal['next_sequence']
            seed = {'max_iterations': 3, '_delegate_from': 'forged', 'model_routing': None}
            assert store.seed_session_model_config('s', seed) is None
            agent = types.SimpleNamespace(_persist_disabled=False, _session_row_model_config=lambda: seed)
            with caplog.at_level(logging.WARNING, logger='agent.turn_facade_lease'):
                _seed_adopted_row_model_config(agent, store, 's')
            assert not [r for r in caplog.records if 'adopted session row' in r.getMessage()]
            assert json.loads(db.get_session('s')['model_config']) == before
            assert store.journal['next_sequence'] == sequence and store.journal['pending'] == []
            assert store.failure is None
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.seed_session_model_config('other', {'max_iterations': 3})
            assert json.loads(db.get_session('other')['model_config'] or '{}') == {}
            # The store still writes sidecars afterwards: seeding latched nothing.
            store.patch_session_model_config('s', {'prompt_refresh_epoch': 5})
            assert json.loads(db.get_session('s')['model_config']) == {**before, 'prompt_refresh_epoch': 5}
        finally:
            store.close()
    finally:
        db.close()
