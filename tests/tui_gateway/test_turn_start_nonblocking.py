"""Turn-start publication must not put board projection locks on the prompt thread."""
import threading
import time

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.local_work_threads import TrackedThread
from tui_gateway import server, turn_outcome
from tests.tui_gateway import test_turn_outcome_refresh as helpers

# Keep the existing prompt harness; only the outcome worker is a real thread.
db = helpers.db
turn_env = helpers.turn_env


@pytest.mark.parametrize("lock_name", ["_counts_flight", "_links_lock"])
def test_run_body_invokes_agent_while_projection_locked(turn_env, db, lock_name):
    turn_env.setattr("hermes_cli.local_work_threads.TrackedThread", TrackedThread)
    held, entered, published, invoked = (threading.Event() for _ in range(4))
    release = threading.Event()
    lock = getattr(activity, lock_name)
    scope = ("turn-start-test",)
    turn_env.setattr(activity, "_origin_scope", lambda: scope)
    turn_env.setattr(activity, "_activity_fingerprint", lambda: ())
    turn_env.setattr(activity._revalidate, "counts_fresh", lambda *a: False)
    turn_env.setattr(activity, "_recompute_all_counts", lambda *a, **k: ())
    turn_env.setattr(activity._revalidate, "forced", lambda *a: True)
    turn_env.setattr(activity, "_links_fingerprint", lambda: ())
    turn_env.setattr(activity, "_publish_links", lambda *a: ())

    def hold():
        with lock:
            held.set()
            release.wait(60)

    def refresh(sid, trigger, **kwargs):
        if trigger == "turn_start":
            entered.set()
            if lock_name == "_counts_flight":
                activity._all_counts()
            else:
                activity._links_lookup(scope, False)
            published.set()

    turn_env.setattr(helpers.so, "refresh_outcome", refresh)
    agent = helpers._Agent("row-nonblocking", db, helpers.SUMMARY)
    session = helpers._session(agent)
    db.create_session(agent.session_id, source="desktop")
    invoke = server._invoke_agent

    def observe(*a, **kw):
        invoked.set()
        return invoke(*a, **kw)

    turn_env.setattr(server, "_invoke_agent", observe)
    # turn_env replaces threading.Thread with an inline prompt harness. Use the
    # real stdlib class so lock ownership and acceptance timing remain physical.
    from hermes_cli.local_work_threads import _RealThread
    holder = _RealThread(target=hold)
    prompt = _RealThread(target=lambda: server._run_prompt_submit(
        "rid", "ui-outcome-nonblocking", session, "work"))
    holder.start()
    assert held.wait(1)
    started = time.monotonic()
    prompt.start()
    try:
        assert entered.wait(1)
        assert invoked.wait(0.8), "board projection blocked the prompt before agent invocation"
        assert time.monotonic() - started < 1
        assert not published.is_set()
        # Keep the actual lock unavailable for the requested 60-second trial.
        holder.join(61)
        assert not holder.is_alive()
        assert published.wait(2), "admitted refresh was lost after projection unlocked"
    finally:
        release.set()
        holder.join(2)
        prompt.join(3)
    assert not prompt.is_alive()
