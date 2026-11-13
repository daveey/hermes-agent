"""Host-side RPC servers for execute_code sandboxes.

Two transports share one request pipeline (token check → allow-list → call
budget → dispatch under output silence → log): ``_rpc_server_loop`` serves the
local UDS/TCP socket, ``_rpc_poll_loop`` polls a remote filesystem for request
files via ``env.execute()``.
"""

import base64
import json
import logging
import secrets
import shlex
import socket
import threading
import time
from pathlib import Path

from agent.thread_scoped_output import thread_scoped_silence
from tools.code_kernel_scope import scope as _local_scope
from tools.registry import tool_error

# Logger name kept as the origin module's so existing log expectations hold.
logger = logging.getLogger("tools.code_execution_tool")

# Terminal parameters that must not be used from ephemeral sandbox scripts.
_TERMINAL_BLOCKED_PARAMS = {"background", "pty", "notify", "notify_on_complete", "watch_patterns", "heartbeat", "persist_on_release"}


def _default_dispatch(task_id):
    from model_tools import handle_function_call
    from tools.file_tools_read_tracking import programmatic_file_read

    def dispatch(tool_name, tool_args):
        with programmatic_file_read():
            return handle_function_call(tool_name, tool_args, task_id=task_id)
    return dispatch


def _private_dirs_cmd(root: str, *subdirs: str) -> str:
    """Shell command creating *root* (and optional *subdirs* under it) owner-only
    on a shared host. ``umask 077`` makes intermediates and leaves private at
    creation (no mkdir-then-chmod window where a co-tenant could open a dir fd);
    ``chmod`` then repairs any named dir that already existed with permissive
    modes."""
    mkdir = " ".join(shlex.quote(d) for d in (subdirs or (root,)))
    chmod = " ".join(shlex.quote(d) for d in (root, *subdirs))
    return f"umask 077 && mkdir -p {mkdir} && chmod 700 {chmod}"


def _execute_checked(env, cmd: str, what: str, *, timeout: int, **kwargs) -> dict:
    """Run *cmd* from ``/`` and raise ``RuntimeError`` on a non-zero exit.

    Used where a silent failure would ship secrets or code into a missing,
    half-written, or still-permissive remote path. The error carries the
    command output only, never the payload."""
    result = env.execute(cmd, cwd="/", timeout=timeout, **kwargs)
    if result.get("returncode", 1) != 0:
        raise RuntimeError(f"{what} failed: {result.get('output')!r}")
    return result


def _wire_bytes(text: str) -> bytes:
    """The UTF-8 bytes an RPC transport ships for a JSON-encoded RPC result *text*, and what
    a size cap must measure. Tool results can hold lone surrogates (file reads decode
    undecodable bytes with surrogateescape), which strict UTF-8 refuses: each becomes its
    ``\\uXXXX`` escape, which inside a JSON string (where ``json.dumps`` puts every one)
    decodes back to the same text (a lone surrogate exactly; a high+low pair, see below).
    Only for RPC results: a shipped file (script, env file) stays strict, since there the
    escape would be literal text."""
    # Exact for lone surrogates only: a high+low pair held as two code points ships as two
    # escapes that json.loads joins into one astral char. A file read never holds such a
    # pair (surrogateescape yields only low surrogates, U+DC80..U+DCFF), but an MCP or
    # plugin tool result could. The remote str then holds one char where this side held two:
    # the UTF-16 meaning survives, the code-point sequence (and len) does not.
    return text.encode("utf-8", "backslashreplace")


def _remote_write(env, remote_path: str, content: str, *, atomic: bool = False,
                  timeout: int = 30, check: bool = False, rpc_result: bool = False):
    """Write *content* owner-only to *remote_path*; returns the execute() result
    (``check=True`` raises on failure via _execute_checked). ``rpc_result=True`` (a JSON
    RPC response) ships lone surrogates as escapes via _wire_bytes; anything else is
    strict UTF-8, so a lone surrogate in a shipped file raises before anything is written.

    The base64 payload always travels as ``stdin_data``: pipe-mode backends
    (ssh, docker, local: the real shared-host ones) deliver it on
    real stdin, so it never enters argv where a co-tenant can read it via
    ``/proc/*/cmdline``; ``BaseEnvironment.execute`` embeds it as a heredoc
    for heredoc-mode backends (modal, daytona, vercel), and managed_modal
    forwards it as ``stdinData``, the same contract ``_write_to_sandbox``
    relies on."""
    raw = _wire_bytes(content) if rpc_result else content.encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    target = shlex.quote(remote_path)
    write = (f"base64 -d > {target}.tmp && mv -f {target}.tmp {target}"
             if atomic else f"base64 -d > {target}")
    cmd = f"umask 077 && {write}"
    if check:
        return _execute_checked(env, cmd, f"remote file ship for {remote_path!r}",
                                timeout=timeout, stdin_data=encoded)
    return env.execute(cmd, cwd="/", timeout=timeout, stdin_data=encoded)


def _rpc_token_ok(request: dict, rpc_token: str) -> bool:
    """Constant-time token check; an empty server token fails closed. Compared as bytes:
    compare_digest raises TypeError on a non-ASCII str, and the token is script-supplied JSON."""
    return bool(rpc_token) and secrets.compare_digest(
        str(request.get("token") or "").encode(), rpc_token.encode()
    )


def _handle_rpc_request(request: dict, **kwargs) -> str:
    """Local-work seam: reserve FIRST (before allow-list/budget/dispatch, so a late callback
    invalidates quiet before any processing, refusals included); release in finally."""
    with _local_scope("tool_dispatch", "code-execution-rpc"):
        return _handle_rpc_request_body(request, **kwargs)


def _handle_rpc_request_body(request: dict, *, allowed_tools: frozenset, tool_call_counter: list,
                             max_tool_calls: int, dispatch, tool_call_log: list, call_start: float,
                             where: str, full_output_cap: int | None = None) -> str:
    """Enforce allow-list + budget, then dispatch one authenticated request. Only a dispatched
    call consumes budget and is logged; refusals are free. ``full_output_cap`` (the remote
    file-RPC transport) bounds the shipped bytes of every result: a terminal() result inlines
    its spill only within it, and any other result over it becomes an error naming its size
    (default: the local socket, which caps only the spill a terminal() result reads)."""
    tool_name = request.get("tool", "")
    tool_args = request.get("args", {})
    if tool_name not in allowed_tools:
        return tool_error(f"Tool '{tool_name}' is not available in execute_code. "
                          f"Available: {', '.join(sorted(allowed_tools))}")
    if tool_call_counter[0] >= max_tool_calls:
        return tool_error(f"Tool call limit reached ({max_tool_calls}). "
                          "No more tool calls allowed in this execution.")
    if tool_name == "terminal" and isinstance(tool_args, dict):
        for param in _TERMINAL_BLOCKED_PARAMS:
            tool_args.pop(param, None)
    # Silence handler status prints so they don't leak into the CLI spinner.
    try:
        with thread_scoped_silence():
            result = dispatch(tool_name, tool_args)
    except Exception as exc:
        logger.error("Tool call failed in %s: %s", where, exc, exc_info=True)
        result = tool_error(str(exc))
    tool_call_counter[0] += 1
    if tool_name == "terminal":
        result = (_complete_terminal_output(result) if full_output_cap is None else
                  _complete_terminal_output(result, full_output_cap, encoded_cap=full_output_cap))
    if full_output_cap is not None and isinstance(result, str):
        size = len(_wire_bytes(result))
        if size > full_output_cap:
            # The call already ran: say so, or a side-effecting tool (patch, write_file) with a
            # big result reads as failed and invites a retry of a change that landed.
            result = tool_error(
                f"{tool_name} ran, but its result is {size:,} bytes, over the "
                f"{full_output_cap:,}-byte limit of this remote sandbox's tool transport, so "
                "only its result was dropped: if the call changed anything, that change stands; "
                "do not repeat it to get the result. To read data, ask for less (read_file "
                "offset/limit, a narrower search_files pattern or limit, a shorter web_extract "
                "char_limit)",
                result_bytes=size, limit_bytes=full_output_cap)
    entry = {"tool": tool_name, "args_preview": str(tool_args)[:80],
             "duration": round(time.monotonic() - call_start, 2)}
    error = _result_error(result)
    if error:
        entry["error"] = error
    tool_call_log.append(entry)
    return result


_FULL_OUTPUT_READ_CAP_BYTES = 20 * 1024 * 1024
# The file-RPC poller ships each result base64 (+33%) through env.execute stdin: Modal's legacy
# server path caps one stdin buffer at 2 MB and Daytona/Vercel stage it as an uploaded file. Every
# remote result stays within 1 MiB of shipped UTF-8 (json escaping can double newline-dense text),
# so its base64 stays under ~1.4 MB: a terminal() result over it keeps the window, truncated, with
# its spill path; any other result over it becomes an error naming its size. One cap covers every
# file-RPC backend, including ssh/docker pipe mode, which has no 2 MB stdin limit but gets no
# backend-specific branch.
_REMOTE_FULL_OUTPUT_READ_CAP_BYTES = 1024 * 1024
_SPILL_CAPPED_MARK = "[spill capped at "


def _read_full_output(path, cap: int = _FULL_OUTPUT_READ_CAP_BYTES) -> str | None:
    """The terminal spill file's text when it holds the WHOLE output, else None (missing,
    unreadable, over *cap* bytes, or itself capped by the collector)."""
    try:
        spill = Path(str(path))
        if spill.stat().st_size > cap:
            return None
        text = spill.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return None
    return None if _SPILL_CAPPED_MARK in text[-200:] else text.strip()


def _looks_windowed(body: dict) -> bool:
    """True when a terminal result's ``output`` is a cut window even though no spill file
    came with it: it carries the truncation marker, or the reported total exceeds it."""
    from tools.tool_output_truncate import contains_truncation_notice
    output = body["output"]
    total = body.get("output_total_chars")
    if isinstance(total, int) and not isinstance(total, bool) and total > len(output):
        return True
    return bool(body.get("truncation_note")) or contains_truncation_notice(output)


def _complete_terminal_output(result, cap: int = _FULL_OUTPUT_READ_CAP_BYTES, *,
                              encoded_cap: int | None = None):
    """Give code the complete stdout of a sandbox ``terminal()`` call. The terminal tool
    renders a head+tail window sized for the chat; a script that writes ``output`` to disk
    as data must not get that window (defect agent-7ac9c5fa93: a checksum manifest shipped
    the truncation marker). The spill file is already ANSI-stripped and redacted by the
    terminal tool. ``truncated`` tells the script whether ``output`` is whole: without a
    readable spill (managed_modal, artifacts off, a failed spill redaction, a suffix that
    pushed the text over the limit) a windowed ``output`` still reports ``truncated: True``.
    When the spill replaces the window, notes the terminal tool appended after collection
    (timeout/interrupt text, the sudo notice, a transform hook's rewrite) are not carried;
    ``exit_code`` and ``exit_code_meaning`` still report a timeout or interrupt. With
    *encoded_cap*, a spill-backed result whose UTF-8 JSON exceeds it keeps the window."""
    try:
        body = json.loads(result)
    except (TypeError, ValueError):
        return result
    if not isinstance(body, dict) or not isinstance(body.get("output"), str):
        return result
    if body.get("approval_pending") or body.get("status") == "pending_approval":
        return result  # the command never ran: keep the approval contract's exact shape
    spill_path = body.get("full_output_path")
    full = _read_full_output(spill_path, cap) if spill_path else None
    if full is not None:
        whole = dict(body, output=full, truncated=False)
        whole.pop("truncation_note", None)
        text = json.dumps(whole, ensure_ascii=False)
        if encoded_cap is None or len(_wire_bytes(text)) <= encoded_cap:
            return text
    body["truncated"] = bool(spill_path) or _looks_windowed(body)
    return json.dumps(body, ensure_ascii=False)


def _result_error(result) -> str:
    """The ``error`` text of a JSON-object tool result, else ``""``."""
    if not isinstance(result, str) or not result.startswith("{") or '"error"' not in result:
        return ""
    try:
        body = json.loads(result)
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error)[:300] if error else ""


def tool_errors_since(tool_call_log: list, start: int = 0) -> list:
    """Failed in-script tool calls since *start*, for the execute_code result: a script that
    ignores a helper's ``{"error": ...}`` return would otherwise report plain success while
    the write/patch it relied on never happened."""
    return [{"tool": e["tool"], "error": e["error"]} for e in tool_call_log[start:] if e.get("error")][:5]


def _rpc_server_loop(server_sock: socket.socket, task_id: str, tool_call_log: list,
                     tool_call_counter: list, max_tool_calls: int, allowed_tools: frozenset,
                     stop_event: threading.Event, rpc_token: str, dispatch=None):
    """Accept one client and serve newline-delimited JSON requests until it disconnects, idles
    300s, or the call limit is reached. ``tool_call_counter`` is a mutable ``[int]``. ``dispatch``
    overrides how an allowed, budgeted call runs: per-call sandboxes use the default (the thread
    carries the cell's context); session kernels rebind each call to the CURRENT cell's authority.
    """
    if dispatch is None:
        dispatch = _default_dispatch(task_id)
    conn = None
    try:
        server_sock.settimeout(0.05)
        while not stop_event.is_set():
            try:
                conn, _ = server_sock.accept()
                break
            except TimeoutError:
                continue
        if conn is None:
            return
        conn.settimeout(300)
        buf = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                call_start = time.monotonic()
                # Reserve before parse/auth: even refused late requests invalidate quiet.
                with _local_scope("tool_dispatch", "code-execution-rpc-socket"):
                    try:
                        request = json.loads(line.decode())
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        resp = tool_error(f"Invalid RPC request: {exc}")
                    else:
                        resp = _handle_rpc_request(
                            request, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
                            max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
                            call_start=call_start, where="sandbox",
                        ) if _rpc_token_ok(request, rpc_token) else tool_error("Unauthorized RPC request")
                conn.sendall(_wire_bytes(resp + "\n"))
    except TimeoutError:
        logger.debug("RPC listener socket timeout")
    except OSError as e:
        logger.debug("RPC listener socket error: %s", e, exc_info=True)
    finally:
        if conn:
            try:
                conn.close()
            except OSError as e:
                logger.debug("RPC conn close error: %s", e)


def _rpc_poll_loop(env, rpc_dir: str, task_id: str, tool_call_log: list, tool_call_counter: list,
                   max_tool_calls: int, allowed_tools: frozenset, stop_event: threading.Event,
                   rpc_token: str):
    """Poll the remote filesystem for request files and answer them. Background thread; each
    ``env.execute()`` is an independent process, so this is safe alongside the script-execution
    thread. Malformed or unauthorized requests are removed without a response."""
    dispatch = _default_dispatch(task_id)
    poll_interval = 0.1
    quoted_rpc_dir = shlex.quote(rpc_dir)
    while not stop_event.is_set():
        try:
            ls_result = env.execute(f"ls -1 {quoted_rpc_dir}/req_* 2>/dev/null || true", cwd="/", timeout=10)
            output = ls_result.get("output", "").strip()
            if not output:
                stop_event.wait(poll_interval)
                continue
            req_files = sorted(f for f in (line.strip() for line in output.split("\n"))
                               if f and not f.endswith(".tmp") and "/req_" in f)
            for req_file in req_files:
                if stop_event.is_set():
                    break
                call_start = time.monotonic()
                quoted_req_file = shlex.quote(req_file)
                read_result = env.execute(f"cat {quoted_req_file}", cwd="/", timeout=10)
                try:
                    request = json.loads(read_result.get("output", ""))
                except (json.JSONDecodeError, ValueError):
                    logger.debug("Malformed RPC request in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                if not _rpc_token_ok(request, rpc_token):
                    logger.debug("Unauthorized RPC request in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                seq = request.get("seq", 0)
                if not isinstance(seq, int):
                    # A non-int seq cannot form the res_NNNNNN name the caller
                    # polls; formatting it after dispatch would raise, leave the
                    # request in place, and replay the tool call every cycle.
                    logger.debug("RPC request with malformed seq in %s", req_file)
                    env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
                    continue
                tool_result = _handle_rpc_request(
                    request, allowed_tools=allowed_tools, tool_call_counter=tool_call_counter,
                    max_tool_calls=max_tool_calls, dispatch=dispatch, tool_call_log=tool_call_log,
                    call_start=call_start, where="remote sandbox",
                    full_output_cap=_REMOTE_FULL_OUTPUT_READ_CAP_BYTES,
                )
                # Atomic (tmp + rename) and owner-only; results carry tool output
                # on a shared-host backend.
                _remote_write(env, f"{rpc_dir}/res_{seq:06d}", tool_result,
                              atomic=True, timeout=60, rpc_result=True)
                env.execute(f"rm -f {quoted_req_file}", cwd="/", timeout=5)
        except Exception as e:
            if not stop_event.is_set():
                logger.debug("RPC poll error: %s", e, exc_info=True)
        if not stop_event.is_set():
            stop_event.wait(poll_interval)
