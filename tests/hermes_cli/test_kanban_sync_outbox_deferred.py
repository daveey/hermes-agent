"""Outbox-age sampling skips the schema open when every pending op is Board-deferred. [daveey]

Live Mac (t_01a203e5bc71cc65, run23): the default board held 4 pending ops, all with ``retry_after`` set, so the
"any pending row" probe never proved empty and every sync round opened a fresh connection (schema parse) only to
answer None. The answer is always an unanswered row (``retry_after IS NULL``), so the probe now asks for that.
"""
import pathlib
import sys
import threading
import types
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


_PROBE = "hermes_cli.kanban_sync_probe_diag"
_EMPTY_SQL = (
    "SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' "
    "AND retry_after IS NULL LIMIT 1"
)


@pytest.fixture(autouse=True)
def probe_notes(monkeypatch):
    """Record ``probe_diag.note`` even when the parent module is not imported yet."""
    notes = []

    def note(path, empty):
        notes.append((path, empty))

    try:
        probe_diag = sys.modules.get(_PROBE) or __import__(_PROBE, fromlist=["note"])
    except ImportError:
        probe_diag = types.ModuleType(_PROBE)
        monkeypatch.setitem(sys.modules, _PROBE, probe_diag)
    monkeypatch.setattr(probe_diag, "note", note, raising=False)
    return notes


def _spy_details(monkeypatch):
    monkeypatch.setattr(sync_cpu, "_local", threading.local())
    monkeypatch.setattr(loop_accounting, "scopes_enabled", lambda: False)
    loop_accounting.reset()
    real = loop_accounting.record
    details = []

    def record(name, **kwargs):
        if name.startswith("sync.detail/"):
            details.append((name, kwargs.get("outer", True)))
        return real(name, **kwargs)

    monkeypatch.setattr(loop_accounting, "record", record)
    return details


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
    """Script both boot-id reads. A failure is OSError on /proc and a non-zero sysctl.

    The host's real ``kern.bootsessionuuid`` is never consulted: on macOS that read succeeds, so a
    /proc-only failure would cache a live identity instead of the fixture's later answer.
    """
    monkeypatch.setattr(replica_age, "_BOOT", [None], raising=False)
    proc_reads = []
    sysctl_reads = []
    proc_answers = []
    sysctl_answers = []
    real = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/sys/kernel/random/boot_id":
            proc_reads.append(self)
            if proc_answers and proc_answers[0] is OSError:
                proc_answers.pop(0)
                raise OSError("unreadable")
            if proc_answers:
                value = proc_answers.pop(0)
                return value if str(value).endswith("\n") else f"{value}\n"
            return "boot-a\n"
        return real(self, *args, **kwargs)

    class _Libc:
        def sysctlbyname(self, name, buf, size, _newp, _newlen):
            sysctl_reads.append(bytes(name))
            if sysctl_answers and sysctl_answers[0] is OSError:
                sysctl_answers.pop(0)
                raise OSError("sysctl failed")
            answer = sysctl_answers.pop(0) if sysctl_answers else 1
            if isinstance(answer, str):
                buf.value = answer.encode("ascii")
                return 0
            return int(answer)

    import ctypes

    monkeypatch.setattr(pathlib.Path, "read_text", read_text)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: _Libc())
    return proc_reads, proc_answers, sysctl_reads, sysctl_answers


def test_boot_id_is_read_once_per_process(boot_reads):
    proc_reads, _proc_answers, sysctl_reads, _sysctl_answers = boot_reads
    assert replica_age._boot_id() == "boot-a"
    assert replica_age._boot_id() == "boot-a"
    assert len(proc_reads) == 1
    assert sysctl_reads == []


def test_failed_boot_id_read_is_not_kept(boot_reads):
    proc_reads, proc_answers, sysctl_reads, sysctl_answers = boot_reads
    proc_answers.append(OSError)
    sysctl_answers.append(1)  # both paths fail; do not assume this host lacks sysctlbyname
    assert replica_age._boot_id() is None
    assert replica_age._boot_id() == "boot-a"
    assert replica_age._boot_id() == "boot-a"
    assert len(proc_reads) == 2
    assert len(sysctl_reads) == 1


def test_boot_reader_fails_both_paths_and_uses_sysctl_after_proc(boot_reads):
    _proc_reads, proc_answers, sysctl_reads, sysctl_answers = boot_reads
    assert replica_age._read_boot_id() == "boot-a"
    assert sysctl_reads == []
    proc_answers.append(OSError)
    sysctl_answers.append("mac-boot")
    assert replica_age._read_boot_id() == "mac-boot"
    assert sysctl_reads == [b"kern.bootsessionuuid"]
    proc_answers.append(OSError)
    sysctl_answers.append(1)
    assert replica_age._read_boot_id() is None
    proc_answers.append(OSError)
    sysctl_answers.append(OSError)
    assert replica_age._read_boot_id() is None


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
        assert details == [
            ("sync.detail/after_round.awake", False),
            ("sync.detail/after_round.query_rebaseline", False),
            ("sync.detail/after_round.sample_query", False),
            ("sync.detail/after_round.sample_skip", False),
            ("sync.detail/after_round.sample_observe", False),
            ("sync.detail/after_round.sample", False),
        ]
        stats = loop_accounting.counters()
        assert stats["sync.detail/after_round.awake"]["runs"] == 1
        assert stats["sync.detail/after_round.sample"]["runs"] == 1
        assert not [k for k in stats if k.startswith(("sync.board/", "sync.cpu."))]
    finally:
        loop_accounting.reset()


def test_deferred_round_notes_empty_probe_and_skips_the_open(board, monkeypatch, probe_notes):
    path, conn = board
    _insert(conn, "deferred", retry=10**12)
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    queries = []
    real_query = client._WATCH.query

    def query(path, sql, *, unchanged_ok=False):
        queries.append((sql, unchanged_ok))
        return real_query(path, sql, unchanged_ok=unchanged_ok)

    monkeypatch.setattr(client._WATCH, "query", query)
    details = _spy_details(monkeypatch)
    result = client.RoundResult(board="default", mode="primary")
    try:
        assert sync_cpu.step(age.after_round, [result], [("default", path)], awake_fn=lambda: True) == []
        assert queries == [(_EMPTY_SQL, True)]
        assert opened.call_count == 0
        assert probe_notes and probe_notes[0][0] == path
        assert probe_notes[0][1] is not None and probe_notes[0][1][1] is None
        assert ("sync.detail/after_round.sample_query", False) in details
        assert ("sync.detail/after_round.sample_skip", False) in details
        assert ("sync.detail/after_round.sample_observe", False) in details
        assert not [name for name, _outer in details if name.endswith(".sample_full")]
        assert sum(loop_accounting._copy_accounted().values()) == 0
    finally:
        loop_accounting.reset()


def test_unanswered_round_records_full_sample_and_awake_kw(board, monkeypatch, probe_notes):
    path, conn = board
    monkeypatch.setattr(age, "_since", lambda created: float(created))
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    _insert(conn, "unanswered", created=1000)
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    observed = []

    def observe(path, op_id, now, *, awake=True):
        observed.append((op_id, awake))

    monkeypatch.setattr(age.observation, "observe", observe)
    details = _spy_details(monkeypatch)
    result = client.RoundResult(board="default", mode="primary")
    try:
        assert sync_cpu.step(
            age.after_round, [result], [("default", path)], awake_fn=lambda: False) == []
        assert opened.call_count == 1
        assert observed == [("unanswered", False)]
        assert probe_notes and probe_notes[0][1] is not None
        assert probe_notes[0][1][1] is not None
        assert ("sync.detail/after_round.sample_query", False) in details
        assert ("sync.detail/after_round.sample_full", False) in details
        assert ("sync.detail/after_round.sample_observe", False) in details
        assert not [name for name, _outer in details if name.endswith(".sample_skip")]
    finally:
        loop_accounting.reset()


def test_behind_filter_stays_on_the_full_path(board, monkeypatch):
    path, conn = board
    monkeypatch.setattr(age, "_since", lambda created: float(created))
    _insert(conn, "held", created=1000)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    monkeypatch.setattr(client.backlog, "behind", lambda path: True)
    monkeypatch.setattr(client.backlog, "evidence", lambda path: {"head": 1})

    def sendable(conn, reader, ident, rows, evidence):
        assert ident == "" and evidence == {"head": 1}
        assert rows and rows[0]["op_id"] == "held"
        return [], None

    monkeypatch.setattr(client.backlog, "sendable_before_head", sendable)

    def sample(path):
        return age._sample(path)

    details = _spy_details(monkeypatch)
    try:
        assert sync_cpu.step(sample, path) is None
        assert opened.call_count == 1
        assert ("sync.detail/sample.sample_full", False) in details
        assert not [name for name, _outer in details if name.endswith(".sample_skip")]
    finally:
        loop_accounting.reset()


def test_error_round_does_not_sample(board, monkeypatch, probe_notes):
    path, conn = board
    _insert(conn, "unanswered", created=1000)
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    details = _spy_details(monkeypatch)
    result = client.RoundResult(board="default", mode="primary", error="down")
    try:
        assert sync_cpu.step(age.after_round, [result], [("default", path)], awake_fn=lambda: True) == []
        assert probe_notes == []
        names = [name for name, _outer in details]
        assert "sync.detail/after_round.awake" in names
        assert "sync.detail/after_round.sample" not in names
        assert "sync.detail/after_round.sample_observe" not in names
    finally:
        loop_accounting.reset()
