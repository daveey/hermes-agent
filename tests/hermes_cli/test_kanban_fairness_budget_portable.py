"""DB-only capture remains usable with Windows-style lock/fsync primitives."""
import builtins
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_fairness_budget as budget
from scripts.kanban_fairness import main


def test_db_capture_without_unix_primitives(tmp_path, monkeypatch):
    import fcntl

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    db = str(kb.kanban_db_path())
    calls = []

    def locking(fd, mode, count):
        assert count == 1
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        calls.append(mode)
        fcntl.flock(fd, fcntl.LOCK_EX if mode == 1 else fcntl.LOCK_UN)

    real_import = builtins.__import__
    windows_lock = SimpleNamespace(locking=locking, LK_LOCK=1, LK_UNLCK=2)

    def platform_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Simulate the budget module's platform without changing unrelated Linux
        # SQLite lifecycle locks or pytest's own process bookkeeping.
        if globals and globals.get("__name__") == budget.__name__:
            if name == "fcntl":
                raise ImportError("Windows has no fcntl")
            if name == "msvcrt":
                return windows_lock
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", platform_import)
    proxy = SimpleNamespace(**{key: value for key, value in vars(os).items()
                               if key not in ("O_DIRECTORY", "O_NONBLOCK", "O_NOFOLLOW")})
    monkeypatch.setattr(budget, "os", proxy)
    out = tmp_path / "capture"
    assert main(["capture", "arm", "--db", db, "--out", str(out), "--capture-id", "portable"]) == 0
    assert json.loads((out / "plan.json").read_text())["capture_id"] == "portable"
    for action in ("status", "drain", "seal", "clear"):
        assert main(["capture", action, "--out", str(out)]) == 0
    bounded = tmp_path / "bounded"
    budget.initialize(bounded, 128)
    with pytest.raises(budget.BudgetExceeded):
        budget.write(bounded / "too-large", b"x" * 128)
    assert not (bounded / "too-large").exists()
    assert calls and calls == [1, 2] * (len(calls) // 2)
    assert budget._used(out) <= budget._limit(out)
