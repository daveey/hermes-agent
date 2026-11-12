"""Stale unspawned claims use canonical failure finalization."""

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_db as base

kanban_home = base.kanban_home


def test_stale_claim_reclaim_without_spawn_counts_toward_breaker(kanban_home):
    """A claim that expires without a worker ever spawning is a non-success
    attempt (#111306): each automatic reclaim advances ``consecutive_failures``
    and the breaker trips at ``failure_limit`` instead of the card spinning
    claim -> reclaim -> claim forever."""
    with kbc.connect() as conn:
        t = kb.create_task(conn, title="never spawned", assignee="a")
        host = kb._claimer_id().split(":", 1)[0]
        for expected in (1, 2):
            kb.claim_task(conn, t, claimer=f"{host}:worker")
            # No _set_worker_pid: the claimer never spawned a worker.
            conn.execute(
                "UPDATE tasks SET claim_expires = ? WHERE id = ?",
                (int(time.time()) - 3600, t),
            )
            assert kb.release_stale_claims(
                conn, signal_fn=lambda _p, _s: None, failure_limit=2,
            ) == 1
            row = conn.execute(
                "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (t,),
            ).fetchone()
            assert row["consecutive_failures"] == expected
        assert row["status"] == "triage"
        kinds = [e.kind for e in kb.list_events(conn, t)]
        assert kinds[-3:] == ["reclaimed", "gave_up", "failure_finalized"]
        assert kinds.count("gave_up") == 1
        assert kinds.count("failure_finalized") == 2
