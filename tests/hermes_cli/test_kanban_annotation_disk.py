"""A backend restart serves the previous process's kanban annotation instead of starting cold.

On hermes-hub every dashboard restart left up to 11 session-list workers waiting 5-6 s in ``_annotation_snapshot``
behind the first read, which ran 15-17 s under the Desktop's reconnect burst (t_01a18b5c8b3db19c). [daveey]
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_session_activity as ksa

SCOPE = ("annotation-disk-test", "default")


def _parts(active=None):
    return ksa._identity.freeze_annotation((
        active or {"s1": 2}, {"s1": 1}, {}, {}, {"s1": frozenset({"t_a", "t_b"})}, {"s1": 1}, {}, {},
        {"s1": frozenset({("gw/default/default/t_x", "blocked", False)})}, {"s2": frozenset({"t_c"})}, {"s1": 1}))


def _load(scope=SCOPE):
    loaded = disk.load(scope)
    assert loaded is not None
    return loaded


@pytest.fixture
def copy_on(monkeypatch):
    monkeypatch.setattr(disk, "FORCE", True)
    monkeypatch.setattr(ksa, "_origin_scope", lambda: SCOPE)
    ksa._reset_cache_for_tests()
    yield
    ksa._reset_cache_for_tests()


def test_round_trip_keeps_sets_and_tuples(copy_on):
    parts = _parts()
    assert disk.save(SCOPE, parts)
    loaded, saved_at = _load()
    assert loaded == parts
    assert abs(time.time() - saved_at) < 5


def test_old_other_scope_or_corrupt_copy_is_no_copy(copy_on):
    assert disk.save(SCOPE, _parts())
    assert disk.load(("another", "scope")) is None
    assert disk.load(SCOPE, max_age_s=-1) is None
    disk._path(SCOPE).write_text("{not json", encoding="utf-8")
    assert disk.load(SCOPE) is None
    body = {"version": 1, "scope": list(SCOPE), "saved_at": time.time(), "parts": [{}] * 3}
    disk._path(SCOPE).write_text(json.dumps(body), encoding="utf-8")
    assert disk.load(SCOPE) is None


def test_a_version_1_copy_is_ignored(copy_on):
    """A version 1 copy may hold lists and dicts bare (before PR #2200) or wrapped as ``{"l"}``/``{"d"}``: the
    bumped version makes each core ignore the other's encoding instead of seeding wrappers as data (review nit 3)."""
    parts = _parts()
    assert disk.save(SCOPE, parts) and disk.save_host(SCOPE, "f" * 64, time.time(), parts)
    for path in (disk._path(SCOPE), disk._path(SCOPE).with_suffix(".host.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        expected = disk._HOST_VERSION if path.name.endswith('.host.json') else disk._VERSION
        assert body["version"] == expected != 1
        body["version"] = 1
        path.write_text(json.dumps(body), encoding="utf-8")
    assert disk.load(SCOPE) is None
    assert disk.load_host(SCOPE) is None


def test_off_under_the_test_harness_by_default(monkeypatch, copy_on):
    monkeypatch.setattr(disk, "FORCE", False)
    assert not disk.save(SCOPE, _parts())
    assert disk.load(SCOPE) is None


def test_refresh_writes_the_copy_once_per_change(copy_on, monkeypatch):
    reads = [_parts({"s1": 2}), _parts({"s1": 2}), _parts({"s1": 3})]
    monkeypatch.setattr(ksa, "_read_annotation_parts", lambda: reads.pop(0))
    writes = []
    real = disk.save
    monkeypatch.setattr(disk, "save", lambda scope, parts: writes.append(parts) or real(scope, parts))
    for _ in range(3):
        ksa._annotation_snapshot()
    assert [w[0] for w in writes] == [{"s1": 2}, {"s1": 3}]
    assert _load()[0][0] == {"s1": 3}


def test_cold_poll_during_the_first_read_serves_the_copy_without_waiting(copy_on, monkeypatch):
    """The reconnect burst: a read holds the refresh lock and nothing was served yet in this process."""
    saved = _parts({"s1": 7})
    assert disk.save(SCOPE, saved)
    saved_at = _load()[1]
    started, release = threading.Event(), threading.Event()

    def slow_read():
        started.set()
        release.wait(10)
        return _parts({"s1": 9})

    monkeypatch.setattr(ksa, "_read_annotation_parts", slow_read)
    first = threading.Thread(target=ksa._annotation_snapshot)
    first.start()
    try:
        assert started.wait(5)
        t0 = time.monotonic()
        rows = ksa.annotate_kanban_activity([{"id": "s1"}])
        assert time.monotonic() - t0 < 1.0
        assert rows[0]["kanban_active"] == 7
        assert "kanban_unknown" not in rows[0]
        # Dated by the copy, never as a fresh observation.
        assert rows[0]["kanban_observed_at"] <= saved_at
    finally:
        release.set()
        first.join(5)
    # The process's own read replaces the copy and is dated normally.
    rows = ksa.annotate_kanban_activity([{"id": "s1"}])
    assert rows[0]["kanban_active"] == 9
    assert rows[0]["kanban_observed_at"] > saved_at - ksa._CACHE_TTL_S
    assert disk._seeded == {}


def test_shared_block_keeps_the_copy_date(copy_on, monkeypatch):
    assert disk.save(SCOPE, _parts({"s1": 7}))
    saved_at = _load()[1]
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(ksa, "_read_annotation_parts",
                        lambda: (started.set(), release.wait(10), _parts({"s1": 9}))[2])
    first = threading.Thread(target=ksa._annotation_snapshot)
    first.start()
    try:
        assert started.wait(5)
        with ksa.one_annotation_snapshot():
            a = ksa.annotate_kanban_activity([{"id": "s1"}])[0]
            b = ksa.annotate_kanban_activity([{"id": "s1"}])[0]
        assert a["kanban_active"] == b["kanban_active"] == 7
        assert a["kanban_observed_at"] <= saved_at and b["kanban_observed_at"] <= saved_at
    finally:
        release.set()
        first.join(5)


def test_cold_poll_without_a_copy_still_waits_for_the_read(copy_on, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(ksa, "_read_annotation_parts",
                        lambda: (started.set(), release.wait(10), _parts({"s1": 9}))[2])
    first = threading.Thread(target=ksa._annotation_snapshot)
    first.start()
    got = []
    try:
        assert started.wait(5)
        waiter = threading.Thread(target=lambda: got.append(ksa._annotation_snapshot()))
        waiter.start()
        waiter.join(0.3)
        assert waiter.is_alive() and not got
    finally:
        release.set()
        first.join(5)
    waiter.join(5)
    assert got and got[0][0] == {"s1": 9}


def test_uncontended_cold_read_past_budget_serves_the_copy(copy_on, monkeypatch):
    """A fresh process's only poll (session-idle-check's collector) on an overloaded host: the read overruns the
    budget with nobody else holding the lock. It used to mark every row unknown (t_01a19eee6379cea9)."""
    assert disk.save(SCOPE, _parts({"s1": 7}))
    saved_at = _load()[1]
    release = threading.Event()
    monkeypatch.setattr(ksa, "_REFRESH_WAIT_S", 0.2)
    monkeypatch.setattr(ksa, "_read_annotation_parts", lambda: (release.wait(10), _parts({"s1": 9}))[1])
    try:
        rows = ksa.annotate_kanban_activity([{"id": "s1"}])
        assert "kanban_unknown" not in rows[0]
        assert rows[0]["kanban_active"] == 7
        assert rows[0]["kanban_observed_at"] <= saved_at
    finally:
        release.set()
    assert ksa._annotate_refresh_lock.acquire(timeout=5)
    ksa._annotate_refresh_lock.release()
    rows = ksa.annotate_kanban_activity([{"id": "s1"}])
    assert rows[0]["kanban_active"] == 9


def test_uncontended_cold_read_past_budget_without_a_copy_is_unknown(copy_on, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ksa, "_REFRESH_WAIT_S", 0.2)
    monkeypatch.setattr(ksa, "_read_annotation_parts", lambda: (release.wait(10), _parts({"s1": 9}))[1])
    try:
        rows = ksa.annotate_kanban_activity([{"id": "s1"}])
        assert rows[0]["kanban_unknown"] is True
        assert "kanban_active" not in rows[0]
    finally:
        release.set()
    assert ksa._annotate_refresh_lock.acquire(timeout=5)
    ksa._annotate_refresh_lock.release()
