"""The autouse session-context reset waits for an import another thread has in flight.

A daemon thread a previous test started (the state.db incident reporter that a corruption latch
schedules) can be importing ``gateway.session_context`` when the next test's setup runs. The
reset read ``sys.modules`` directly, got the module before its body had run and failed setup with
"partially initialized module ... has no attribute 'reset_session_vars'" (flaky
tests/hermes_state/test_storage_health_latch.py on CI).
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import importlib
import sys
import threading
import types

from tests._fixtures.session_context_reset import reset_session_context
from tests.waiting import wait_until

_PROBE = "_session_context_reset_probe"
_GATE = "_session_context_reset_gate"

_PROBE_SOURCE = f"""
import {_GATE} as gate

gate.entered.set()
gate.release.wait(60)

_session_context_engaged = True
reset_calls = 0


def reset_session_vars():
    global reset_calls
    reset_calls += 1
"""


def _inside_import_machinery(thread: threading.Thread) -> bool:
    if thread.ident is None:
        return False
    frame = sys._current_frames().get(thread.ident)
    while frame is not None:
        if frame.f_code.co_filename.startswith("<frozen importlib._bootstrap"):
            return True
        frame = frame.f_back
    return False


def test_reset_waits_for_an_import_in_flight_on_another_thread(tmp_path, monkeypatch):
    gate = types.SimpleNamespace(entered=threading.Event(), release=threading.Event())
    monkeypatch.setitem(sys.modules, _GATE, gate)
    monkeypatch.delitem(sys.modules, _PROBE, raising=False)
    (tmp_path / f"{_PROBE}.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    errors: list[Exception] = []

    def reset() -> None:
        try:
            reset_session_context(_PROBE)
        except Exception as exc:  # health: allow BLE001 -- surfaced by the assertion below
            errors.append(exc)

    importer = threading.Thread(target=importlib.import_module, args=(_PROBE,), daemon=True)
    resetter = threading.Thread(target=reset, daemon=True)
    importer.start()
    try:
        assert gate.entered.wait(30), "probe import never started"
        resetter.start()
        wait_until(lambda: not resetter.is_alive() or _inside_import_machinery(resetter),
                   timeout=30, what="reset to finish or wait on the in-flight import")
    finally:
        gate.release.set()
    importer.join(30)
    resetter.join(30)

    assert errors == []
    probe = sys.modules[_PROBE]
    assert probe.reset_calls == 1
    assert probe._session_context_engaged is False
