"""The session-list annotation is read once per host: other processes adopt the published copy. [daveey]

Design t_01a1d45d75851804 §2c (review item R9): every web-server process and the gateway computed the same parts for
the same scope (``_refresh_annotation_locked`` = 13.3% of a steady hub serve's samples, 10.8% of the gateway's). A
refresh now publishes ``(scope, fingerprint, read_at, parts)``; another process adopts it only on an exact fingerprint
and while it is younger than ``_COUNTS_MAX_AGE_S``. "Another process" is simulated by dropping every in-memory cache
(``_reset_cache_for_tests``): only the files under the test's Hermes home survive, as across processes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_activity_revision as revision
from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa

SCOPE = ("annotation-host-test", "default")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.delenv(host.KILL_SWITCH_ENV, raising=False)
    monkeypatch.setattr(disk, "FORCE", True)
    monkeypatch.setattr(ksa, "_origin_scope", lambda: SCOPE)
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [])
    monkeypatch.setattr(ksa, "_waiting_sources", {})
    monkeypatch.setattr(host, "_declared", {})
    sha = ["a" * 40]
    monkeypatch.setattr(host, "core_sha", lambda: sha[0])
    now = {"mono": 1000.0, "wall": 5000.0, "sha": sha}
    monkeypatch.setattr(ksa.time, "monotonic", lambda: now["mono"])
    monkeypatch.setattr(ksa.time, "time", lambda: now["wall"])
    calls = {"recompute": 0}
    recompute = ksa._recompute_all_counts

    def counting(*a, **k):
        calls["recompute"] += 1
        return recompute(*a, **k)

    monkeypatch.setattr(ksa, "_recompute_all_counts", counting)
    now["calls"] = calls
    conn = _board(tmp_path / "kanban.db", [("t1", "running", "s1"), ("t2", "blocked", "s1"), ("t3", "ready", "s2")])
    now["conn"] = conn
    _sessions(["s1", "s2", "s3"])  # this profile's chats: the holder's narrow key counts only their cards
    ksa._reset_cache_for_tests()
    yield now
    conn.close()
    ksa._reset_cache_for_tests()


def _board(path: Path, rows) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    revision.ensure_revision(conn, revision.LOCAL_TABLES)
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    return conn


def _sessions(ids) -> None:
    """Sessions of this origin profile (its ``state.db``)."""
    from hermes_constants import get_hermes_home

    db = get_hermes_home() / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO sessions (id) VALUES (?)", [(i,) for i in ids])
    conn.close()


def _advance(world, seconds):
    world["mono"] += seconds
    world["wall"] += seconds


def _poll():
    rows = ksa.annotate_kanban_activity([{"id": "s1"}, {"id": "s2"}, {"id": "s3"}])
    return [{k: v for k, v in row.items() if k != "kanban_observed_at"} for row in rows], rows


def _other_process():
    ksa._reset_cache_for_tests()


def test_another_process_adopts_the_copy_on_an_exact_fingerprint(world):
    first, _ = _poll()
    assert world["calls"]["recompute"] == 1
    assert disk.load_host(SCOPE) is not None
    _other_process()
    _advance(world, 2)
    adopted, raw = _poll()
    assert world["calls"]["recompute"] == 1  # no read of its own
    assert adopted == first
    # Dated by the copy's own read, never by the adopting poll.
    assert all(row["kanban_observed_at"] <= world["wall"] - 2 for row in raw)


def test_adopted_rows_equal_a_local_refresh(world, monkeypatch):
    ksa.register_waiting_source("closeout", lambda: {"s3": ["t9"]}, inputs=lambda: ["ledger", 1])
    dispose = ksa.register_done_check("test", lambda card: True)
    try:
        _poll()
        _other_process()
        adopted, _ = _poll()
        assert world["calls"]["recompute"] == 1
        _other_process()
        monkeypatch.setenv(host.KILL_SWITCH_ENV, "0")  # this process reads for itself
        local, _ = _poll()
        assert world["calls"]["recompute"] == 2
        assert adopted == local
        assert local[2]["kanban_waiting_on"] == ["t9"] and local[0]["kanban_active"] >= 1
    finally:
        dispose()


def test_identical_parts_changed_inputs_republish_and_adopt(world):
    world['conn'].execute("DELETE FROM tasks")
    _poll()
    before = disk.load_host(SCOPE)
    _advance(world, 4)
    # Committed inputs move, but the next read has the same empty parts.
    world['conn'].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's4')")
    world['conn'].execute("DELETE FROM tasks WHERE id='t4'")
    with ksa._annotate_refresh_lock:
        ksa._refresh_annotation_locked(SCOPE)
    after = disk.load_host(SCOPE)
    assert before is not None and after is not None
    assert before[0] != after[0] and before[1] < after[1]
    assert before[2] == after[2]
    calls = world['calls']['recompute']
    _other_process()
    assert host.adopt(SCOPE) == after[2]
    assert world['calls']['recompute'] == calls


def test_a_waiting_source_change_defeats_adopt(world):
    """A chat-closeout ledger write moves its declared inputs: the copy's waiting part is another input's."""
    ledger = {"version": 1, "pending": ["t9"]}
    ksa.register_waiting_source("closeout", lambda: {"s3": list(ledger["pending"])},
                                inputs=lambda: ["ledger", ledger["version"]])
    first, _ = _poll()
    assert first[2]["kanban_waiting_on"] == ["t9"]
    ledger.update(version=2, pending=["t10"])
    _other_process()
    after, _ = _poll()
    assert world["calls"]["recompute"] == 2
    assert after[2]["kanban_waiting_on"] == ["t10"]


def test_an_undeclared_waiting_source_is_rerun_by_the_adopter(world):
    pending = ["t9"]
    ksa.register_waiting_source("closeout", lambda: {"s3": list(pending)})
    _poll()
    pending[:] = ["t10"]
    _other_process()
    after, _ = _poll()
    assert world["calls"]["recompute"] == 1  # counts adopted
    assert after[2]["kanban_waiting_on"] == ["t10"]  # the source the fingerprint can't see ran here


def test_a_stale_copy_is_ignored(world):
    _poll()
    _advance(world, ksa._COUNTS_MAX_AGE_S + 1)
    _other_process()
    _poll()
    assert world["calls"]["recompute"] == 2


def test_a_code_sha_change_defeats_adopt(world):
    _poll()
    world["sha"][0] = "b" * 40
    _other_process()
    _poll()
    assert world["calls"]["recompute"] == 2


def test_a_board_write_defeats_adopt(world):
    first, _ = _poll()
    world["conn"].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    _other_process()
    after, _ = _poll()
    assert world["calls"]["recompute"] == 2
    assert after[0]["kanban_active"] == first[0]["kanban_active"] + 1


def test_another_plugins_code_defeats_adopt(world, tmp_path, monkeypatch):
    """A process still running an older plugin build never adopts a newer build's read (or the reverse)."""
    plugin = tmp_path / "plugins" / "closeout"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("def waiting():\n    return {}\n")
    (plugin / "core.py").write_text("X = 1\n")
    code = compile((plugin / "__init__.py").read_text(), str(plugin / "__init__.py"), "exec")
    scope: dict = {}
    exec(code, scope)
    ksa.register_waiting_source("closeout", scope["waiting"], inputs=lambda: 1)
    _poll()
    _other_process()
    (plugin / "core.py").write_text("X = 22\n")  # this "process" loads the updated plugin
    _poll()
    assert world["calls"]["recompute"] == 2


def test_kill_switch_turns_publishing_and_adopting_off(world, monkeypatch):
    monkeypatch.setenv(host.KILL_SWITCH_ENV, "0")
    _poll()
    assert disk.load_host(SCOPE) is None


def test_unportable_inputs_publish_no_fingerprint(world):
    assert host.fingerprint(SCOPE, (("x", ("stat", ((1, 2, 3, 4), None), object())),), 1e9) is None
    full = (("b", ("stat", ((1, 2, 3, int(4000 * 1e9)), None), object(), 7)),)
    assert host.fingerprint(SCOPE, full, 5000.0) is not None  # a settled stat key drops its connection
    assert host.fingerprint(SCOPE, (("hub", sqlite3.connect(":memory:")),), 5000.0) is None


def test_host_copy_round_trips_nested_evidence(world):
    parts = ksa._identity.freeze_annotation((
        {"s1": 2}, {}, {}, {}, {"s1": frozenset({"t1"})}, {}, {}, {},
        {"s1": frozenset({("gw/default/default/t_x", "blocked", False)})}, {}, {},
        {"s1": {"k": {"version": 1, "evidence_refs": ["r1"], "identity": {"run_id": 3}, "pair": (1, 2)}}}))
    assert disk.save_host(SCOPE, "f" * 64, 4990.0, parts)
    assert disk.load_host(SCOPE) == ("f" * 64, 4990.0, parts)


def test_holder_refreshes_a_demanded_scope_once_per_change(world):
    import contextvars

    host._known[SCOPE] = contextvars.copy_context()
    disk.touch_demand(SCOPE)
    assert host._holder_pass() == 1
    assert world["calls"]["recompute"] == 1 and disk.load_host(SCOPE) is not None
    assert host._holder_pass() == 0  # the copy answers the current inputs
    world["conn"].execute("INSERT INTO tasks VALUES ('t4', 'ready', 's1')")
    assert host._holder_pass() == 0  # a foreign write respects the per-scope floor
    _advance(world, 10)
    assert host._holder_pass() == 1
    _other_process()
    after, _ = _poll()
    assert after[0]["kanban_active"] == 3 and world["calls"]["recompute"] == 2  # adopted the holder's read


def test_holder_refreshes_a_scope_demanded_on_another_profile(world, tmp_path, monkeypatch):
    """One serve hosts several profiles: a non-launch profile's demand file lives under that profile's home, so the
    holder reads its age inside the scope's own context (PR #2200 review nit 1)."""
    import contextvars

    import hermes_scope
    from hermes_constants import get_hermes_home_override

    other_home = tmp_path / "profiles" / "other"
    other_home.mkdir(parents=True)
    other = ("annotation-host-test", "other")
    monkeypatch.setattr(ksa, "_origin_scope",
                        lambda: other if get_hermes_home_override() == str(other_home) else SCOPE)
    token = hermes_scope.push_state(home=str(other_home))
    try:
        host._known[other] = contextvars.copy_context()
        disk.touch_demand(other)
        assert disk.demand_age(other) is not None
    finally:
        hermes_scope.pop_state(token)
    assert disk.demand_age(other) is None  # the launch home holds no demand for it
    assert host._holder_pass() == 1
    token = hermes_scope.push_state(home=str(other_home))
    try:
        assert disk.load_host(other) is not None  # published under the other profile's home
    finally:
        hermes_scope.pop_state(token)


def test_holder_renews_the_copy_before_adopters_would_miss(world):
    """Under steady demand the holder publishes a fresh ``read_at`` before the copy reaches ``_COUNTS_MAX_AGE_S``:
    its re-read at ``HOLDER_REFRESH_AGE_S`` really reads instead of taking the still-revalidatable counts cache and
    republishing the old ``read_at`` (PR #2200 review nit 2)."""
    import contextvars

    host._known[SCOPE] = contextvars.copy_context()
    disk.touch_demand(SCOPE)
    assert host._holder_pass() == 1
    ages = []
    for _ in range(int(3 * ksa._COUNTS_MAX_AGE_S / host.HOLDER_INTERVAL_S)):
        _advance(world, host.HOLDER_INTERVAL_S)
        copy = disk.load_host(SCOPE)
        assert copy is not None
        ages.append(world["wall"] - copy[1])  # what an adopter polling now finds
        host._holder_pass()
    assert max(ages) <= host.HOLDER_REFRESH_AGE_S + host.HOLDER_INTERVAL_S < ksa._COUNTS_MAX_AGE_S, ages
    reads = world["calls"]["recompute"]
    _other_process()
    _poll()
    assert world["calls"]["recompute"] == reads  # an adopter at any moment finds a current copy
