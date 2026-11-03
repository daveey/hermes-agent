"""Final hub CPU/cap gate: an independent full CPU sample taken UNDER admission.lock, ready and review paths."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def configure(home, limit=50, cap=None):
    lines = ["kanban:"]
    if cap is not None:
        lines.append(f"  max_in_progress: {cap}")
    if limit is not None:
        lines += ["  admission:", f"    cpu_busy_below_pct: {limit}"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n")


def advisory(home, pct=10.0, age_ms=0):
    mm.record_hub_cpu_sample({"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": int(time.time() * 1000) - age_ms}, root=home)


def task(conn, review=False, title="gate"):
    t = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("review" if review else "ready", t))
    return t


def claim(conn, t, review):
    return (kb.claim_review_task if review else kb.claim_task)(conn, t)


def fake_sample(pct, calls=None, check=None):
    def sample(*a, **k):
        if check:
            check()
        if calls is not None:
            calls.append(time.monotonic())
        if pct is None:
            return None
        return {"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": int(time.time() * 1000)}
    return sample


def runs(conn):
    return conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]


def refused(conn, t, review, reason):
    assert kb.get_task(conn, t).status == ("review" if review else "ready")
    assert runs(conn) == 0
    assert sync.pop_claim_deferral(t) == reason


@pytest.mark.parametrize("review", [False, True])
def test_gate_absent_admits_without_sampling(board, monkeypatch, review):
    conn, _home = board
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("no root limit: must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is not None


@pytest.mark.parametrize("review", [False, True])
def test_below_limit_admits_and_claimed_event_carries_consumed_sample(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(49.999))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed'",
                                      (t,)).fetchone()[0])
    assert payload["hub_cpu"] == {"cpu_busy_pct": 49.999, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": payload["hub_cpu"]["cpu_busy_sample_end_ms"],
                                  "limit_pct": 50.0}


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("pct,reason", [(50.0, "hub_cpu_at_or_above_limit"), (50.001, "hub_cpu_at_or_above_limit"),
                                        (None, "hub_cpu_unknown")])
def test_at_or_above_limit_or_unknown_refuses_with_no_local_run(board, monkeypatch, review, pct, reason):
    conn, home = board
    configure(home)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(pct))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


@pytest.mark.parametrize("review", [False, True])
def test_invalid_root_limit_refuses_without_sampling(board, monkeypatch, review):
    conn, home = board
    configure(home, limit='"fifty"')  # type: ignore[arg-type]
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("invalid limit: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_cpu_unknown")


def test_profile_override_cannot_disable_root_gate(board, monkeypatch, tmp_path):
    conn, home = board
    configure(home)
    advisory(home)
    profile = home / "profiles" / "p"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: null\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: home)
    monkeypatch.setattr(cm, "sample", fake_sample(80.0))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_at_or_above_limit")


@pytest.mark.parametrize("review", [False, True])
def test_sample_is_taken_while_admission_lock_is_held(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    seen = []

    def check():
        try:
            with af.admission_lock(home, timeout=0):
                seen.append("free")
        except af.AdmissionBusy:
            seen.append("held")
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=check))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    assert seen == ["held"]


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("advice,reason", [(80.0, "hub_cpu_at_or_above_limit"), ("stale", "hub_cpu_unknown"),
                                           ("missing", "hub_cpu_unknown"), ("future", "hub_cpu_unknown")])
def test_advisory_precheck_defers_before_any_lease_or_sample(board, monkeypatch, review, advice, reason):
    conn, home = board
    configure(home)
    if advice == "stale":
        advisory(home, age_ms=15_001)
    elif advice == "future":
        advisory(home, age_ms=-5_000)
    elif advice != "missing":
        advisory(home, pct=advice)
    monkeypatch.setattr(sync, "claim_gate", lambda *a, **k: pytest.fail("advisory refusal must not ask for a lease"))
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("advisory refusal must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


def test_advisory_age_boundary_15000_ms_is_fresh(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home, age_ms=14_900)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0))
    t = task(conn)
    assert kb.claim_task(conn, t) is not None


@pytest.mark.parametrize("review", [False, True])
def test_whole_host_cap_is_rechecked_under_lock(board, monkeypatch, review):
    conn, home = board
    configure(home, cap=2)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0))
    monkeypatch.setattr(mm, "active_card_runs", lambda: 2)
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")
    monkeypatch.setattr(mm, "active_card_runs", lambda: None)  # an unreadable board fails closed
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")
    monkeypatch.setattr(mm, "active_card_runs", lambda: 1)
    assert claim(conn, t, review) is not None


def test_lease_expiring_during_sample_cannot_start(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home)
    real_time = time.time
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: monkeypatch.setattr(
        ka, "_now", lambda: real_time() + 10 ** 7)))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_unknown")


def test_simultaneous_claimers_at_6_of_7_start_at_most_one(board, monkeypatch):
    conn, home = board
    configure(home, cap=7)
    advisory(home)
    count = {"running": 6}
    gate_lock = threading.Lock()

    def active():
        with gate_lock:
            return count["running"]
    monkeypatch.setattr(mm, "active_card_runs", active)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: time.sleep(0.2)))  # health: allow HX013 -- stands in for the ~1 s sample held under the lock
    tasks = [task(conn, title=f"t{i}") for i in range(4)]

    def go(t):
        with kbc.connect(home / "kanban.db") as other:
            got = kb.claim_task(other, t)
        if got is not None:
            with gate_lock:
                count["running"] += 1
        return got
    for _ in range(3):  # contended rounds: losers defer (lock busy) or are refused at 7/7
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(go, tasks))
    assert runs(conn) == 1


def _lock_state(home):
    try:
        with af.admission_lock(home, timeout=0):
            return "free"
    except af.AdmissionBusy:
        return "held"


@pytest.mark.skipif(not cm.sys.platform.startswith("linux"), reason="real /proc/stat sample")
def test_real_sample_is_entirely_under_lock_bounded_and_measures_chat_turn_cost(board):
    """Frozen contract: an independent FULL real ~1 s /proc/stat sample is taken entirely while admission.lock is held
    (held at its first and last read), the hold stays <= ~2 s, and a chat/notification turn reserving the lock with
    BUSY_WAIT_S=0.5 s during the sample is measured (its outcome must match the measured hold interval)."""
    from hermes_cli import backend_retirement

    conn, home = board
    configure(home, limit=100)  # the real measured value admits unless the host is 100% busy
    advisory(home)
    t = task(conn)
    m = {}
    started = threading.Event()

    def chat_turn():
        started.wait(5)
        time.sleep(0.2)  # health: allow HX013 -- positions the chat turn inside the claim's real ~1 s sample
        m["chat_try"] = time.monotonic()
        try:
            with af.admission_lock(home, timeout=backend_retirement.BUSY_WAIT_S):
                m["chat"] = "acquired"
        except af.AdmissionBusy:
            m["chat"] = "busy"
        m["chat_done"] = time.monotonic()
    real_sample = cm.sample

    def sample(*a, **k):
        m["lock_at_sample_start"] = _lock_state(home)
        m["sample_start"] = time.monotonic()
        started.set()
        out = real_sample(*a, **k)
        m["sample_end"] = time.monotonic()
        m["lock_at_sample_end"] = _lock_state(home)
        m["sample"] = out
        return out
    cm_sample, cm.sample = cm.sample, sample
    try:
        th = threading.Thread(target=chat_turn)
        th.start()
        t0 = time.monotonic()
        got = kb.claim_task(conn, t)
        m["claim_end"] = time.monotonic()
        th.join(5)
    finally:
        cm.sample = cm_sample
    claim_s = m["claim_end"] - t0
    sample_s = m["sample_end"] - m["sample_start"]
    record = {"claim_s": round(claim_s, 4), "sample_s": round(sample_s, 4),
              "lock_at_sample_start": m["lock_at_sample_start"], "lock_at_sample_end": m["lock_at_sample_end"],
              "sample": m["sample"], "chat": m["chat"], "chat_wait_s": round(m["chat_done"] - m["chat_try"], 4),
              "chat_try_offset_s": round(m["chat_try"] - m["sample_start"], 4),
              "busy_wait_s": backend_retirement.BUSY_WAIT_S}
    print("HUB_ADMISSION_LOCK_MEASURE " + json.dumps(record))
    assert got is not None
    # The whole independent sample ran with admission.lock held (first and last /proc/stat read inside the hold).
    assert m["lock_at_sample_start"] == "held" and m["lock_at_sample_end"] == "held"
    assert m["sample"] is not None and cm.MIN_WINDOW_MS <= m["sample"]["cpu_busy_window_ms"] <= cm.MAX_WINDOW_MS
    assert sample_s >= 1.0
    assert claim_s <= 2.5  # frozen bound: one ~1 s sample (window <= 2 s) plus the commit
    # The chat turn's outcome follows from the measured hold: lock held over [sample_start, claim_end].
    wait_end = m["chat_try"] + backend_retirement.BUSY_WAIT_S
    if m["sample_start"] <= m["chat_try"] and wait_end <= m["sample_end"]:
        assert m["chat"] == "busy" and m["chat_done"] - m["chat_try"] >= backend_retirement.BUSY_WAIT_S
    elif m["chat"] == "acquired":
        assert m["chat_done"] >= m["sample_end"]  # only once the claim's hold had ended
    assert m["chat_done"] - m["chat_try"] <= 2.5


def test_queue_reason_codes_cover_the_eight_overflow_reasons():
    assert {"workers_unknown", "no_eligible_worker", "workers_saturated", "hub_cpu_at_or_above_limit",
            "hub_cpu_unknown", "hub_capacity_full", "hub_sample_already_used", "hub_overflow_reserved"} <= queue.CODES


@pytest.mark.parametrize("review", [False, True])
def test_full_cap_refuses_before_the_sample_so_the_lock_is_not_held_for_it(board, monkeypatch, review):
    """Review F6: the cheap whole-host cap check runs first; a claim at the cap never holds admission.lock ~1 s."""
    conn, home = board
    configure(home, cap=2)
    advisory(home)
    monkeypatch.setattr(mm, "active_card_runs", lambda: 2)
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("at the cap: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")


def test_deadline_already_passed_refuses_before_the_sample(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home, age_ms=-10 ** 10)  # fresh against the shifted clock below, so the advisory admits
    real_time = time.time
    monkeypatch.setattr(sync, "claim_gate", lambda conn, task_id, expires, lane="ready": sync.ClaimGate(expires))
    monkeypatch.setattr(ka, "_now", lambda: real_time() + 10 ** 7)
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("deadline passed: refuse before sampling"))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_unknown")
