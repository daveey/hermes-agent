"""execute_code's terminal() hands code the COMPLETE command output.

Regression for self-heal defect agent-7ac9c5fa93: a script built a SHA256SUMS
file from ``terminal(...)["output"]``; the chat-display head+tail window
(``tool_output.max_bytes``) cut 13,635 chars out of the middle and the
``[OUTPUT TRUNCATED - ...]`` marker landed in the file with exit_code 0.
Code callers now get the full text (from the redacted spill file) or an
explicit ``truncated: True``; write_file flags content carrying a Hermes
truncation marker.
"""

import json
from pathlib import Path

import pytest

from tools.code_execution_rpc import _handle_rpc_request
from tools.code_execution_tool import SANDBOX_ALLOWED_TOOLS, execute_code
from tools.file_tools import write_file_tool

BIG_CHARS = 140_127


@pytest.fixture(autouse=True)
def _local_terminal(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    from tools.code_kernel import shutdown_all_kernels
    shutdown_all_kernels()
    yield
    shutdown_all_kernels()


def _rpc(result: dict) -> dict:
    """One terminal call through the host-side RPC pipeline with a canned tool result."""
    resp = _handle_rpc_request(
        {"tool": "terminal", "args": {"command": "cat big"}},
        allowed_tools=frozenset({"terminal"}), tool_call_counter=[0], max_tool_calls=50,
        dispatch=lambda name, args: json.dumps(result), tool_call_log=[], call_start=0.0,
        where="test")
    return json.loads(resp)


@pytest.mark.platforms("posix")
def test_sandbox_terminal_returns_complete_output(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("".join(f"{i:06d} " + "x" * 60 + "\n" for i in range(2100))[:BIG_CHARS])
    code = (
        "import json\n"
        "from hermes_tools import terminal\n"
        f"r = terminal({('cat ' + str(big))!r})\n"
        "print(json.dumps({'len': len(r['output']), "
        "'marker': 'TRUNCATED' in r['output'], 'truncated': r.get('truncated'), "
        "'exit_code': r.get('exit_code')}))\n"
    )
    result = json.loads(execute_code(code=code, task_id="t-full-output",
                                     enabled_tools=list(SANDBOX_ALLOWED_TOOLS)))
    assert result["status"] == "success", result
    seen = json.loads(result["output"].strip().splitlines()[-1])
    assert seen == {"len": BIG_CHARS, "marker": False, "truncated": False, "exit_code": 0}


@pytest.mark.platforms("posix")
def test_spill_file_has_no_shell_markers(tmp_path):
    """The spill holds exactly what the command printed: no session pipe/cwd marker lines."""
    from tools.terminal_tool import terminal_tool
    big = tmp_path / "big.txt"
    big.write_text("y" * 99 + "\n" + "z" * 120_000)
    r = json.loads(terminal_tool(f"cat {big}", task_id="t-spill-markers"))
    spill = Path(r["full_output_path"]).read_text()
    assert "__HERMES_" not in spill
    assert spill.strip() == big.read_text()


def test_spilled_output_replaces_window(tmp_path):
    spill = tmp_path / "out.log"
    spill.write_text("A" * 1000 + "MIDDLE" + "B" * 1000)
    r = _rpc({"output": "AAA\n\n... [OUTPUT TRUNCATED - 1,500 chars omitted out of 2,006 total] ...\n\nBBB",
              "exit_code": 0, "error": None, "output_total_chars": 2006,
              "full_output_path": str(spill), "truncation_note": "Output exceeded ..."})
    assert r["output"] == spill.read_text()
    assert r["truncated"] is False
    assert "truncation_note" not in r


def test_unreadable_spill_reports_truncated(tmp_path):
    window = "AAA\n\n... [OUTPUT TRUNCATED - 1,500 chars omitted out of 2,006 total] ...\n\nBBB"
    missing = str(tmp_path / "gone.log")
    r = _rpc({"output": window, "exit_code": 0, "error": None, "output_total_chars": 2006,
              "full_output_path": missing})
    assert r["output"] == window
    assert r["truncated"] is True
    assert r["full_output_path"] == missing


def test_capped_spill_reports_truncated(tmp_path):
    spill = tmp_path / "out.log"
    spill.write_text("A" * 100 + "\n... [spill capped at 5,000,000 chars] ...\n")
    r = _rpc({"output": "window", "exit_code": 0, "error": None, "output_total_chars": 9_000_000,
              "full_output_path": str(spill)})
    assert r["output"] == "window"
    assert r["truncated"] is True


def test_window_without_spill_reports_truncated():
    """managed_modal / artifacts off / dropped spill handle: no full_output_path, yet the
    output is a cut window, so it must not be reported complete."""
    window = "AAA\n\n... [OUTPUT TRUNCATED - 1,500 chars omitted out of 2,006 total] ...\n\nBBB"
    r = _rpc({"output": window, "exit_code": 0, "error": None})
    assert r["output"] == window
    assert r["truncated"] is True
    r = _rpc({"output": "short head", "exit_code": 0, "error": None, "output_total_chars": 50_074})
    assert r["truncated"] is True


def test_short_output_is_marked_complete():
    r = _rpc({"output": "hello", "exit_code": 0, "error": None})
    assert r["output"] == "hello"
    assert r["truncated"] is False


def test_pending_approval_result_keeps_its_shape():
    pending = {"output": "", "exit_code": -1, "error": "", "status": "pending_approval",
               "approval_pending": True, "command": "rm -rf x"}
    assert _rpc(pending) == pending


def test_write_file_warns_on_truncation_marker(tmp_path):
    marked = tmp_path / "SHA256SUMS"
    content = ("abc  a.txt\n\n... [OUTPUT TRUNCATED - 13,635 chars omitted out of 63,562 total] ...\n\n"
               "def  z.txt\n")
    r = json.loads(write_file_tool(str(marked), content, task_id="t-marker"))
    assert marked.read_text() == content  # the write is never blocked
    assert "truncation marker" in r["warning"]

    plain = tmp_path / "plain.txt"
    r = json.loads(write_file_tool(str(plain), "abc  a.txt\n", task_id="t-marker"))
    assert "warning" not in r
