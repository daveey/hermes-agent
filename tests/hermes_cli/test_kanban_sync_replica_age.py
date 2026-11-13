"""The replica-freshness alarm (t_01a1d3df5e5d0757): a synced board whose replica stays behind the feed head, or
whose sync loop stalls, fails or stops, files ONE card per incident in awake (monotonic) time.

Every test drives fake clocks: ``mono`` (awake time; it does not move while a Mac sleeps) and ``wall``. The loop's
round outcomes are fed through ``outbox_age.after_round`` exactly as ``run_forever`` does; the replica's
``pulled_at`` stamp is written into the real ``sync-shadow`` copy, as ``_pulled_replica`` stamps it at the feed head.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_outbox_age as outbox_age
from hermes_cli import kanban_sync_replica_age as replica_age
from hermes_cli import kanban_sync_runstate as runstate
from tests.hermes_cli import kanban_sync_support as support

ME, BOARD = "macbook", "default"
PT = ZoneInfo("America/Los_Angeles")
MIN = 60.0


def _pt(text: str) -> float:
    """``2026-11-12 17:27:00`` Pacific -> epoch seconds."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PT).timestamp()


@pytest.fixture(autouse=True)
def _fresh():
    outbox_age.reset()
    yield
    outbox_age.reset()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setattr(replica_age, "_core_sha", lambda: "2cc7101849aa")
    return home


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    c = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(c, BOARD, ME)
    sync.set_mode(c, "primary")
    try:
        yield c
    finally:
        c.close()


def _stamp(at: float) -> None:
    """The replica reached the feed head at ``at`` (``kanban_db_sync.mark_pulled``)."""
    replica = ksc.open_shadow(BOARD, ME)
    try:
        sync.mark_pulled(replica, at)
    finally:
        replica.close()


class Host:
    """One gateway's clocks, its sync loop's rounds and its dispatcher's checks (every ``CHECK_EVERY_S``)."""

    def __init__(self, kanban_home, wall: float, *, file_fn=None, board_status=None):
        self.boards = [(BOARD, kanban_home / "kanban.db")]
        self.mono, self.wall = 1000.0, wall
        self.cards: list[tuple[float, str, str]] = []  # (wall, detail, body) per card filed
        self.file_fn = file_fn or self._file
        self.board_status = board_status or (lambda board, ids: {})

    def _file(self, slug, detail, body):
        self.cards.append((self.wall, detail, body))
        return f"t_card{len(self.cards)}"

    def check(self):
        return replica_age.check(self.boards, now=self.mono, wall=lambda: self.wall, file_fn=self.file_fn,
                                 board_status=self.board_status)

    def round(self, *, behind=False, error=None, head=False):
        """One completed sync round; ``head``: it reached the feed head (stamps the replica)."""
        if head:
            _stamp(self.wall)
        outbox_age.after_round([ksc.RoundResult(board=BOARD, mode="primary", behind=behind, error=error)],
                               self.boards, mono=lambda: self.mono, age=lambda _p: None)
        replica_age.note_round([ksc.RoundResult(board=BOARD, mode="primary", behind=behind, error=error)],
                               mono=lambda: self.mono, wall=lambda: self.wall)

    def run(self, seconds: float, *, every: float = MIN, each=None):
        """Advance awake time ``seconds``, running ``each()`` (a round) and a check once per ``every``."""
        end = self.mono + seconds
        while self.mono + every <= end + 1e-9:
            self.mono += every
            self.wall += every
            if each:
                each()
            self.check()

    def sleep(self, seconds: float):
        self.wall += seconds  # monotonic time stands still while the machine sleeps


# --- the four ways a replica stays stale --------------------------------------------------------------------------

def test_rounds_ending_behind_for_21_awake_minutes_file_exactly_one_card(live, kanban_home):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    _stamp(host.wall)
    host.check()
    host.run(19 * MIN, each=lambda: host.round(behind=True))
    assert host.cards == []  # under the 20-minute bound
    host.run(2 * MIN, each=lambda: host.round(behind=True))
    assert len(host.cards) == 1
    detail = host.cards[0][1]
    assert "behind" in detail and "18:00:00 PST" in detail and "default" in detail
    host.run(60 * MIN, each=lambda: host.round(behind=True))
    assert len(host.cards) == 1  # the same incident files once


def test_rounds_failing_with_urlerror_file_one_card_naming_the_last_error(live, kanban_home):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    _stamp(host.wall)
    host.check()
    host.run(21 * MIN, each=lambda: host.round(error="URLError: <urlopen error [Errno 51] Network is unreachable>"))
    assert len(host.cards) == 1
    assert "URLError: <urlopen error [Errno 51] Network is unreachable>" in host.cards[0][1]
    assert "failed" in host.cards[0][1]


def test_a_loop_that_runs_no_rounds_files_one_card(live, kanban_home):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    _stamp(host.wall)
    host.round(head=True)
    host.check()
    host.run(21 * MIN)  # dead loop: checks only
    assert len(host.cards) == 1
    assert "no sync round since 2026-11-12 18:00:00 PST" in host.cards[0][1]


def test_a_synced_board_without_a_readable_replica_files_one_card(live, kanban_home):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    assert not ksc.shadow_path(BOARD).exists()
    host.check()
    host.run(21 * MIN, each=lambda: host.round(error="HubError: 503 Service Unavailable"))
    assert len(host.cards) == 1
    assert "no readable replica" in host.cards[0][1]


def test_an_unreadable_replica_counts_as_missing(live, kanban_home):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    path = ksc.shadow_path(BOARD)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database" * 100)
    host.check()
    host.run(21 * MIN)
    assert len(host.cards) == 1 and "no readable replica" in host.cards[0][1]


# --- sleep, recovery, scope -----------------------------------------------------------------------------------------

def test_sleep_never_counts(live, kanban_home):
    """The wall clock jumps 5 h while awake time moves 1 minute, the replica behind the whole time."""
    host = Host(kanban_home, _pt("2026-11-12 08:00:00"))
    _stamp(host.wall)
    host.check()
    host.sleep(5 * 3600)
    host.run(1 * MIN, each=lambda: host.round(behind=True))
    assert host.cards == []


def test_a_wall_clock_step_backwards_is_still_an_advance(live, kanban_home):
    """The wall clock steps back 30 min (an NTP or manual fix): healthy rounds stamp T-29m, T-28m, ..., each one
    below the remembered T for all 21 awake minutes.

    ``pulled_at`` is wall time, so any change of the stamp is a pull that reached the feed head."""
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    stamp_at = host.wall
    _stamp(stamp_at)
    host.check()
    host.wall -= 30 * MIN
    host.run(21 * MIN, each=lambda: host.round(head=True))
    last = replica_age._stamp(BOARD)
    assert last is not None and last < stamp_at  # every stamp the rounds wrote stayed behind T
    assert host.cards == []


def test_the_stamp_advancing_ends_the_incident_then_the_next_comments_or_opens_a_new_card(live, kanban_home, caplog):
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"), file_fn=replica_age.report)
    _stamp(host.wall)
    host.check()
    host.run(21 * MIN, each=lambda: host.round(behind=True))
    first = live.execute("SELECT id, title, idempotency_key FROM tasks WHERE created_by = ?",
                         (replica_age.AUTHOR,)).fetchall()
    assert len(first) == 1 and first[0][2] == f"{replica_age.KEY}:{BOARD}"
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_sync_replica_age"):
        host.run(1 * MIN, each=lambda: host.round(head=True))
    assert [r for r in caplog.records if r.levelno == logging.INFO and "reached the feed head" in r.getMessage()]
    # A second incident while that card is open: a comment on it, no new card.
    host.run(21 * MIN, each=lambda: host.round(behind=True))
    rows = live.execute("SELECT id FROM tasks WHERE created_by = ?", (replica_age.AUTHOR,)).fetchall()
    assert [r[0] for r in rows] == [first[0][0]]
    comments = live.execute("SELECT body FROM task_comments WHERE task_id = ? AND author = ?",
                            (first[0][0], replica_age.AUTHOR)).fetchall()
    assert len(comments) == 1 and comments[0][0].startswith("Again:")
    # A third incident after that card is done: a new card, which the key now names.
    host.run(1 * MIN, each=lambda: host.round(head=True))
    kb.complete_task(live, first[0][0], summary="fixed")
    host.run(21 * MIN, each=lambda: host.round(behind=True))
    rows = live.execute("SELECT id, idempotency_key FROM tasks WHERE created_by = ? ORDER BY created_at, rowid",
                        (replica_age.AUTHOR,)).fetchall()
    assert len(rows) == 2 and rows[1][1] == f"{replica_age.KEY}:{BOARD}"


def test_a_detached_board_never_files(live, kanban_home):
    sync.set_mode(live, "off")
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    _stamp(host.wall - 3600)
    host.run(90 * MIN)
    assert host.cards == []


def test_an_unsynced_board_never_files(kanban_home):
    support.write_sync_config(kanban_home, False)
    c = kbc.connect(kanban_home / "kanban.db")
    c.close()
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    host.run(90 * MIN)
    assert host.cards == []
    # and through the dispatcher's entry point, which reads the host's board list itself
    assert outbox_age.check(mono=lambda: 1e6) == []


def test_an_exception_in_the_check_never_breaks_the_dispatcher_tick(live, kanban_home, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("replica read exploded")
    monkeypatch.setattr(replica_age, "_stamp", boom)
    boards = [(BOARD, kanban_home / "kanban.db")]
    assert replica_age.check(boards, now=5000.0) == []
    assert outbox_age.check(mono=lambda: 9000.0, boards=boards, age=lambda _p: None) == []


def test_the_dispatcher_tick_runs_the_replica_check(live, kanban_home, monkeypatch):
    seen = []
    monkeypatch.setattr(replica_age, "check", lambda boards, **kw: seen.append((boards, kw["now"])) or ["t_r"])
    boards = [(BOARD, kanban_home / "kanban.db")]
    assert outbox_age.check(mono=lambda: 7000.0, boards=boards, age=lambda _p: None) == ["t_r"]
    assert seen == [(boards, 7000.0)]


def test_the_card_names_host_board_stamp_minutes_outcome_outbox_and_status_command(live, kanban_home):
    kb.create_task(live, title="unsent")  # pending outbox ops
    pending = live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0]
    assert pending >= 1
    host = Host(kanban_home, _pt("2026-11-12 18:00:00"))
    _stamp(host.wall)
    host.check()
    host.run(21 * MIN, each=lambda: host.round(behind=True))
    (_at, detail, body), = host.cards
    import socket
    assert socket.gethostname() in detail and ME in detail
    assert "2026-11-12 18:00:00 PST" in detail
    assert "20 awake min" in detail
    assert f"pending outbox ops: {pending}" in detail
    assert "`hermes kanban sync status`" in body
    assert "2cc7101849aa" in detail  # the core this gateway runs


def test_the_card_lists_frozen_children_of_remote_parents_with_their_board_status(live, kanban_home):
    child = kb.create_task(live, title="rework after review")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        live.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_remote_review", child))
    asked = []

    def board_status(board, ids):
        asked.append((board, sorted(ids)))
        return {"t_remote_review": "done"}

    host = Host(kanban_home, _pt("2026-11-12 23:53:44"), board_status=board_status)
    _stamp(host.wall)
    host.check()
    host.run(21 * MIN)
    (_at, _detail, body), = host.cards
    assert asked == [(BOARD, ["t_remote_review"])]
    assert child in body and "t_remote_review" in body and "Board status: done" in body
    assert "stuck-card-recovery.md" in body and "item 3" in body and "t_01a1d42acdce8e23" in body


def test_an_unreachable_board_lists_the_local_mirror_status_instead(live, kanban_home):
    child = kb.create_task(live, title="rework")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        live.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", ("t_remote_review", child))
        live.execute(f"INSERT INTO {sync.REMOTE_PARENTS_TABLE} (parent_id, status, refreshed_at) VALUES (?, ?, 0)",
                     ("t_remote_review", "running"))

    def board_status(board, ids):
        raise OSError("Board unreachable")

    host = Host(kanban_home, _pt("2026-11-12 23:53:44"), board_status=board_status)
    _stamp(host.wall)
    host.check()
    host.run(21 * MIN)
    (_at, _detail, body), = host.cards
    assert child in body and "running (local mirror" in body


# --- replays of the 2026-11-12 incidents ---------------------------------------------------------------------------

def _fired(host):
    return [datetime.fromtimestamp(at, PT).strftime("%H:%M PT") for at, _d, _b in host.cards]


def test_replay_hub_slow_rounds_1112(live, kanban_home, capsys):
    """hermes-hub: rounds about every minute until 17:27 PT, then every 15-28 min; the replica's stamp stayed at
    18:06:53 PT (02:06:53 UTC) through at least 18:39 PT. One card for the incident."""
    host = Host(kanban_home, _pt("2026-11-12 17:00:00"))
    host.round(head=True)
    host.check()
    host.run(27 * MIN, each=lambda: host.round(head=True))  # healthy until 17:27
    # Slow phase: a round 15-28 min apart; each at-head round stamps, then rounds end behind from 18:06:53.
    plan = [("17:42:30", True), ("17:59:10", True), ("18:06:53", True), ("18:21:40", False), ("18:39:05", False),
            ("19:04:00", False), ("19:20:30", True)]
    for at, head in plan:
        target = _pt(f"2026-11-12 {at}")
        host.run(target - host.wall - (target - host.wall) % MIN)
        step = target - host.wall
        host.mono += step
        host.wall += step
        host.round(head=head, behind=not head)
        host.check()
    print("hub replay fired at:", _fired(host))
    assert len(host.cards) == 1
    assert _fired(host) == ["18:27 PT"]
    assert "18:06:53 PST" in host.cards[0][1]


def test_replay_macbook_75k_backlog_after_sleep_1112(live, kanban_home, capsys):
    """David-MBP: asleep ~10 h, woke ~75k changes behind; rounds ran back to back, each ending behind its page budget,
    until about 22:30 PT. One card, 20 awake minutes after the last time at the head; the 10 h asleep never count."""
    host = Host(kanban_home, _pt("2026-11-12 07:58:00"))
    host.round(head=True)
    host.check()
    host.run(2 * MIN, each=lambda: host.round(head=True))  # 08:00: last round at the head before the lid closes
    host.sleep(10 * 3600)  # 18:00 PT wake
    host.run(4.5 * 3600, each=lambda: host.round(behind=True))  # 22:30
    print("MacBook backlog replay fired at:", _fired(host))
    assert len(host.cards) == 1
    assert _fired(host) == ["18:20 PT"]
    assert "2026-11-12 08:00:00 PST" in host.cards[0][1] and "behind" in host.cards[0][1]
    host.run(1 * MIN, each=lambda: host.round(head=True))
    host.run(3 * 3600, each=lambda: host.round(head=True))
    assert len(host.cards) == 1  # back at the head: no second card


SCHEMA_AHEAD = ("SchemaAhead: this core does not declare columns a newer core added (task_events: origin_gateway; "
                "task_runs: admission_class, admission_origin, admission_pool; tasks: admission_class, "
                "admission_frees, admission_origin, review_required); refusing to sync until this host updates")


def test_replay_macbook_schema_skew_refusal_1112(live, kanban_home, capsys):
    """David-MBP 23:53:44 PT: `kanban sync: default not synced: this core does not declare columns ...`, then the
    loop gave the sync role up and ran no more rounds. One card naming the refusal and the core sha."""
    host = Host(kanban_home, _pt("2026-11-12 23:30:00"))
    host.round(head=True)
    host.check()
    host.run(23 * MIN, each=lambda: host.round(head=True))  # 23:53
    step = _pt("2026-11-12 23:53:44") - host.wall
    host.mono += step
    host.wall += step
    host.round(error=SCHEMA_AHEAD)  # the refusing round
    runstate.record_down("gave up the sync role: board default: " + SCHEMA_AHEAD.split(": ", 1)[1])
    host.run(60 * MIN)  # no rounds at all after the give-up
    print("MacBook schema-skew replay fired at:", _fired(host))
    assert len(host.cards) == 1
    assert _fired(host) == ["00:13 PT"]
    detail = host.cards[0][1]
    assert "refusing to sync" in detail and "origin_gateway" in detail
    assert "gave up the sync role" in detail
    assert "2cc7101849aa" in detail
