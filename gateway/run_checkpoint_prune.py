"""The daily checkpoint prune runs on its own thread, off the gateway housekeeping thread.

A real prune repacks the whole store (``git gc``): minutes on a multi-GB one. Run inline as the
housekeeping tick's last chore it held that thread for the whole prune, so no runtime heartbeat
landed (``hermes gateway status`` read the gateway as wedged) and the restart-safe cron-delivery
drain, with every other chore, stopped until it returned. The tick now only *requests* a pass.
"""
from __future__ import annotations

import contextvars
import threading

THREAD_NAME = 'gateway-checkpoint-prune'
CHORE_LABEL = 'Checkpoint prune tick'


def _prune() -> None:
    """``auto_prune_from_config`` gates on ``checkpoints.auto_prune`` and the 24h ``.last_prune``
    marker, so every pass but the daily one returns at once."""
    from tools.checkpoint_maintenance import auto_prune_from_config
    auto_prune_from_config()


class CheckpointPruneWorker:
    """One long-lived daemon thread per housekeeping loop; single-flight, coalescing.

    Custody is unchanged from the inline chore: each pass is one ``run_chore`` (one admitted
    ``gateway.housekeeping`` lease held for the whole prune; a refusal or a failure is logged
    there and the thread survives).
    """

    def __init__(self, logger, *, start=None):
        self._logger = logger
        # ``start`` registers the thread with its owner before starting it (the gateway passes
        # ``process_ownership.start_writer``, so a surviving prune keeps the runtime lock exactly
        # as the housekeeping thread it used to run on does).
        self._start = start or (lambda thread: thread.start())
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread: threading.Thread | None = None

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def request(self) -> None:
        """Ask for one pass. O(1), never blocks on a running pass; requests during a pass coalesce
        into at most one follow-up pass."""
        with self._lock:
            if self._stopped:
                return
            if self._thread is None:
                # The caller's context: the prune resolves the same Hermes home and profile scope
                # as the housekeeping thread did when it ran the prune itself.
                context = contextvars.copy_context()
                thread = threading.Thread(
                    target=context.run, args=(self._run,), name=THREAD_NAME, daemon=True)
                self._start(thread)
                self._thread = thread
            self._wake.set()

    def close(self) -> None:
        """No pass starts after this returns; a running pass is not interrupted (its ``git gc`` is a
        subprocess). The thread exits as soon as it is idle."""
        with self._lock:
            self._stopped = True
            self._wake.set()

    def _run(self) -> None:
        from gateway.run_drain_work import run_chore
        while True:
            self._wake.wait()  # no timeout: an idle worker never wakes
            with self._lock:
                if self._stopped:
                    return
                self._wake.clear()
            # ``close()`` after this point finds the pass already admitted to start.
            run_chore(self._logger, CHORE_LABEL, _prune)
