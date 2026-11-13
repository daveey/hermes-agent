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


WINDOW = "AAA\n\n... [OUTPUT TRUNCATED - 1,500 chars omitted out of 2,006 total] ...\n\nBBB"


def _file_rpc_result(monkeypatch, tool_result: dict, tool: str = "terminal",
                     log: list | None = None) -> tuple[dict, int]:
    """One *tool* call through the remote file-RPC poller (modal/daytona/vercel/ssh
    transport); returns the decoded res_ payload and the size of the shipped stdin."""
    import base64
    import threading
    from tools import code_execution_rpc

    monkeypatch.setattr(code_execution_rpc, "_default_dispatch",
                        lambda task_id: lambda name, args: json.dumps(tool_result, ensure_ascii=False))
    stop, shipped, polls = threading.Event(), {}, [0]
    request = json.dumps({"seq": 1, "tool": tool, "token": "tok", "args": {"command": "cat big"}})

    class Env:
        def execute(self, cmd, cwd=None, timeout=None, stdin_data=None):
            if cmd.startswith("ls "):
                polls[0] += 1
                if polls[0] > 3:  # a ship that raised never answers: stop, don't spin forever
                    stop.set()
                return {"output": "" if shipped else "/rpc/req_000001\n", "returncode": 0}
            if cmd.startswith("cat "):
                return {"output": request, "returncode": 0}
            if "base64 -d" in cmd:
                shipped["stdin"] = stdin_data
                stop.set()
            return {"output": "", "returncode": 0}

    code_execution_rpc._rpc_poll_loop(Env(), "/rpc", "t-file-rpc", [] if log is None else log,
                                      [0], 50, frozenset({tool}), stop, "tok")
    assert "stdin" in shipped, "the poller never shipped a result"
    payload = shipped["stdin"]
    return json.loads(base64.b64decode(payload).decode("utf-8")), len(payload)


def test_remote_file_rpc_ships_a_lone_surrogate(monkeypatch):
    """read_file decodes undecodable bytes with surrogateescape; strict UTF-8 refused the
    result, so the poller raised and never answered. It now arrives as the same text."""
    content = "head \udc80 tail \ud800"
    r, _ = _file_rpc_result(monkeypatch, {"content": content, "total_lines": 1}, tool="read_file")
    assert r == {"content": content, "total_lines": 1}


def test_remote_terminal_cap_measures_what_ships(tmp_path, monkeypatch):
    """The encoded cap measures the bytes _remote_write ships: a surrogate costs a 6-byte
    escape there, not the 3 bytes surrogatepass counted, so this result stays windowed
    (surrogatepass: 650 KB whole, inlined; shipped: 1.1 MB whole, 900 KB windowed)."""
    spill = tmp_path / "out.log"
    spill.write_text("s" * 200_000)
    r, shipped = _file_rpc_result(monkeypatch, {
        "output": WINDOW, "exit_code": 0, "error": None, "full_output_path": str(spill),
        "command": "\udc80" * 150_000})
    assert r["output"] == WINDOW
    assert r["truncated"] is True
    assert r["command"] == "\udc80" * 150_000
    assert shipped < 2 * 1024 * 1024


def test_remote_file_rpc_bounds_a_large_non_terminal_result(monkeypatch):
    """Not only terminal(): any file-RPC result over the cap becomes an error naming its
    size instead of a multi-MB payload embedded in a heredoc command."""
    log: list = []
    r, shipped = _file_rpc_result(monkeypatch, {"content": "x" * (3 * 1024 * 1024)},
                                  tool="read_file", log=log)
    assert set(r) == {"error", "result_bytes", "limit_bytes"}
    assert r["result_bytes"] > r["limit_bytes"] == 1024 * 1024
    assert "read_file result is" in r["error"] and "offset/limit" in r["error"]
    assert shipped < 2 * 1024 * 1024
    assert log[-1]["tool"] == "read_file" and "limit" in log[-1]["error"]
    # A result within the cap ships unchanged.
    r, _ = _file_rpc_result(monkeypatch, {"content": "x" * 1000}, tool="read_file")
    assert r == {"content": "x" * 1000}


def test_local_socket_ships_a_lone_surrogate():
    """The local socket transport encoded strictly too; the surrogate now round-trips."""
    import socket
    import threading
    from tools.code_execution_rpc import _rpc_server_loop

    content = "a \udc9f b"
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()
    t = threading.Thread(target=_rpc_server_loop, args=(
        server, "t-sock", [], [0], 50, frozenset({"read_file"}), stop, "tok"),
        kwargs={"dispatch": lambda name, args: json.dumps({"content": content}, ensure_ascii=False)},
        daemon=True)
    t.start()
    try:
        with socket.create_connection(server.getsockname(), timeout=10) as client:
            client.sendall(json.dumps({"tool": "read_file", "token": "tok", "args": {}}).encode() + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = client.recv(65536)
                assert chunk, "server closed without answering"
                buf += chunk
        assert json.loads(buf.decode("utf-8")) == {"content": content}
    finally:
        stop.set()
        t.join(10)
        server.close()


def test_remote_file_rpc_caps_shipped_full_output(tmp_path, monkeypatch):
    """A file-RPC result travels base64 through env.execute stdin (Modal's legacy path caps
    a stdin buffer at 2 MB): a big spill stays on disk, reported truncated with its path."""
    spill = tmp_path / "out.log"
    spill.write_text("x" * (3 * 1024 * 1024))
    r, shipped = _file_rpc_result(monkeypatch, {
        "output": WINDOW, "exit_code": 0, "error": None,
        "output_total_chars": 3 * 1024 * 1024, "full_output_path": str(spill)})
    assert r["output"] == WINDOW
    assert r["truncated"] is True
    assert r["full_output_path"] == str(spill)
    assert shipped < 2 * 1024 * 1024
    # The local socket transport still hands code the whole spill.
    assert _rpc({"output": WINDOW, "exit_code": 0, "error": None,
                 "full_output_path": str(spill)})["truncated"] is False


def test_remote_file_rpc_ships_small_full_output(tmp_path, monkeypatch):
    spill = tmp_path / "out.log"
    spill.write_text("A" * 1000 + "MIDDLE" + "B" * 1000)
    r, _ = _file_rpc_result(monkeypatch, {
        "output": WINDOW, "exit_code": 0, "error": None, "full_output_path": str(spill)})
    assert r["output"] == spill.read_text()
    assert r["truncated"] is False


def test_remote_file_rpc_bounds_the_escaped_result(tmp_path, monkeypatch):
    """The cap bounds the shipped JSON, not the raw spill: newline-dense text doubles under
    json escaping, so a spill under 1 MiB on disk can still overflow the remote result."""
    spill = tmp_path / "out.log"
    spill.write_text("1\n" * 400_000)  # 800 KB raw, ~1.2 MB once each newline is escaped
    r, shipped = _file_rpc_result(monkeypatch, {
        "output": WINDOW, "exit_code": 0, "error": None, "full_output_path": str(spill)})
    assert r["output"] == WINDOW
    assert r["truncated"] is True
    assert r["full_output_path"] == str(spill)
    assert shipped < 2 * 1024 * 1024


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
