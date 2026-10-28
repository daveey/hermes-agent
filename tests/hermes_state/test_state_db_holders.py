"""Behavioral tests for the state-holder and repair-admission authority."""

import os
from types import SimpleNamespace

import pytest

import hermes_state_holders


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("identity", ["alias", "different-inode", "different-device"])
def test_foreign_holder_uses_device_and_inode_not_path(
    tmp_path, monkeypatch, identity
):
    """Descriptor identity is authoritative even when /proc spells another path."""
    db_path = tmp_path / "state.db"
    db_path.touch()
    alias_path = tmp_path / "namespace-alias" / "state.db" if identity == "alias" else db_path

    proc_root = tmp_path / "proc"
    for pid in (111, 222):
        (proc_root / str(pid) / "fd").mkdir(parents=True)
    os.symlink(db_path, proc_root / "222" / "fd" / "3")

    # Keep the /proc projection local: patching os globally also intercepts
    # pytest's home guard and pathlib while they inspect these same symlinks.
    projected_os = SimpleNamespace(**vars(os))
    projected_os.getpid = lambda: 111
    monkeypatch.setattr(hermes_state_holders, "os", projected_os)
    real_listdir = os.listdir

    def _listdir(path):
        if isinstance(path, str):
            path = path.replace("/proc", str(proc_root))
        return real_listdir(path)

    monkeypatch.setattr(hermes_state_holders.os, "listdir", _listdir)

    def _readlink(path):
        if path == "/proc/222/fd/3":
            return str(alias_path)
        return os.readlink(path.replace("/proc", str(proc_root)))

    monkeypatch.setattr(hermes_state_holders.os, "readlink", _readlink)
    real_stat = os.stat

    def _stat(path, *args, **kwargs):
        mapped = str(path).replace("/proc", str(proc_root))
        result = real_stat(mapped, *args, **kwargs)
        if str(path) == "/proc/222/fd/3" and identity != "alias":
            values = list(result)
            values[1 if identity == "different-inode" else 2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(hermes_state_holders.os, "stat", _stat)

    assert hermes_state_holders.foreign_state_db_holders(db_path) == (
        [(222, str(alias_path))] if identity == "alias" else []
    )


def test_windows_restart_manager_scan_sizes_then_excludes_self(monkeypatch, tmp_path):
    """The rstrtmgr lane sizes on ERROR_MORE_DATA, drops our own pid, and fails closed."""
    import ctypes

    db_path = tmp_path / "state.db"
    db_path.touch()
    (tmp_path / "state.db-wal").touch()
    calls = []

    class _Fn:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    def start(session, _flags, _key):
        session._obj.value = 7
        return 0

    def register(_session, count, names, *_rest):
        calls.append(("register", count, sorted(names)))
        return 0

    def get_list(_session, needed, count, apps, _reasons):
        needed._obj.value = 2
        if apps is None:
            return 234  # ERROR_MORE_DATA: sizing call
        apps[0].process.pid = os.getpid()
        apps[1].process.pid = 4242
        count._obj.value = 2
        return 0

    class _Api:
        RmStartSession = _Fn(start)
        RmRegisterResources = _Fn(register)
        RmGetList = _Fn(get_list)
        RmEndSession = _Fn(lambda _session: calls.append("end") or 0)

    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Api(), raising=False)

    holders = hermes_state_holders._windows_restart_manager_holders(db_path)

    assert [pid for pid, _ in holders] == [4242]
    assert calls[0] == ("register", 2, sorted(str(p) for p in (db_path, tmp_path / "state.db-wal")))
    assert calls[-1] == "end"

    _Api.RmStartSession = _Fn(lambda *_args: 5)
    with pytest.raises(OSError):
        hermes_state_holders._windows_restart_manager_holders(db_path)


# ── macOS leg, faked so it runs (and regresses) on Linux CI too ────────────────────────────────
#
# The darwin leg used psutil.Process.open_files(), which stats every file of every process, and
# then realpath'd each path. Under the test real-home I/O guard the first foreign Hermes log file
# raised, the scan failed closed, and 65 tests/hermes_state cases saw "a live writer still holds
# state.db" on every Mac while Linux CI (the /proc leg) stayed green.

FOREIGN_HOME = "/foreign-hermes-home"


@pytest.fixture
def darwin_scan(tmp_path, monkeypatch):
    """Route foreign_state_db_holders to its darwin leg with a fake libproc enumeration, and
    refuse any stat/lstat/realpath of a foreign process's file (what the home-I/O guard does)."""
    import hermes_state_dbfile

    db_path = tmp_path / "state.db"
    db_path.touch()
    (tmp_path / "state.db-wal").touch()
    db_stat = os.stat(db_path)
    wal_stat = os.stat(tmp_path / "state.db-wal")
    state = {"targets": [], "raise": None}

    def fake_targets():
        if state["raise"] is not None:
            raise state["raise"]
        yield from state["targets"]

    platform_view = SimpleNamespace(**{k: getattr(hermes_state_holders.sys, k) for k in ("argv", "executable")})
    platform_view.platform = "darwin"
    monkeypatch.setattr(hermes_state_holders, "sys", platform_view)
    monkeypatch.setattr(hermes_state_holders, "_IS_WINDOWS", False)
    monkeypatch.setattr(hermes_state_dbfile, "_iter_darwin_fd_targets", fake_targets)
    projected_os = SimpleNamespace(**vars(os))
    projected_os.getpid = lambda: 111
    projected_os.path = SimpleNamespace(**vars(os.path))

    def refuse_foreign(fn):
        def guarded(path, *args, **kwargs):
            assert not os.fspath(path).startswith(FOREIGN_HOME), f"scan touched a foreign file: {path}"
            return fn(path, *args, **kwargs)
        return guarded

    projected_os.stat = refuse_foreign(os.stat)
    projected_os.lstat = refuse_foreign(os.lstat)
    projected_os.path.realpath = refuse_foreign(os.path.realpath)
    monkeypatch.setattr(hermes_state_holders, "os", projected_os)
    state.update(db=db_path, db_id=(db_stat.st_dev, db_stat.st_ino),
                 wal_id=(wal_stat.st_dev, wal_stat.st_ino))
    return state


def test_darwin_scan_never_touches_foreign_files_and_finds_holder_by_identity(darwin_scan):
    db = darwin_scan["db"]
    darwin_scan["targets"] = [
        (222, 4, f"{FOREIGN_HOME}/logs/hermes-tunnel.log", (1, 1)),
        # Same vnode under another spelling (symlinked home, bind alias): identity decides.
        (222, 5, "/alias-home/state.db", darwin_scan["db_id"]),
        (111, 3, str(db), darwin_scan["db_id"]),  # this process: never a foreign holder
    ]
    assert hermes_state_holders.foreign_state_db_holders(db) == [(222, "/alias-home/state.db")]


def test_darwin_scan_quiet_store_is_clean_despite_foreign_files(darwin_scan):
    darwin_scan["targets"] = [
        (222, 4, f"{FOREIGN_HOME}/logs/hermes-tunnel.log", (1, 1)),
        (333, 7, f"{FOREIGN_HOME}/state.db", (1, 2)),
    ]
    assert hermes_state_holders.foreign_state_db_holders(darwin_scan["db"]) == []


def test_darwin_scan_counts_a_retired_generation_and_case_folded_path(darwin_scan):
    db = darwin_scan["db"]
    darwin_scan["targets"] = [
        # The WAL path now names a new inode; pid 222 still holds the unlinked one.
        (222, 6, os.path.realpath(str(db)) + "-wal", (darwin_scan["wal_id"][0], darwin_scan["wal_id"][1] + 1000)),
        (333, 6, os.path.realpath(str(db)).upper() + "-SHM", (9, 9)),
    ]
    assert [pid for pid, _ in hermes_state_holders.foreign_state_db_holders(db)] == [222, 333]


def test_darwin_scan_failure_fails_closed(darwin_scan):
    darwin_scan["raise"] = OSError("proc_listpids failed")
    holders = hermes_state_holders.foreign_state_db_holders(darwin_scan["db"])
    assert holders and holders[0][0] == -1
