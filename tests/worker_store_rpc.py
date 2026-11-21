"""Direct owner operations with the worker RPC's error envelope for store fixtures."""
from agent.runtime_session_store import WorkerPersistenceError
from hermes_state_runtime import RuntimeStoreError, mutate_worker_execution
from hermes_state_worker_observations import worker_session_context


def worker_store_rpc(db, method, **params):
    try:
        if method == 'worker.sessionContext':
            return worker_session_context(db, **params)
        if method == 'worker.persist':
            return mutate_worker_execution(db, **params)
        raise AssertionError(f'unimplemented fixture RPC: {method}')
    except RuntimeStoreError as exc:
        failure = WorkerPersistenceError(str(exc))
        failure.data = getattr(exc, 'data', {})
        raise failure from exc
