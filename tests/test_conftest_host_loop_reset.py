"""Retirement reset must await a module's import lock before reading its globals."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import importlib
import sys
import threading
import types

from tests._fixtures.host_loop_reset import reset_retired_host_loops
from tests.waiting import wait_until

_PROBE = "_host_loop_reset_probe"
_GATE = "_host_loop_reset_gate"


def _inside_import_machinery(thread):
    frame = sys._current_frames().get(thread.ident)
    while frame is not None:
        if frame.f_code.co_filename.startswith("<frozen importlib._bootstrap"):
            return True
        frame = frame.f_back
    return False


def test_reset_waits_for_import_before_clearing_retirement(tmp_path, monkeypatch):
    gate = types.SimpleNamespace(entered=threading.Event(), release=threading.Event())
    monkeypatch.setitem(sys.modules, _GATE, gate)
    monkeypatch.delitem(sys.modules, _PROBE, raising=False)
    (tmp_path / f"{_PROBE}.py").write_text(
        f"import {_GATE} as gate\n"
        "import threading\n"
        "_RETIRED = {'superseded': 'old backend'}\n"
        "gate.entered.set()\n"
        "assert gate.release.wait(30), 'import gate never released'\n"
        "_RETIRE_MU = threading.Lock()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    errors = []

    def reset():
        try:
            reset_retired_host_loops(_PROBE)
        except Exception as exc:  # health: allow BLE001 -- asserted in the parent thread
            errors.append(exc)

    importer = threading.Thread(target=importlib.import_module, args=(_PROBE,), daemon=True)
    resetter = threading.Thread(target=reset, daemon=True)
    importer.start()
    try:
        assert gate.entered.wait(30), "probe import never started"
        resetter.start()
        wait_until(lambda: not resetter.is_alive() or _inside_import_machinery(resetter),
                   timeout=30, what="reset to finish or wait for the import lock")
    finally:
        gate.release.set()
        importer.join(30)
        if resetter.ident is not None:
            resetter.join(30)
    assert not importer.is_alive() and not resetter.is_alive()
    assert errors == []
    assert sys.modules[_PROBE]._RETIRED == {}


def test_autouse_fixture_delegates_after_test(monkeypatch):
    from tests._fixtures import host_loop_reset
    from tests.hermes_cli import conftest

    calls = []
    monkeypatch.setattr(host_loop_reset, "reset_retired_host_loops", lambda: calls.append("reset"))
    fixture = conftest._reset_retired_host_loops.__wrapped__()
    next(fixture)
    assert calls == []
    assert next(fixture, "finished") == "finished"
    assert calls == ["reset"]


def test_reset_does_not_import_unused_module(monkeypatch):
    monkeypatch.delitem(sys.modules, _PROBE, raising=False)
    reset_retired_host_loops(_PROBE)
    assert _PROBE not in sys.modules


def test_reset_clears_only_while_holding_retirement_lock(monkeypatch):
    lock = threading.Lock()

    class Retired(dict):
        def clear(self):
            assert lock.locked()
            super().clear()

    retired = Retired(superseded="old backend")
    monkeypatch.setitem(sys.modules, _PROBE, types.SimpleNamespace(_RETIRE_MU=lock, _RETIRED=retired))
    reset_retired_host_loops(_PROBE)
    assert retired == {}
    assert not lock.locked()
