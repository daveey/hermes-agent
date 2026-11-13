"""The final hub gate reads the advisory sampler's fresh sample; it never measures again under the lock.

Hub 2026-11-13 (card t_01a1d2e5c4899f40): with the advisory fresh (0.5 s old, 23 % busy) every claim of a top-priority
card took a Board lease and then refused under ``admission.lock`` as ``hub_cpu_unknown`` and released the grant
(``unused_grant``), about every 5 min. The gateway process holds the GIL ~100 % of a core, so the locked whole-host
census ran ~1.3 s; census + the independent ~1 s sample + the recount overran the 2.25 s hold budget, and the
overrun was reported as an unknown CPU reading. The advisory could not predict it, so the lease churned.
"""
import json
import time

import pytest

from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import test_kanban_hub_admission as helpers

pytestmark = pytest.mark.platforms("posix")
PRELEASE_MAX_AGE_MS = ka.ADVISORY_MAX_AGE_MS - getattr(ka, "PRELEASE_HEADROOM_MS", 5_000)

board = helpers.board
configure, advisory, task, claim, refused = (helpers.configure, helpers.advisory, helpers.task, helpers.claim,
                                             helpers.refused)


def no_own_sample(monkeypatch):
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("the final gate must read the sampler's sample"))


@pytest.mark.parametrize("review", [False, True])
def test_final_gate_admits_on_the_sampler_sample_without_measuring_under_the_lock(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home, pct=23.0, age_ms=500)
    no_own_sample(monkeypatch)
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed'",
                                      (t,)).fetchone()[0])
    written = mm.read_hub_cpu_sample(home)
    assert payload["hub_cpu"] == {**written, "limit_pct": 50.0}  # the very triple the sampler wrote


@pytest.mark.parametrize("review", [False, True])
def test_slow_locked_census_on_a_gil_starved_hub_still_admits_inside_the_budget(board, monkeypatch, review):
    """The hub's case: a ~1.3 s census under the lock. One census and no sleep fit the 2.25 s hold."""
    conn, home = board
    configure(home, cap=7)
    advisory(home)
    no_own_sample(monkeypatch)
    calls = []

    def slow_census(*a, **k):
        calls.append(time.monotonic())
        time.sleep(0.9)  # health: allow HX013 -- stands in for the GIL-starved hub census (~1.3 s measured)
        return 0
    monkeypatch.setattr(mm, "active_card_runs", slow_census)
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    assert len(calls) == 1  # one locked census: nothing between it and the commit needs a recount


def test_advisory_keeps_lease_headroom_so_the_accepted_sample_is_still_fresh_under_the_lock(board, monkeypatch):
    """Defer BEFORE the lease when the sample would be too old by the time the locked gate reads it."""
    conn, home = board
    configure(home)
    advisory(home, age_ms=PRELEASE_MAX_AGE_MS + 100)
    monkeypatch.setattr(sync, "claim_gate", lambda *a, **k: pytest.fail("a soon-stale sample must not take a lease"))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_unknown")


def test_sample_accepted_before_the_lease_is_still_fresh_after_a_slow_lease(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home, age_ms=PRELEASE_MAX_AGE_MS - 100)
    no_own_sample(monkeypatch)
    real_gate, real_now = sync.claim_gate, ka._now

    def slow_lease(*a, **k):  # the Board round-trip ages the sample by most of the headroom
        monkeypatch.setattr(ka, "_now", lambda: real_now() + (ka.ADVISORY_MAX_AGE_MS - PRELEASE_MAX_AGE_MS - 500) / 1000)
        return real_gate(*a, **k)
    monkeypatch.setattr(sync, "claim_gate", slow_lease)
    t = task(conn)
    assert kb.claim_task(conn, t) is not None


@pytest.mark.parametrize("change,reason", [("stale", "hub_cpu_sample_stale"), ("missing", "hub_cpu_sample_stale"),
                                           ("busy", "hub_cpu_at_or_above_limit"), ("window", "hub_cpu_sample_stale")])
def test_sample_that_changes_after_the_advisory_is_rechecked_under_the_lock(board, monkeypatch, change, reason):
    conn, home = board
    configure(home)
    advisory(home)
    no_own_sample(monkeypatch)
    real_gate = sync.claim_gate

    def lease_then_change(*a, **k):
        path = home / "cache" / mm.HUB_CPU_SAMPLE_FILE
        if change == "stale":
            advisory(home, age_ms=ka.ADVISORY_MAX_AGE_MS + 1)
        elif change == "missing":
            path.unlink()
        elif change == "busy":
            advisory(home, pct=50.0)
        else:
            path.write_text(json.dumps({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": cm.MAX_WINDOW_MS + 1,
                                        "cpu_busy_sample_end_ms": int(time.time() * 1000)}))
        return real_gate(*a, **k)
    monkeypatch.setattr(sync, "claim_gate", lease_then_change)
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, reason)
