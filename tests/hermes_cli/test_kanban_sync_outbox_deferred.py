"""Outbox-age sampling skips the schema open when every pending op is Board-deferred. [daveey]

Live Mac (t_01a203e5bc71cc65, run23): the default board held 4 pending ops, all with ``retry_after`` set, so the
"any pending row" probe never proved empty and every sync round opened a fresh connection (schema parse) only to
answer None. The answer is always an unanswered row (``retry_after IS NULL``), so the probe now asks for that.
"""
import pathlib
import threading
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_cpu as sync_cpu
from hermes_cli import kanban_sync_outbox_age as age
from hermes_cli import kanban_sync_replica_age as replica_age
from hermes_cli import loop_accounting
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    client.ensure_identity(conn, "default", "macbook")
    sync.set_mode(conn, "primary")
    conn.execute("DELETE FROM kanban_sync_outbox")
    yield path, conn
    conn.close()
    client.reset_board_watch()


def _insert(conn, op, retry=None, created=1000):
    conn.execute(
        "INSERT INTO kanban_sync_outbox "
        "(op_id, table_name, row_uid, op, fields, hlc, created_at, retry_after) "
        "VALUES (?, 'tasks', ?, 'upsert', '{}', '', ?, ?)", (op, f"t_{op}", created, retry))


def test_only_deferred_pending_ops_skip_the_open(board, monkeypatch):
    path, conn = board
    for i in range(4):
        _insert(conn, f"deferred{i}", retry=100 + i)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    now = [50]
    monkeypatch.setattr(age.time, "time", lambda: now[0])
    assert age._sample(path) is None
    now[0] = 200  # deferrals expired: still not unanswered, still None
    assert age._sample(path) is None
    assert opened.call_count == 0


def test_cleared_deferral_is_sampled_at_once(board, monkeypatch):
    path, conn = board
    monkeypatch.setattr(age, "_since", lambda created: float(created))
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    _insert(conn, "op", retry=100)
    assert age._sample(path) is None
    conn.execute("UPDATE kanban_sync_outbox SET retry_after = NULL")  # the Board's answer reset
    assert age._sample(path) == ("op", 1000.0)


def test_unanswered_op_behind_deferred_ones_is_still_sampled(board, monkeypatch):
    path, conn = board
    monkeypatch.setattr(age, "_since", lambda created: float(created))
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    _insert(conn, "deferred", retry=10**12, created=900)
    _insert(conn, "unanswered", created=1000)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    assert age._sample(path) == ("unanswered", 1000.0)
    assert opened.call_count == 1


def test_after_round_with_only_deferred_ops_clears_the_observation(board, monkeypatch):
    path, conn = board
    _insert(conn, "deferred", retry=10**12)
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    result = client.RoundResult(board="default", mode="primary")
    assert age.after_round([result], [("default", path)], awake_fn=lambda: True) == []
    assert not path.with_suffix(".outbox-age.json").exists()
    assert opened.call_count == 0


@pytest.fixture
def boot_reads(monkeypatch):
    monkeypatch.setattr(replica_age, "_BOOT", [None], raising=False)
    reads = []
    real = pathlib.Path.read_text
    answers = []

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/sys/kernel/random/boot_id":
            reads.append(self)
            if answers and answers[0] is OSError:
                answers.pop(0)
                raise OSError("unreadable")
            return "boot-a\n"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    return reads, answers


def test_boot_id_is_read_once_per_process(boot_reads):
    reads, _answers = boot_reads
    assert replica_age._boot_id() == "boot-a"
    assert replica_age._boot_id() == "boot-a"
    assert len(reads) == 1


def test_failed_boot_id_read_is_not_kept(boot_reads):
    reads, answers = boot_reads
    answers.append(OSError)  # Linux libc has no sysctlbyname, so the macOS fallback reads None too
    assert replica_age._boot_id() is None
    assert replica_age._boot_id() == "boot-a"
    assert replica_age._boot_id() == "boot-a"
    assert len(reads) == 2


def test_after_round_details_are_nonadditive(board, monkeypatch):
    path, conn = board
    _insert(conn, "deferred", retry=10**12)
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    monkeypatch.setattr(sync_cpu, "_local", threading.local())
    real_record = loop_accounting.record
    details = []

    def record(name, **kwargs):
        if name.startswith("sync.detail/after_round."):
            details.append((name, kwargs.get("outer", True)))
        return real_record(name, **kwargs)

    monkeypatch.setattr(loop_accounting, "record", record)
    loop_accounting.reset()
    try:
        result = client.RoundResult(board="default", mode="primary")
        assert sync_cpu.step(age.after_round, [result], [("default", path)], awake_fn=lambda: True) == []
        # Inclusive parts of the step: published with outer=False, so they add nothing to the thread's
        # accounted CPU, the step or sync.cpu.total/loop (the collector's total-minus-board value).
        assert details == [("sync.detail/after_round.awake", False), ("sync.detail/after_round.sample", False)]
        stats = loop_accounting.counters()
        assert stats["sync.detail/after_round.awake"]["runs"] == 1
        assert stats["sync.detail/after_round.sample"]["runs"] == 1
        assert not [k for k in stats if k.startswith(("sync.board/", "sync.cpu."))]
    finally:
        loop_accounting.reset()
