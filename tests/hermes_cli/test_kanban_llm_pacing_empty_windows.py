"""LLM pacing follow-ups from the PR #3058 review (t_01a1db2e63e89126). [daveey]

1. (historical) paced -> held(until T) -> paced: ``kanban_dispatch_skip.record`` dedupes against every skip of the episode, so the
   second ``paced`` appended nothing. Past T the newest row was the expired ``held`` one, ``_llm_paced_hold`` returned
   None and ``kanban diagnostics`` called the still-paced card ``stranded_in_ready`` (error). A skip that replaces a
   time-bounded newest row (one carrying ``until``) is now appended again.
2. An account whose usage read succeeded with BOTH account windows ``null`` (fully idle / brand-new) produced no
   reading at all, so it added no fresh-window headroom. ``read_pool`` now reads it as two fresh windows (0%, no
   reset).
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli.test_kanban_llm_pacing_followups import CFG, DAY, HOUR, _skips, paced_board
from tests.hermes_cli.test_kanban_model_tier import board as _board_fixture

board = _board_fixture
__all__ = ["paced_board"]


# --- 1. a skip superseding a time-bounded newest row ----------------------------------------------------------
# (The dispatch-level paced -> held -> paced flips were removed with the start limit on 2026-11-19: a paced pool
# records no skip any more. The skip record's dedup is exercised directly below.)


def test_a_replica_catching_up_does_not_duplicate_the_current_skip(paced_board):
    """The superseding append keys on this host's current skip: when that is already the new key (a pickup copy
    re-imported before the replica pulled this host's row back), nothing is appended again."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        assert skips.record(conn, tid, skips.LLM_PACED, detail="anthropic held", until=time.time() + 60)
        assert skips.record(conn, tid, skips.LLM_PACED, detail="anthropic paced")
        # The replica copy lost the just-pushed ``paced`` row: the newest event is the timed ``held`` again.
        conn.execute("DELETE FROM task_events WHERE id = (SELECT max(id) FROM task_events WHERE task_id = ? "
                     "AND kind = ?)", (tid, skips.EVENT))
        conn.commit()
        assert not skips.record(conn, tid, skips.LLM_PACED, detail="anthropic paced")


@pytest.mark.parametrize("refresh_age", [skips.SEEN_REFRESH_SECONDS, skips.SEEN_REFRESH_SECONDS + 1])
def test_replica_lag_keeps_current_skip_deduplicated_after_refresh(paced_board, monkeypatch, refresh_age):
    """An expired local fast path must not re-append paced while the replica still ends at held."""
    clock = [1_800_000_000]
    monkeypatch.setattr(skips.time, "time", lambda: clock[0])
    paced = {"reason": skips.LLM_PACED, "detail": "anthropic paced"}
    held = {"reason": skips.LLM_PACED, "detail": "anthropic held", "until": clock[0] + 60}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="replica lag past refresh", assignee="default")
        assert skips.record(conn, tid, skips.LLM_PACED, detail="anthropic held", until=clock[0] + 60)
        clock[0] += 1
        assert skips.record(conn, tid, skips.LLM_PACED, detail="anthropic paced")
        since = clock[0]
        # Model the re-imported event history, retaining the machine-local seen table.
        conn.execute("DELETE FROM task_events WHERE id = (SELECT max(id) FROM task_events WHERE task_id = ? "
                     "AND kind = ?)", (tid, skips.EVENT))
        conn.commit()
        assert _skips(conn, tid) == [held]
        for _ in range(3):
            clock[0] += refresh_age  # reach the write-path recheck, without mocking _settled
            assert not skips.record(conn, tid, skips.LLM_PACED, detail="anthropic paced")
            assert _skips(conn, tid) == [held]
            assert skips.latest(conn, tid) == {**paced, "at": since}
            row = conn.execute(f"SELECT payload, since, seen_at FROM {skips.SEEN_TABLE} WHERE task_id = ?",
                               (tid,)).fetchone()
            assert kb._json_dict(row[0]) == paced
            assert (row[1], row[2]) == (since, clock[0])


# --- 2. an account with both windows null is fresh headroom --------------------------------------------------

def _window(label, used, reset_in=None, *, now, scope="account"):
    from datetime import UTC, datetime
    reset = None if reset_in is None else datetime.fromtimestamp(now + reset_in, tz=UTC)
    return SimpleNamespace(label=label, used_percent=used, reset_at=reset, scope=scope)


def _snap(*windows, unavailable_reason=None):
    return SimpleNamespace(windows=tuple(windows), details=(), unavailable_reason=unavailable_reason,
                           available=bool(windows) and not unavailable_reason)


def test_an_account_read_with_both_windows_null_counts_as_two_fresh_windows(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(pd, "_snapshots", lambda provider: [
        ("busy", _snap(_window("Current session", 60, HOUR, now=now), _window("Current week", 10, 6 * DAY, now=now))),
        ("idle", _snap()),  # /api/oauth/usage: five_hour null, seven_day null
        ("opus-only", _snap(_window("Opus week", 5, 6 * DAY, now=now, scope="model"))),
        ("failed", None),  # the read itself failed: unknown, never headroom
        ("oauth-less", _snap(unavailable_reason="not an OAuth account")),
    ])
    current = pd.read_pool("anthropic", now)
    fresh = sorted((r.account, r.kind, r.used, r.reset_at) for r in current if r.account != "busy")
    assert fresh == [("idle", lp.FIVE_HOUR, 0.0, None), ("idle", lp.SEVEN_DAY, 0.0, None),
                     ("opus-only", lp.FIVE_HOUR, 0.0, None), ("opus-only", lp.SEVEN_DAY, 0.0, None)]

    history = [lp.Reading(now - HOUR, "busy", lp.FIVE_HOUR, 30, now + HOUR)]
    pace = lp.pool_pace("anthropic", current, history, CFG)
    five_hour = ((95 - 60) / HOUR + 2 * 95 / lp.WINDOW_SECONDS[lp.FIVE_HOUR]) / (30 / HOUR)
    assert pace.factor == pytest.approx(five_hour, rel=1e-3)


def test_a_fully_idle_pool_is_open():
    now = 1_800_000_000.0
    current = [lp.Reading(now, "idle", kind, 0.0, None) for kind in (lp.FIVE_HOUR, lp.SEVEN_DAY)]
    pace = lp.pool_pace("anthropic", current, [], CFG)
    assert pace.state == lp.OPEN
