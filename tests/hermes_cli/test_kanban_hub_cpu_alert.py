"""The dispatcher opens one Kanban card per stale-hub-CPU-advisory incident (2026-11-12: zero claims, log line only)."""
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_hub_cpu_alert as alert
from hermes_cli import machine_cpu_metrics as cpu
from hermes_cli import machine_metrics as mm


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import hermes_constants
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: home)
    monkeypatch.setattr(cpu, "root_limit", lambda *a, **k: (cpu.ON, 50.0))
    kb.init_db()
    return home


class World:
    def __init__(self, home: Path):
        self.home, self.mono, self.wall = home, 1000.0, time.time()
        self.state = alert._Incident()

    def sample(self, age_s: float) -> None:
        mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": int((self.wall - age_s) * 1000)}, self.home)

    def tick(self, advance: float = 0.0):
        self.mono += advance
        self.wall += advance
        return alert.check(mono=lambda: self.mono, wall=lambda: self.wall, state=self.state)


def _cards():
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        return conn.execute("SELECT id, status, title, body FROM tasks WHERE idempotency_key = ?",
                            (alert.KEY,)).fetchall()


def _comments(tid):
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,))]


def test_stale_for_sixty_seconds_of_observation_opens_exactly_one_card(home):
    w = World(home)
    w.sample(age_s=300)  # the old gateway's sample: stale from the first tick on
    assert w.tick() is None  # first observation starts the clock; a fresh process never files for its downtime
    assert w.tick(alert.STALE_FOR_S - 1) is None and _cards() == []
    tid = w.tick(1)
    assert tid is not None
    (cid, status, title, body), = _cards()
    assert cid == tid and status not in ("done", "archived") and "hub_cpu_unknown" in title
    assert "s old" in body and "60 s of dispatcher observation" in body
    for _ in range(5):  # the same incident never files again
        assert w.tick(30) is None
    assert len(_cards()) == 1


def test_missing_advisory_file_counts_as_stale(home):
    w = World(home)
    w.tick()
    tid = w.tick(alert.STALE_FOR_S)
    assert tid and "missing or unreadable" in _cards()[0][3]


def test_a_fresh_sample_ends_the_incident_and_resets_the_clock(home):
    w = World(home)
    w.sample(age_s=300)
    w.tick()
    w.tick(alert.STALE_FOR_S - 5)
    w.sample(age_s=1)  # the sampler recovered before the threshold
    assert w.tick(1) is None and w.state.stale_since is None
    w.sample(age_s=300)
    assert w.tick(1) is None and w.tick(alert.STALE_FOR_S - 1) is None  # a new incident needs its own 60 s
    assert _cards() == []


def test_fresh_within_fifteen_seconds_never_files(home):
    w = World(home)
    for _ in range(20):
        w.mono += 10
        w.wall += 10
        w.sample(age_s=14)
        assert w.tick() is None
    assert _cards() == []


@pytest.mark.parametrize("gate", [(cpu.OFF, None), (cpu.UNKNOWN, None)])
def test_gate_not_on_never_files(home, monkeypatch, gate):
    monkeypatch.setattr(cpu, "root_limit", lambda *a, **k: gate)
    w = World(home)
    w.tick()
    assert w.tick(alert.STALE_FOR_S * 10) is None and _cards() == []


def test_next_incident_comments_on_the_open_card_or_opens_a_new_one_once_it_is_done(home):
    w = World(home)
    w.tick()
    first = w.tick(alert.STALE_FOR_S)
    w.sample(age_s=0)
    w.tick(1)  # recovered
    w.sample(age_s=300)
    w.tick(1)
    assert w.tick(alert.STALE_FOR_S) == first  # still open: same card
    assert len(_cards()) == 1
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
    w.sample(age_s=0)
    w.tick(1)  # recovered
    assert w.tick(16) is None  # stale again: the clock starts
    second = w.tick(alert.STALE_FOR_S)
    assert second and second != first
    ids = {row[0]: row[1] for row in _cards()}
    assert ids[first] == "done" and ids[second] != "done"
    w.sample(age_s=0)
    w.tick(1)
    w.tick(16)
    assert w.tick(alert.STALE_FOR_S) == second  # newest wins: later incidents land on the open card


def test_comment_on_a_reopened_incident_is_rate_limited_hourly(home):
    with kbc.connect_closing(kb.kanban_home() / "kanban.db") as conn:
        tid = alert._report(conn, "first")
        assert alert._report(conn, "second") == tid
        assert alert._report(conn, "third") == tid
    assert _comments(tid) == ["Again: second"]


def test_check_never_raises(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("controlled")
    monkeypatch.setattr(cpu, "root_limit", boom)
    assert alert.check() is None
    monkeypatch.setattr(cpu, "root_limit", lambda *a, **k: (cpu.ON, 50.0))
    w = World(home)
    w.tick()
    assert alert.check(mono=lambda: w.mono + 999, wall=lambda: w.wall, state=w.state,
                       file_fn=lambda d: boom()) is None
