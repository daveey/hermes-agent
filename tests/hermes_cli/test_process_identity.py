"""Tests for hermes_cli.process_identity — spawn tags, the machine spawn
ledger, and the updater's ledger-identified reap rung.

Layer context (Aug 2026, after the 12-minute Windows update hang): reapers
previously inferred process lineage from PPIDs and cmdline shape. These
primitives make identity positive instead: spawners stamp children
(HERMES_SPAWN), long-lived processes self-register (pid, create_time,
purpose, spawner) in spawn-ledger.json, and `hermes update` reaps holders the
ledger PROVES are orphaned backends — in any update context, no hand-off
contract needed.

Runs on any host: psutil interactions go through a fake module.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import process_identity as pi


class _FakeNoSuchProcess(Exception):
    pass


def _fake_psutil(procs: dict[int, float]):
    """pid -> create_time; missing pid raises NoSuchProcess."""

    def _process(pid: int):
        if pid not in procs:
            raise _FakeNoSuchProcess(pid)
        proc = MagicMock()
        proc.pid = pid
        proc.create_time.return_value = procs[pid]
        return proc

    return types.SimpleNamespace(Process=_process, NoSuchProcess=_FakeNoSuchProcess)


# ---------------------------------------------------------------------------
# Spawn tags
# ---------------------------------------------------------------------------

def test_spawn_tag_roundtrip():
    with patch.dict(sys.modules, {"psutil": _fake_psutil({999: 1234.5})}):
        with patch.object(pi.os, "getpid", return_value=999):
            raw = pi.build_spawn_tag("serve", project_root=Path("/x/install"))
    tag = pi.parse_spawn_tag(raw)
    assert tag is not None
    assert tag.purpose == "serve"
    assert tag.spawner_pid == 999
    assert tag.spawner_create == pytest.approx(1234.5, abs=0.01)
    assert tag.install == pi.install_id(Path("/x/install"))


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "v0:abc:serve:1:2.0",          # wrong version
        "v1:abc:serve:notanint:2.0",   # bad pid
        "v1:abc:serve:-4:2.0",         # non-positive pid
        "v1::serve:10:2.0",            # empty install
        "v1:abc::10:2.0",              # empty purpose
        "v1:abc:serve:10",             # wrong arity
        "v1:abc:serve:10:zzz",         # bad create
    ],
)
def test_parse_spawn_tag_rejects_malformed(raw):
    assert pi.parse_spawn_tag(raw) is None


def test_parse_spawn_tag_dash_create_is_none():
    tag = pi.parse_spawn_tag("v1:abcdef:serve:42:-")
    assert tag is not None
    assert tag.spawner_create is None


def test_desktop_style_tag_parses():
    # The Electron side emits install `-` with a winms-derived create time.
    tag = pi.parse_spawn_tag("v1:-:serve:5100:1755689000.123")
    assert tag is not None
    assert tag.spawner_pid == 5100


def test_install_id_stable_and_path_scoped():
    a = pi.install_id(Path("/opt/hermes"))
    assert a == pi.install_id(Path("/opt/hermes"))
    assert a != pi.install_id(Path("/opt/other"))
    assert len(a) == 12


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _entry(pid, create, purpose="serve", install=None, spawner_pid=None, spawner_create=None):
    return {
        "pid": pid,
        "create_time": create,
        "purpose": purpose,
        "install": install or pi.install_id(Path("/x/install")),
        "spawner_pid": spawner_pid,
        "spawner_create": spawner_create,
        "registered_at": 0.0,
        "argv": "",
    }


def test_register_self_writes_and_prunes_dead(tmp_path):
    ledger = tmp_path / "spawn-ledger.json"
    # Pre-existing: pid 300 dead, pid 400 alive.
    ledger.write_text(json.dumps([_entry(300, 1.0), _entry(400, 2.0)]), encoding="utf-8")
    fake = _fake_psutil({400: 2.0, 999: 50.0})
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "_ledger_path", return_value=ledger), \
         patch.object(pi.os, "getpid", return_value=999):
        assert pi.register_self("serve", project_root=Path("/x/install")) is True
    entries = json.loads(ledger.read_text(encoding="utf-8"))
    pids = {e["pid"] for e in entries}
    assert pids == {400, 999}  # 300 pruned as provably dead
    me = next(e for e in entries if e["pid"] == 999)
    assert me["purpose"] == "serve"
    assert me["create_time"] == pytest.approx(50.0, abs=0.01)


def test_register_self_survives_non_utf8_argv(tmp_path):
    ledger = tmp_path / "spawn-ledger.json"
    fake = _fake_psutil({999: 50.0})
    bad_argv = ["hermes", "serve", os.fsdecode(b"/tmp/project-\xff")]  # surrogate-escaped path
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "_ledger_path", return_value=ledger), \
         patch.object(pi.os, "getpid", return_value=999), \
         patch.object(sys, "argv", bad_argv):
        assert pi.register_self("serve", project_root=Path("/x/install")) is True
    me = next(e for e in json.loads(ledger.read_text(encoding="utf-8")) if e["pid"] == 999)
    assert me["argv"] == " ".join(bad_argv)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
def test_register_self_writes_ledger_with_0600(tmp_path):
    ledger = tmp_path / "spawn-ledger.json"
    fake = _fake_psutil({999: 50.0})
    old_umask = os.umask(0o022)  # permissive umask: the mode must come from the writer, not the env
    try:
        with patch.dict(sys.modules, {"psutil": fake}), \
             patch.object(pi, "_ledger_path", return_value=ledger), \
             patch.object(pi.os, "getpid", return_value=999):
            assert pi.register_self("serve", project_root=Path("/x/install")) is True
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(os.stat(ledger).st_mode) == 0o600


def test_register_self_inherits_spawn_tag_lineage(tmp_path):
    ledger = tmp_path / "spawn-ledger.json"
    fake = _fake_psutil({999: 50.0})
    env = {pi.SPAWN_ENV_VAR: "v1:-:serve:777:123.000"}
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.dict(pi.os.environ, env, clear=False), \
         patch.object(pi, "_ledger_path", return_value=ledger), \
         patch.object(pi.os, "getpid", return_value=999):
        pi.register_self("serve", project_root=Path("/x/install"))
    me = json.loads(ledger.read_text(encoding="utf-8"))[0]
    assert me["spawner_pid"] == 777
    assert me["spawner_create"] == pytest.approx(123.0, abs=0.01)


def test_register_self_falls_back_to_desktop_parent_env(tmp_path):
    # Legacy Desktop: no HERMES_SPAWN, but HERMES_PARENT_PID + winms marker.
    ledger = tmp_path / "spawn-ledger.json"
    fake = _fake_psutil({999: 50.0})
    env = {
        "HERMES_PARENT_PID": "888",
        "HERMES_PARENT_START_MARKER": "winms:1755689000123",
    }
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.dict(pi.os.environ, env, clear=False), \
         patch.object(pi.os.environ, "get", pi.os.environ.get), \
         patch.object(pi, "_ledger_path", return_value=ledger), \
         patch.object(pi.os, "getpid", return_value=999):
        pi.os.environ.pop(pi.SPAWN_ENV_VAR, None)
        pi.register_self("serve", project_root=Path("/x/install"))
    me = json.loads(ledger.read_text(encoding="utf-8"))[0]
    assert me["spawner_pid"] == 888
    assert me["spawner_create"] == pytest.approx(1755689000.123, abs=0.01)


def test_corrupt_ledger_quarantined_not_rewritten_blind(tmp_path):
    # #89298 contract: corruption is quarantined aside, never treated as [].
    ledger = tmp_path / "spawn-ledger.json"
    ledger.write_text("{ not json", encoding="utf-8")
    fake = _fake_psutil({999: 50.0})
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "_ledger_path", return_value=ledger), \
         patch.object(pi.os, "getpid", return_value=999):
        assert pi.register_self("serve", project_root=Path("/x/install")) is True
    parked = tmp_path / "spawn-ledger.json.corrupt"
    assert parked.exists()
    assert parked.read_text(encoding="utf-8") == "{ not json"
    entries = json.loads(ledger.read_text(encoding="utf-8"))
    assert [e["pid"] for e in entries] == [999]


def test_ledger_entries_filters_dead_reused_and_foreign(tmp_path):
    ledger = tmp_path / "spawn-ledger.json"
    entries = [
        _entry(100, 10.0),                                  # alive, matches
        _entry(200, 20.0),                                  # pid reused (create mismatch)
        _entry(300, 30.0),                                  # dead
        _entry(400, 40.0, install="ffffffffffff"),          # other install
    ]
    ledger.write_text(json.dumps(entries), encoding="utf-8")
    fake = _fake_psutil({100: 10.0, 200: 9999.0, 400: 40.0})
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "_ledger_path", return_value=ledger):
        live = pi.ledger_entries(project_root=Path("/x/install"))
    assert [e["pid"] for e in live] == [100]


@pytest.mark.platforms("posix")
def test_ledger_entries_excludes_a_killed_but_unreaped_process(tmp_path):
    """A zombie is dead even though it keeps its create_time until reaped: ``hermes update``
    books the stopped dashboard gone, then must not find it again as a pre-update survivor."""
    import psutil

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = psutil.Process(child.pid).create_time()
        child.terminate()
        deadline = time.monotonic() + 10
        while psutil.Process(child.pid).status() != psutil.STATUS_ZOMBIE:
            assert time.monotonic() < deadline, "child never became a zombie"
            time.sleep(0.05)
        ledger = tmp_path / "spawn-ledger.json"
        ledger.write_text(json.dumps([_entry(child.pid, created, purpose="dashboard")]), encoding="utf-8")
        with patch.object(pi, "_ledger_path", return_value=ledger):
            assert pi.ledger_entries(project_root=Path("/x/install")) == []
    finally:
        child.kill()
        child.wait()


def test_spawner_is_dead_tristate():
    fake = _fake_psutil({500: 5.0})
    with patch.dict(sys.modules, {"psutil": fake}):
        assert pi.spawner_is_dead(_entry(1, 1.0, spawner_pid=500, spawner_create=5.0)) is False
        assert pi.spawner_is_dead(_entry(1, 1.0, spawner_pid=600, spawner_create=6.0)) is True
        # PID reuse: recorded spawner create differs from live process → dead.
        assert pi.spawner_is_dead(_entry(1, 1.0, spawner_pid=500, spawner_create=999.0)) is True
        assert pi.spawner_is_dead(_entry(1, 1.0)) is None


class TestStrictBranchStatusRead:
    """#142085: the strict branch of ``_pid_alive_matches`` read ``Process.status()`` on Windows,
    where psutil's NtQuerySystemInformation path does not release the GIL — under load that froze
    every other Python thread in the process for minutes. The non-strict branch already skipped
    the read on Windows; the strict branch must too (Windows has no POSIX zombies, so the
    STATUS_ZOMBIE comparison had nothing to contribute there). Tested on the real host: faking
    ``os.name`` on POSIX proves nothing about the cost on Windows."""

    @staticmethod
    def _spy_status(monkeypatch):
        psutil = pytest.importorskip("psutil")
        calls = []
        real_status = psutil.Process.status

        def spy(self):
            calls.append(self.pid)
            return real_status(self)

        monkeypatch.setattr(psutil.Process, "status", spy)
        return calls

    @pytest.mark.platforms("windows")
    def test_windows_strict_branch_skips_status(self, monkeypatch):
        calls = self._spy_status(monkeypatch)
        psutil = pytest.importorskip("psutil")
        pid = os.getpid()
        create_time = pi.process_birth(psutil.Process(pid))

        assert pi._pid_alive_matches(pid, create_time, strict=True) is True
        assert calls == []

    @pytest.mark.platforms("posix")
    def test_posix_strict_branch_still_probes_status(self, monkeypatch):
        # Control: on POSIX a zombie keeps its create_time, so the status read is load-bearing.
        calls = self._spy_status(monkeypatch)
        psutil = pytest.importorskip("psutil")
        pid = os.getpid()
        create_time = pi.process_birth(psutil.Process(pid))

        assert pi._pid_alive_matches(pid, create_time, strict=True) is True
        assert calls == [pid]


# ---------------------------------------------------------------------------
# Updater rung: _ledger_reapable_backend_pids
# ---------------------------------------------------------------------------

def _holders(*pids):
    return [(p, "python.exe", f"python.exe -m hermes_cli.main --profile p{p} serve") for p in pids]


def test_updater_reaps_ledger_proven_orphans():
    from hermes_cli import update_cmd_windows

    entries = [
        _entry(200, 2.0, spawner_pid=700, spawner_create=7.0),   # spawner dead → reap
        _entry(201, 2.1, spawner_pid=500, spawner_create=5.0),   # spawner alive → keep
        _entry(202, 2.2, purpose="chat", spawner_pid=700, spawner_create=7.0),  # not reapable purpose
    ]
    fake = _fake_psutil({500: 5.0})
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "ledger_entries", return_value=entries), \
         patch.object(pi, "spawner_is_dead", wraps=pi.spawner_is_dead):
        assert update_cmd_windows._ledger_reapable_backend_pids(_holders(200, 201, 202, 203)) == [200]




def test_updater_ledger_rung_never_raises():
    from hermes_cli import update_cmd_windows

    with patch.object(pi, "ledger_entries", side_effect=RuntimeError("boom")):
        assert update_cmd_windows._ledger_reapable_backend_pids(_holders(200)) == []


def test_desktop_ssh_backend_spawn_shape_is_desktop_owned(monkeypatch):
    """Desktop's SSH spawn is ``env HERMES_DESKTOP=1 hermes serve --isolated ... --ssh-session-token-file F``
    with NO token env var (its tests assert the var name never appears on the wire). Missing that
    shape made the SSH child claim ROLE_SERVE on the remote host (the #119824 shape there)."""
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    ssh_argv = ["serve", "--isolated", "--host", "127.0.0.1", "--port", "0",
                "--ssh-session-token-file", "/home/u/.hermes/desktop-ssh/abc.token"]

    assert pi.is_desktop_owned_backend(ssh_argv) is True
    monkeypatch.setattr(sys, "argv", ["hermes", *ssh_argv])
    assert pi.is_desktop_owned_backend() is True
    # The bare inherited flag (a Desktop terminal pane running `hermes serve`) is still not ownership.
    assert pi.is_desktop_owned_backend(["serve", "--host", "127.0.0.1", "--port", "0"]) is False


# ---------------------------------------------------------------------------
# Host-reclaim rung: reap_orphaned_backend_owner (#121964)
# ---------------------------------------------------------------------------

def _reap(entry_pid=555, create=55.0, procs=None, kill_fn=None, **entry_kw):
    """Run the reclaim predicate with a faked psutil world; returns (result, kills)."""
    entry = _entry(entry_pid, create, **entry_kw)
    kills = []
    fake = _fake_psutil({} if procs is None else procs)
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "ledger_entries", return_value=[entry]):
        result = pi.reap_orphaned_backend_owner(
            entry_pid, create, kill_fn=(kills.append if kill_fn is None else kill_fn))
    return result, kills


def test_reclaim_reaps_dead_spawner_owner():
    result, kills = _reap(spawner_pid=700, spawner_create=7.0, procs={555: 55.0})
    assert result == 555
    assert kills == [555]


def test_reclaim_spares_live_spawner_owner():
    result, kills = _reap(spawner_pid=500, spawner_create=5.0, procs={555: 55.0, 500: 5.0})
    assert result is None
    assert kills == []


def test_reclaim_spares_unprovable_spawner():
    # Spawner probe itself fails (permission): unprovable means never touch.
    import types

    from unittest.mock import MagicMock

    def _process(pid):
        if pid == 500:
            raise PermissionError(pid)
        proc = MagicMock()
        proc.create_time.return_value = 55.0
        return proc

    fake = types.SimpleNamespace(Process=_process, NoSuchProcess=_FakeNoSuchProcess)
    with patch.dict(sys.modules, {"psutil": fake}), \
         patch.object(pi, "ledger_entries",
                      return_value=[_entry(555, 55.0, spawner_pid=500, spawner_create=5.0)]):
        assert pi.reap_orphaned_backend_owner(555, 55.0, kill_fn=lambda pid: None) is None


def test_reclaim_reaps_null_spawner_orphan_to_init():
    from hermes_cli import dashboard_procs

    with patch.object(dashboard_procs, "_process_ppid", return_value=1), \
         patch.object(dashboard_procs, "_lock_owned_serve_pids", return_value=set()):
        # Ancient orphan: create_time far past vs real clock → past the lock-write grace.
        result, kills = _reap(procs={555: 1000.0}, create=1000.0)
    assert result == 555
    assert kills == [555]


def test_reclaim_spares_null_spawner_with_live_parent():
    from hermes_cli import dashboard_procs

    with patch.object(dashboard_procs, "_process_ppid", return_value=1234):
        result, kills = _reap(procs={555: 55.0})
    assert result is None
    assert kills == []


def test_reclaim_spares_lock_claimed_orphan():
    """A valid backend.lock.json claims the PID: another session owns it, never kill."""
    from hermes_cli import dashboard_procs

    with patch.object(dashboard_procs, "_process_ppid", return_value=1), \
         patch.object(dashboard_procs, "_lock_owned_serve_pids", return_value={555}):
        result, kills = _reap(procs={555: 1000.0}, create=1000.0)
    assert result is None
    assert kills == []


def test_reclaim_spares_interactive_purposes_and_self():
    result, kills = _reap(purpose="chat", spawner_pid=700, spawner_create=7.0,
                          procs={555: 55.0})
    assert (result, kills) == (None, [])
    with patch.object(pi.os, "getpid", return_value=555):
        result, kills = _reap(spawner_pid=700, spawner_create=7.0, procs={555: 55.0})
    assert (result, kills) == (None, [])


def test_reclaim_ignores_unknown_pid_and_never_raises():
    with patch.dict(sys.modules, {"psutil": _fake_psutil({555: 55.0})}), \
         patch.object(pi, "ledger_entries", return_value=[_entry(555, 55.0)]):
        assert pi.reap_orphaned_backend_owner(999, 9.0, kill_fn=lambda pid: None) is None
    with patch.object(pi, "ledger_entries", side_effect=RuntimeError("boom")):
        assert pi.reap_orphaned_backend_owner(555, 55.0, kill_fn=lambda pid: None) is None


_CONCURRENT_WRITER = r"""
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
from hermes_cli.process_identity import register_child
start_at, n = float(sys.argv[2]), int(sys.argv[3])
kids = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
        for _ in range(n)]
while time.time() < start_at:
    time.sleep(0.001)
for kid in kids:
    register_child(kid.pid, "ledger-race")
print(" ".join(str(k.pid) for k in kids), flush=True)
sys.stdin.read()  # keep the children alive until the test has read the ledger
for kid in kids:
    kid.kill()
"""


def test_concurrent_processes_never_drop_each_others_ledger_rows(tmp_path, monkeypatch):
    """Every spawn_owned writes the machine-wide ledger; three processes registering at once lost
    about two thirds of their rows to each other's read-modify-write before the cross-process lock.
    A writer descheduled while holding the lock (a loaded runner) used to make the others write past
    it and lose rows too; those rows are now queued beside the ledger and folded in when it lets go."""
    import json
    import time
    from pathlib import Path

    repo = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "home")}
    start_at = time.time() + 2.0
    writers = [subprocess.Popen([sys.executable, "-c", _CONCURRENT_WRITER, repo, str(start_at), "15"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
               for _ in range(3)]
    try:
        pids = {int(p) for w in writers for p in w.stdout.readline().split()}
        assert len(pids) == 45
        readable = {e["pid"] for e in pi.ledger_entries(all_installs=True) if e.get("purpose") == "ledger-race"}
        assert readable == pids, f"{len(pids - readable)} of 45 rows lost to a concurrent writer"
        deadline = time.monotonic() + 30  # a row queued past a descheduled holder lands once it lets go
        while True:
            rows = json.loads((tmp_path / "home" / "spawn-ledger.json").read_text(encoding="utf-8"))
            kept = {r["pid"] for r in rows if r.get("purpose") == "ledger-race"}
            if kept == pids or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert kept == pids, f"{len(pids - kept)} of 45 rows never reached the ledger file"
    finally:
        for w in writers:
            w.stdin.close()
            w.wait(timeout=30)


@pytest.mark.platforms("posix")  # an fcntl lock holder
def test_a_stalled_ledger_lock_holder_never_blocks_register_child(tmp_path, monkeypatch, caplog):
    """A holder stopped mid-write (SIGSTOP, a debugger) kept the lock: every spawn_owned on the
    machine then waited forever. Past the timeout the row is queued beside the ledger (logged once at
    WARNING), readers see it at once, and it lands in the ledger once the holder lets go."""
    import fcntl
    import logging
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pi, "_LEDGER_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pi, "_PENDING_FOLD_INTERVAL_S", 0.1, raising=False)
    monkeypatch.setattr(pi, "_ledger_lock_warned", False)
    caplog.set_level(logging.DEBUG, logger=pi.logger.name)
    ledger = pi._ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger.with_name(ledger.name + ".lock"), "a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)  # the stalled holder
        done = []
        writer = threading.Thread(target=lambda: done.append(pi.register_child(os.getpid(), "stalled-lock")),
                                  daemon=True)
        writer.start()
        writer.join(timeout=10)
        assert done == [True], "register_child waited on a stalled lock holder"
        assert pi.register_child(os.getpid(), "stalled-again") is True  # a second write past the lock
        seen = [e["purpose"] for e in pi.ledger_entries(all_installs=True)]
        assert seen == ["stalled-again"], f"a row queued past the stalled holder is not readable: {seen}"
        assert not ledger.exists(), "a write past the lock replaced the ledger under its holder"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "spawn ledger lock" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} warnings for two writes past a stalled lock"
    deadline = time.monotonic() + 10
    while not ledger.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    rows = json.loads(ledger.read_text(encoding="utf-8"))
    assert [r.get("purpose") for r in rows] == ["stalled-again"], "the queued row was not folded into the ledger"
    assert not list(pi._pending_dir(ledger).glob("*.json")), "a folded row was left queued"


_STALLED_HOLDER = r"""
import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import hermes_cli.process_identity as pi
go = Path(sys.argv[2])
real_write = pi.atomic_json_write
def stalled_write(*a, **kw):  # stopped after its read, before its replace (SIGSTOP, a debugger)
    Path(str(go) + ".read").touch()
    while not go.exists():
        time.sleep(0.01)
    return real_write(*a, **kw)
pi.atomic_json_write = stalled_write
print(pi.register_child(os.getpid(), "stalled-holder"), flush=True)
"""


@pytest.mark.platforms("posix")  # an fcntl lock holder in another process
def test_a_row_written_past_a_stalled_holder_survives_its_late_write(tmp_path, monkeypatch):
    """A writer that gave up on a stalled holder wrote the ledger itself; when the holder resumed, its
    replace (built from its read before the stall) dropped that row (2 rows lost in #667's CI run)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pi, "_LEDGER_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pi, "_PENDING_FOLD_INTERVAL_S", 0.1, raising=False)
    monkeypatch.setattr(pi, "_ledger_lock_warned", False)
    repo = str(Path(__file__).resolve().parents[2])
    go = tmp_path / "go"
    holder = subprocess.Popen([sys.executable, "-c", _STALLED_HOLDER, repo, str(go)], stdout=subprocess.PIPE,
                              text=True, env={**os.environ, "HERMES_HOME": str(tmp_path / "home")})
    try:
        deadline = time.monotonic() + 60
        while not Path(str(go) + ".read").exists():
            assert holder.poll() is None and time.monotonic() < deadline, "the holder never reached its write"
            time.sleep(0.01)
        assert pi.register_child(os.getpid(), "past-the-holder") is True
        go.touch()
        assert holder.stdout.readline().strip() == "True"
        assert holder.wait(timeout=30) == 0
        purposes = {e["purpose"] for e in pi.ledger_entries(all_installs=True)}
        assert "past-the-holder" in purposes, "the stalled holder's late write dropped a row written past it"
        assert pi.register_child(os.getpid(), "past-the-holder") is True  # the lock is free: folds the queue
        rows = json.loads(pi._ledger_path().read_text(encoding="utf-8"))
        assert [r["purpose"] for r in rows if r["pid"] == os.getpid()] == ["past-the-holder"]
    finally:
        go.touch()
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=30)


@pytest.mark.platforms("posix")  # an fcntl lock holder
def test_register_child_waits_behind_writers_that_keep_landing(tmp_path, monkeypatch, caplog):
    """The lock wait gave up after a flat 2 s even while the writers ahead kept landing rows; on a
    loaded host a queue of live writers outlasted it, so rows were written past the lock and lost."""
    import fcntl
    import logging
    import threading

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pi, "_LEDGER_LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pi, "_ledger_lock_warned", False)
    caplog.set_level(logging.DEBUG, logger=pi.logger.name)
    ledger = pi._ledger_path()
    ledger.parent.mkdir(parents=True, exist_ok=True)
    released = threading.Event()

    class WriterClock:
        # Real atomic replacements and real lock contention, but host scheduling cannot
        # consume the fixture's deliberately short stall window between writes.
        now = 0.0
        writes = 0

        def __getattr__(self, name):
            return getattr(time, name)

        def monotonic(self):
            return self.now

        def sleep(self, interval):
            assert interval == pi._LEDGER_LOCK_POLL_S
            assert not released.is_set(), "lock remained contended after holder release"
            self.now += 0.1
            if self.writes < 15:
                pi.atomic_json_write(ledger, [{"pid": 1, "purpose": f"ahead-{self.writes}"}])
                self.writes += 1
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                released.set()

    clock = WriterClock()
    # Replace this module's clock only, not the shared stdlib time module.
    monkeypatch.setattr(pi, "time", clock)
    with open(ledger.with_name(ledger.name + ".lock"), "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        assert pi.register_child(os.getpid(), "behind-the-queue") is True
    assert clock.writes == 15
    assert clock.now > pi._LEDGER_LOCK_TIMEOUT_S
    assert released.is_set(), "register_child wrote before the writers ahead of it let go of the lock"
    assert not [r for r in caplog.records if "spawn ledger lock" in r.getMessage()], "gave up on live writers"
    rows = json.loads(ledger.read_text(encoding="utf-8"))
    assert [r["purpose"] for r in rows if r["pid"] == os.getpid()] == ["behind-the-queue"]
