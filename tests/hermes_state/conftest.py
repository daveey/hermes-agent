"""Shared fixtures for tests/hermes_state."""
import time

import pytest


@pytest.fixture
def settled_clean_keys(monkeypatch):
    """[daveey] Judge deleted-WAL clean-verdict keys as if taken an hour after the test's files were made.

    A clean verdict is only cached once every timestamp in its key is older than
    ``_CLEAN_KEY_SETTLE_NS`` (a same-tick recreate is indistinguishable), and a test's sidecars are
    seconds old. Tests about the cache itself age them through the module's wall clock."""
    import hermes_state_dbfile
    monkeypatch.setattr(hermes_state_dbfile, "_wall_clock_ns", lambda: time.time_ns() + 3600 * 10**9)


@pytest.fixture
def worker(tmp_path):
    """A registered compute worker execution on session ``owned``: ``(db, RuntimeSessionStore)``."""
    # Imported here, not at module level: this conftest loads for every hermes_state test file.
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, register_worker_execution
    from tests.worker_store_rpc import worker_store_rpc

    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owned', 'cli', system_prompt='prefix')
    db.create_session('foreign', 'cli', system_prompt='secret')
    epoch = begin_runtime_epoch(db, instance_id='fixture')
    scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
    register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
    store = RuntimeSessionStore(lambda method, **p: worker_store_rpc(db, method, **p), scope, tmp_path / 'outbox')
    yield db, store
    store.failure = None
    store.journal['pending'] = []
    store.close()
    db.close()
