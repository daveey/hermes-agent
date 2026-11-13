"""A pull that only applied feed rows to this gateway's own cards waits for the repair window too.

MacBook gateway, 2026-11-12 (t_01a1cfcad9a0eafa, parent t_01a1a80b7d6cc910): the sync thread used 14-19% of a core
with no gateway work. On a primary board the pull applies other gateways' writes to this gateway's own cards
(worker heartbeats, claims) into the live file. Those applies fire the ``kanban_repair_*`` triggers, so the live
repair revision moved and the full repair pass ran on every pulling round (~465 CPU-ms each). A feed apply is not
the uncaptured local write those scans exist to find: each apply now reports the revision it found and left
(inside its own transaction), and a round whose live revision change those applies account for without a gap
defers like a replica-cursor move. Any gap, a missing pair or an unknown revision still repairs at once. [daveey]
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"
HEAVY = ("repair_partition_links", "repair_partition_chat_links", "repair_divergence", "repair_missing_children",
         "retire_lost_placeholder_links")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(ksc, "_repair_clock", lambda: now[0])
    return now


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    ksc.reset_board_watch()
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        ksc.reset_board_watch()
        conn.close()


class _Paged:
    """The fake hub, one feed change per page (so one pull makes several applies)."""

    def __init__(self, hub):
        self._hub = hub

    def __getattr__(self, name):
        return getattr(self._hub, name)

    def changes(self, board, since, limit=500):
        return self._hub.changes(board, since, 1)


def _round(kanban_home, svc, *, paged=False):
    hub = svc.hub(ME)
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", _Paged(hub) if paged else hub, ME)


def _spy(monkeypatch, names=HEAVY):
    import hermes_cli.kanban_sync_partition_repair as part
    calls = {n: 0 for n in names}
    for name in names:
        owner = part if hasattr(part, name) and not hasattr(ksc, name) else ksc
        real = getattr(owner, name)

        def wrapped(*a, _real=real, _name=name, **k):
            calls[_name] += 1
            return _real(*a, **k)
        monkeypatch.setattr(owner, name, wrapped)
    return calls


_SEQ = [0]


def _remote(svc, uid=None, fields=None):
    """A write by another gateway: to one of our cards (``uid``) it is applied to the live file by our pull."""
    _SEQ[0] += 1
    uid = uid or f"t_foreign{_SEQ[0]}"
    res = svc.hub(OTHER).ops(BOARD, [{
        "client_op_id": f"o-{_SEQ[0]}", "op": "upsert", "table": "tasks", "uid": uid,
        "hlc": f"{time.time_ns() // 1_000_000 + 1000 + _SEQ[0]:013d}-0000-{OTHER}",
        "fields": fields or {"title": "remote", "status": "ready", "created_at": 1}}])
    assert res["results"][0]["status"] in ("applied", "merged"), res
    return uid


def _heartbeat(svc, uid):
    """A heartbeat-style remote write to our own card (one field, a new value each time)."""
    _SEQ[0] += 1
    return _remote(svc, uid=uid, fields={"priority": _SEQ[0] % 50})


def _uncaptured_edit(conn, task_id, title):
    """A local write the capture triggers miss: only the repair pass can find it. The ``applying`` flag silences
    capture without DDL (dropping triggers would change the schema version, a repair input of its own); the
    ``kanban_repair_*`` triggers still move the revision."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, task_id))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def _revision(conn):
    return conn.execute("SELECT revision FROM kanban_repair_revision WHERE id = 1").fetchone()[0]


def _settle(live, svc, kanban_home):
    """A primary board with two linked own cards, synced until a round runs no pass (the last pass is fresh)."""
    sync.set_mode(live, "primary")
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    kb.link_tasks(live, parent, child)
    for _ in range(4):
        _round(kanban_home, svc)
    return parent, child


def _board_title(svc, uid):
    return svc.board(BOARD)["rows"]["tasks"][uid]["title"]


# --- the change (RED on the parent) ---------------------------------------------------------------------


def test_a_remote_write_to_an_own_card_defers_the_full_pass(live, svc, kanban_home, monkeypatch, clock):
    parent, _child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    before = _revision(live)
    _heartbeat(svc, parent)
    result = _round(kanban_home, svc)
    assert _revision(live) != before, "the fixture must move the live revision through the feed apply"
    assert result.repair_deferred, "a feed apply to an own card re-ran the full repair pass"
    assert not any(calls.values()), calls
    path = kanban_home / "kanban.db"
    boards = [(BOARD, path)]
    # The deferred pass still runs when due, with nothing else arriving, and finds nothing lost.
    assert ksc.deferred_repairs_due(boards, clock[0]) == []
    clock[0] += ksc.REPLICA_REPAIR_MIN_S + 1
    assert ksc.deferred_repairs_due(boards, clock[0]) == boards
    due = _round(kanban_home, svc)
    assert not due.repair_deferred
    assert all(calls[n] == 1 for n in ("repair_divergence", "repair_missing_children")), calls
    assert not any(due.repaired.values()), f"the deferred pass found a lost write: {due.repaired}"
    assert str(path) not in ksc._REPAIR_CREDIT, "a completed pass must clear the credit"


def test_two_apply_only_rounds_chain_and_a_local_write_breaks_the_chain(live, svc, kanban_home, monkeypatch, clock):
    parent, child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    for uid in (parent, child):
        clock[0] += 1
        _heartbeat(svc, uid)
        assert _round(kanban_home, svc).repair_deferred
    assert not any(calls.values()), calls
    clock[0] += 1
    _heartbeat(svc, parent)
    _uncaptured_edit(live, child, "third-round gap edit")
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls
    _round(kanban_home, svc)
    assert _board_title(svc, child) == "third-round gap edit"


def test_a_cursor_only_apply_does_not_break_the_chain(live, svc, kanban_home, monkeypatch, clock):
    parent, _child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred
    seen = []
    real = sync.apply_remote_changes

    def spy_apply(conn, changes, **kw):
        out = real(conn, changes, **kw)
        seen.append((len(changes), out.revision_before, out.revision_after))
        return out
    monkeypatch.setattr(sync, "apply_remote_changes", spy_apply)
    _remote(svc)  # another gateway's card: our live pull filters it out and only moves the cursor
    clock[0] += 1
    assert _round(kanban_home, svc).repair_deferred
    assert any(n == 0 and b == a is not None for n, b, a in seen), seen
    _heartbeat(svc, parent)
    clock[0] += 1
    assert _round(kanban_home, svc).repair_deferred
    assert not any(calls.values()), calls


def test_a_full_pass_clears_the_credit(live, svc, kanban_home, monkeypatch, clock):
    """The credit is for one window: after a completed pass the chain starts at that pass's revision again."""
    parent, child = _settle(live, svc, kanban_home)
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred
    path = str(kanban_home / "kanban.db")
    assert path in ksc._REPAIR_CREDIT
    # A captured local write: the next round runs the pass and clears the credit.
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = 'local' WHERE id = ?", (child,))
    clock[0] += 1
    assert not _round(kanban_home, svc).repair_deferred
    assert path not in ksc._REPAIR_CREDIT
    # The push after that pass moved the live file again; its ack is credited, so the chain starts at the
    # pass's own revision and the next heartbeat round defers.
    calls = _spy(monkeypatch)
    clock[0] += 1
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred, "a stale credit from before the pass broke the next chain"
    assert not any(calls.values()), calls


def test_a_push_ack_after_the_inputs_read_is_credited(live, svc, kanban_home, monkeypatch, clock):
    """``push`` runs after the inputs read; an acknowledged push deletes outbox rows (a watched table). That is
    the sync thread's own bookkeeping (``kanban_sync_own_writes``), so the next apply-only round still defers;
    before t_01a1d02bf3e0d019 it broke the chain and re-ran the full pass on the hub almost every round."""
    parent, child = _settle(live, svc, kanban_home)
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred  # credit held
    calls = _spy(monkeypatch)
    real_push = ksc.push
    marks = []

    def push_then_note(live_conn, *a, **k):
        before = _revision(live_conn)
        out = real_push(live_conn, *a, **k)
        marks.append((before, _revision(live_conn), out.sent))
        return out
    monkeypatch.setattr(ksc, "push", push_then_note)
    # A captured local write: this round repairs (a gap) and pushes it; the ack moves the revision after the read.
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = 'pushed' WHERE id = ?", (child,))
    clock[0] += 1
    assert not _round(kanban_home, svc).repair_deferred
    # The round pushes before its pulls (t_01a1d36ddcea59fe) and again after them: whichever sent it must ack.
    assert any(sent >= 1 and after != before for before, after, sent in marks), \
        f"the push must ack and move the revision: {marks}"
    assert calls["repair_divergence"] == 1, calls
    # An apply-only round after the pushing round: the ack is credited, so it defers.
    clock[0] += 1
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls
    assert _board_title(svc, child) == "pushed"


@pytest.mark.parametrize("heal", ["heal_clobbered_completions", "heal_running_over_ended_run"])
def test_a_heal_that_writes_after_the_inputs_read_breaks_the_chain(live, svc, kanban_home, monkeypatch, clock,
                                                                    heal):
    """The heals run after the inputs read, every round; a write they make is a local write for the next round."""
    import hermes_cli.kanban_sync_divergence as divergence
    parent, child = _settle(live, svc, kanban_home)
    owner = ksc if heal == "heal_clobbered_completions" else divergence
    real = getattr(owner, heal)
    fired = []

    def heal_and_write(conn, *a, **k):
        out = real(conn, *a, **k)
        if not fired:
            fired.append(1)
            _uncaptured_edit(conn, child, f"healed by {heal}")
        return out
    monkeypatch.setattr(owner, heal, heal_and_write)
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred  # the heal wrote after this round's read
    calls = _spy(monkeypatch)
    clock[0] += 1
    _heartbeat(svc, parent)
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls
    _round(kanban_home, svc)
    assert _board_title(svc, child) == f"healed by {heal}"


def test_reset_board_watch_forgets_the_credit(live, svc, kanban_home, clock):
    parent, _child = _settle(live, svc, kanban_home)
    _heartbeat(svc, parent)
    assert _round(kanban_home, svc).repair_deferred
    assert ksc._REPAIR_CREDIT
    ksc.reset_board_watch()
    assert not ksc._REPAIR_CREDIT


# --- the safety net (also hold on the parent) ------------------------------------------------------------


def test_an_uncaptured_write_before_the_pull_repairs_in_that_round(live, svc, kanban_home, monkeypatch, clock):
    parent, child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    _heartbeat(svc, parent)
    _uncaptured_edit(live, child, "gap before pull")
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1 and calls["repair_missing_children"] == 1, calls
    _round(kanban_home, svc)
    assert _board_title(svc, child) == "gap before pull"


def test_an_uncaptured_write_between_two_apply_pages_repairs_in_that_round(live, svc, kanban_home, monkeypatch,
                                                                            clock):
    parent, child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    _heartbeat(svc, parent)
    _heartbeat(svc, child)
    real = sync.apply_remote_changes
    live_file = str(kanban_home / "kanban.db")
    applied = []

    def apply_then_write(conn, changes, **kw):
        out = real(conn, changes, **kw)
        if conn.execute("PRAGMA database_list").fetchone()[2] == live_file and changes:
            applied.append(out)
            if len(applied) == 1:
                _uncaptured_edit(live, parent, "gap between pages")  # its page is already applied: kept
        return out
    monkeypatch.setattr(sync, "apply_remote_changes", apply_then_write)
    assert not _round(kanban_home, svc, paged=True).repair_deferred
    assert len(applied) == 2, "the fixture must apply the two own-card changes on two pages"
    assert calls["repair_divergence"] == 1, calls
    monkeypatch.setattr(sync, "apply_remote_changes", real)
    _round(kanban_home, svc)
    assert _board_title(svc, parent) == "gap between pages"


def test_an_uncaptured_write_after_the_last_apply_repairs_in_that_round(live, svc, kanban_home, monkeypatch, clock):
    parent, child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    _heartbeat(svc, parent)
    real = ksc._repair_inputs
    done = []

    def write_then_read(*a, **k):
        if not done:
            done.append(1)
            _uncaptured_edit(live, child, "gap after apply")
        return real(*a, **k)
    monkeypatch.setattr(ksc, "_repair_inputs", write_then_read)
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls
    _round(kanban_home, svc)
    assert _board_title(svc, child) == "gap after apply"


def test_an_unrecorded_apply_gets_no_credit(live, svc, kanban_home, monkeypatch, clock):
    """No recorder (``credited`` a no-op, e.g. its TEMP trigger failed to install): the apply's step is a gap."""
    import contextlib
    parent, _child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    monkeypatch.setattr(ksc, "credited", lambda conn: contextlib.nullcontext())
    _heartbeat(svc, parent)
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls


def _drop_revision(conn):
    """A board the repair counter was never installed on (or that lost it): no table, no triggers."""
    with kbc.write_txn(conn):
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' "
                                    "AND name GLOB 'kanban_repair_*'").fetchall():
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DROP TABLE IF EXISTS kanban_repair_revision")


def test_an_apply_on_a_board_without_the_revision_table_reports_none(live, svc, kanban_home):
    sync.set_mode(live, "primary")
    _drop_revision(live)
    out = sync.apply_remote_changes(live, [], new_cursor=0, board=BOARD, observers=False)
    assert (out.revision_before, out.revision_after) == (None, None)


def test_a_missing_revision_table_repairs_at_once(live, svc, kanban_home, monkeypatch, clock):
    parent, _child = _settle(live, svc, kanban_home)
    calls = _spy(monkeypatch)
    _drop_revision(live)
    _heartbeat(svc, parent)
    assert not _round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls


@pytest.mark.parametrize("applies,end,ok", [
    ([], 5, True), ([(5, 5)], 5, True), ([(5, 7), (7, 9)], 9, True),
    ([], 6, False),                 # a local write with no apply
    ([(4, 7)], 7, False),           # a write before the first apply
    ([(5, 7), (8, 9)], 9, False),   # a write between applies
    ([(5, 7)], 8, False),           # a write after the last apply
    ([(5, None)], 5, False), ([(None, 5)], 5, False),
])
def test_applies_chain(applies, end, ok):
    assert ksc.applies_chain(5, applies, end) is ok
