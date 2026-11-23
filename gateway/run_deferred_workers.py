"""Release-side publication for executor work that outlives its turn [daveey]."""
from contextlib import suppress


def discard_deferred_worker(runner, workers, done_future):
    workers.pop(done_future, None)
    # No later waiter remains; consume failures so asyncio does not warn (#98973).
    if not done_future.cancelled():
        with suppress(Exception):
            done_future.exception()
    # The originating turn published while this worker was live. Recount AFTER release.
    runner._persist_active_agents()
