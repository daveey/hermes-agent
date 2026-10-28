"""Regression: the pre-final gate must fail open QUIETLY when the process is exiting.

A turn that finishes while the backend exits (Desktop quit, ``hermes update``) reaches
``pre_final_gate._invoke`` after interpreter finalization began; its worker-pool submit raises
``RuntimeError: cannot schedule new futures after interpreter shutdown``. The gate already failed
open, but logged a WARNING with a full traceback, which landed in errors.log and was filed as a
defect by the self-heal sweeper (agent-9a2a3cc05d). Shutdown is not a gate failure: one INFO line,
no traceback, same fail-open decision.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agent import pre_final_gate as gate

REPO = Path(__file__).resolve().parents[2]

_SCRIPT = textwrap.dedent(
    """
    import logging, sys, threading, time
    sys.path.insert(0, {repo!r})
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from agent import pre_final_gate as g
    if {preimport!r}:
        import concurrent.futures.thread  # pool module loaded -> "cannot schedule new futures ..."

    def cb(**kw):
        return {{}}

    def turn():
        time.sleep(0.5)  # the main thread has returned: interpreter finalization has begun
        r = g._invoke(cb, {{}}, 1, 5.0)
        print("FAILED_OPEN", r is g._FAILED, flush=True)

    threading.Thread(target=turn).start()  # non-daemon, like a backend turn worker
    """
)


@pytest.mark.parametrize("preimport", [True, False], ids=["futures-refused", "atexit-refused"])
def test_gate_during_interpreter_shutdown_fails_open_without_traceback(preimport):
    """Both CPython refusals: a loaded pool module refuses the submit; an unloaded one fails its
    own import with "can't register atexit after shutdown"."""
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(repo=str(REPO), preimport=preimport)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    out = proc.stdout + proc.stderr
    assert "FAILED_OPEN True" in proc.stdout, out
    assert "Traceback" not in out, out
    lines = [ln for ln in out.splitlines() if "pre_final_gate hook" in ln]
    assert len(lines) == 1 and lines[0].startswith("INFO "), out
    assert "process exiting" in lines[0], out


def test_unrelated_hook_error_still_warns_with_traceback(caplog):
    def boom(**_kw):
        raise RuntimeError("real hook bug")

    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        assert gate._invoke(boom, {}, 1, 5.0) is gate._FAILED
    recs = [r for r in caplog.records if "pre_final_gate hook" in r.getMessage()]
    assert recs and recs[-1].levelno == logging.WARNING and recs[-1].exc_info, recs


# ── same rule on the tool-executor path ("handle_function_call raised for ...") ──────────────────

def _run_failing_tool(monkeypatch, exc):
    from types import SimpleNamespace

    from agent import tool_executor as te

    def _raise(*_a, **_kw):
        raise exc

    monkeypatch.setattr(te, "_run_sequential_tool_execution_middleware", _raise)
    dispatch = te._SequentialDispatch(
        execute=lambda _args: None, error_result=lambda e: f"Error executing tool 'demo': {e}",
        error_log="handle_function_call raised for %s: %s", finish_spinner=False,
    )
    ref = te._ToolCallRef("demo", {}, "task", "call-1", [])
    managed, _ = te._run_sequential_call(
        SimpleNamespace(), dispatch, ref, scope_block=None, messages=[], remaining_calls=[],
        display_index=1, tool_start_time=0.0,
    )
    return managed


def test_tool_call_during_interpreter_shutdown_logs_one_line(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        managed = _run_failing_tool(
            monkeypatch, RuntimeError("cannot schedule new futures after interpreter shutdown"))
    assert "interpreter shutdown" in managed.result
    recs = [r for r in caplog.records if "handle_function_call raised" in r.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == logging.INFO and not recs[0].exc_info, recs
    assert "process exiting" in recs[0].getMessage()


def test_tool_call_real_error_still_logs_traceback(monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _run_failing_tool(monkeypatch, RuntimeError("real tool bug"))
    recs = [r for r in caplog.records if "handle_function_call raised" in r.getMessage()]
    assert len(recs) == 1 and recs[0].levelno == logging.ERROR and recs[0].exc_info, recs
