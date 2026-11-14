"""Single-flight memo fills in ``profile_roster_cache`` (concurrent cold ``profiles.list`` calls).

The serve runs ``profiles.list`` on an 8-thread pool, and Desktop asks for it several times at connect. Before
the fix every concurrent caller recomputed every profile's fields because a memo only fills on completion:
6 concurrent cold calls cost ~4 s of CPU on the hub against ~0.3 s for the same 6 calls in sequence. A caller
whose signature EQUALS an in-flight fill's now waits for that fill; anything else keeps today's behaviour.
"""
import threading

import pytest

import tui_gateway.profile_roster_cache as cache
from tests.waiting import wait_until


@pytest.fixture(autouse=True)
def _clean():
    cache.invalidate()
    yield
    cache.invalidate()


class _Gate:
    """A compute that blocks until released and counts its runs."""

    def __init__(self, value=None):
        self.release = threading.Event()
        self.started = threading.Event()
        self.runs = 0
        self.lock = threading.Lock()
        self.value = value if value is not None else {"last_session": {"id": "s1", "title": "t"}}

    def __call__(self):
        with self.lock:
            self.runs += 1
        self.started.set()
        assert self.release.wait(10)
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in self.value.items()}


def _fill(key, signature, compute, copier=dict):
    return cache._cached(cache._SESSION_CACHE, cache._SESSION_FLIGHTS, key, signature, compute, copier)


def _threads(n, target):
    results, errors = [None] * n, []

    def run(i):
        try:
            results[i] = target()
        except BaseException as exc:  # surfaced through `errors`
            errors.append(exc)
    return [threading.Thread(target=run, args=(i,)) for i in range(n)], results, errors


def _wait_joined(key, n, flights=None):
    """Wait until n callers are parked on the in-flight fill for key."""
    table = cache._SESSION_FLIGHTS if flights is None else flights
    wait_until(lambda: table.get(key) is not None and table[key].waiters >= n, timeout=5,
               what=f"{n} joiners on {key}")


def test_concurrent_equal_signature_callers_compute_once():
    gate = _Gate()
    sig = (("state.db", 1, 2),)
    threads, results, errors = _threads(6, lambda: _fill("/p", sig, gate))
    threads[0].start()
    assert gate.started.wait(5)
    for t in threads[1:]:
        t.start()
    _wait_joined("/p", 5)
    gate.release.set()
    for t in threads:
        t.join(10)
    assert not errors
    assert gate.runs == 1
    assert all(r == results[0] for r in results)
    # Joiners get independent copies of the stored value, never the memo's own dict.
    results[1]["extra"] = 1
    assert "extra" not in cache._SESSION_CACHE["/p"][1]
    assert "extra" not in results[2]


def test_public_session_fields_concurrent_cold_callers_compute_once(tmp_path, monkeypatch):
    """Public-API twin of the test above (behavioural RED on the base: every caller computed)."""
    monkeypatch.setattr(cache, "mtime_is_racy", lambda mtime_ns: False)
    (tmp_path / "state.db").write_bytes(b"x")
    gate = _Gate()
    threads, results, errors = _threads(4, lambda: cache.cached_session_fields(tmp_path, gate))
    threads[0].start()
    assert gate.started.wait(5)
    for t in threads[1:]:
        t.start()
    # Base has no in-flight table: give its callers time to start computing instead of joining.
    wait_until(lambda: gate.runs > 1 or (getattr(cache, "_SESSION_FLIGHTS", {}).get(str(tmp_path)) is not None
                                         and cache._SESSION_FLIGHTS[str(tmp_path)].waiters >= 3),
               timeout=5, what="joiners parked (fix) or duplicate computes (base)")
    gate.release.set()
    for t in threads:
        t.join(10)
    assert not errors
    assert gate.runs == 1, f"{gate.runs} computes for one unchanged store"
    assert all(r == results[0] for r in results)


def test_moved_input_does_not_join_a_stale_fill():
    """Input next pass: a caller whose signature differs computes its own value and the memo ends on it."""
    old = _Gate({"v": "old"})
    new_runs = []
    t = threading.Thread(target=lambda: _fill("/p", ("A",), old))
    t.start()
    assert old.started.wait(5)
    got = _fill("/p", ("B",), lambda: new_runs.append(1) or {"v": "new"})
    assert got == {"v": "new"} and new_runs == [1]
    old.release.set()
    t.join(10)
    # The superseded fill (A) finishing later must not replace B's memo.
    assert _fill("/p", ("B",), lambda: pytest.fail("B must hit")) == {"v": "new"}


def test_no_signature_never_coalesces():
    gate = _Gate()
    threads, _results, errors = _threads(4, lambda: _fill("/p", None, gate))
    for t in threads:
        t.start()
    wait_until(lambda: gate.runs == 4, timeout=5, what="4 independent computes")
    gate.release.set()
    for t in threads:
        t.join(10)
    assert not errors and gate.runs == 4
    assert "/p" not in cache._SESSION_CACHE


def test_leader_exception_releases_joiners_to_compute_themselves():
    started, release = threading.Event(), threading.Event()
    joiner_runs, leader_errors, joined = [], [], []

    def boom():
        started.set()
        assert release.wait(10)
        raise RuntimeError("leader failed")

    def lead():
        try:
            _fill("/p", ("A",), boom)
        except RuntimeError as exc:
            leader_errors.append(exc)

    lt = threading.Thread(target=lead)
    lt.start()
    assert started.wait(5)
    jt = threading.Thread(target=lambda: joined.append(
        _fill("/p", ("A",), lambda: joiner_runs.append(1) or {"v": 1})))
    jt.start()
    _wait_joined("/p", 1)
    release.set()
    lt.join(10)
    jt.join(10)
    assert len(leader_errors) == 1
    assert not jt.is_alive() and joined == [{"v": 1}] and joiner_runs == [1]
    assert not cache._SESSION_FLIGHTS


def test_reentrant_fill_of_the_same_key_does_not_deadlock():
    inner = []

    def outer():
        inner.append(_fill("/p", ("A",), lambda: {"inner": True}))
        return {"outer": True}

    out = []
    t = threading.Thread(target=lambda: out.append(_fill("/p", ("A",), outer)))
    t.daemon = True
    t.start()
    t.join(5)
    assert not t.is_alive(), "same-thread re-entry waited on its own fill"
    assert inner == [{"inner": True}] and out == [{"outer": True}]


def test_invalidate_during_a_fill_is_not_joined_or_stored():
    old = _Gate({"v": "before"})
    t = threading.Thread(target=lambda: _fill("/p", ("A",), old))
    t.start()
    assert old.started.wait(5)
    cache.invalidate("/p")
    assert _fill("/p", ("A",), lambda: {"v": "after"}) == {"v": "after"}
    old.release.set()
    t.join(10)
    assert _fill("/p", ("A",), lambda: pytest.fail("must hit")) == {"v": "after"}


def test_late_caller_after_fill_hits_the_memo():
    gate = _Gate()
    gate.release.set()
    _fill("/p", ("A",), gate)
    assert _fill("/p", ("A",), gate) == gate.value
    assert gate.runs == 1 and not cache._SESSION_FLIGHTS


def test_distinct_readers_are_not_coalesced(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "mtime_is_racy", lambda mtime_ns: False)
    (tmp_path / "state.db").write_bytes(b"x")
    gate = _Gate({"v": "reader-a"})
    t = threading.Thread(target=lambda: cache.cached_reader_session_fields(tmp_path, ("a", False), gate))
    t.start()
    assert gate.started.wait(5)
    got = cache.cached_reader_session_fields(tmp_path, ("b", True), lambda: {"v": "reader-b"})
    assert got == {"v": "reader-b"}
    gate.release.set()
    t.join(10)


def test_ui_meta_joiners_get_independent_deep_copies(tmp_path):
    (tmp_path / "profile.yaml").write_text("x: 1\n")
    gate = _Gate({"ui_meta": {"nested": {"k": 1}}})
    threads, results, errors = _threads(3, lambda: cache.cached_ui_meta_fields(tmp_path, gate))
    threads[0].start()
    assert gate.started.wait(5)
    for th in threads[1:]:
        th.start()
    _wait_joined(str(tmp_path), 2, cache._UI_META_FLIGHTS)
    gate.release.set()
    for th in threads:
        th.join(10)
    assert not errors and gate.runs == 1
    results[1]["ui_meta"]["nested"]["k"] = 99
    assert results[2]["ui_meta"]["nested"]["k"] == 1
    assert cache._UI_META_CACHE[str(tmp_path)][1]["ui_meta"]["nested"]["k"] == 1


def test_racy_store_caches_once_only_the_clock_passes(tmp_path, monkeypatch):
    """Due-time-only progress (regression guard, passes on base too): a store written inside the racy window
    is not memoised; with NO file change, moving only the clock past the window lets the next call cache."""
    db = tmp_path / "state.db"
    db.write_bytes(b"x")
    mtime = db.stat().st_mtime_ns
    now = [mtime + 1]
    monkeypatch.setattr(cache, "mtime_is_racy", lambda m: now[0] - m < 2_000_000_000)
    runs = []

    def compute():
        runs.append(1)
        return {"v": len(runs)}
    cache.cached_session_fields(tmp_path, compute)
    cache.cached_session_fields(tmp_path, compute)
    assert len(runs) == 2  # racy: never memoised
    now[0] = mtime + 2_000_000_001  # only the clock moved
    cache.cached_session_fields(tmp_path, compute)
    cache.cached_session_fields(tmp_path, compute)
    assert len(runs) == 3
