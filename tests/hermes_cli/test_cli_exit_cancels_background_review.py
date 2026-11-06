"""The classic CLI owns its post-turn background review: exit cancels it, single-query runs never fork it.

Observed 2026-10-29 20:16 PDT (session 20261029_201245_dfaf9b, core cb1aa70f): the foreground turn
answered, the review fork spawned, CLI cleanup ran memory shutdown, then the review's first API call
returned and its ``skill_view`` call failed with ``cannot schedule new futures after interpreter
shutdown``. The review is a daemon ``bg-review`` thread. The TUI cancels it at session finalize
(#102895) and serve fences it during retirement, but the CLI exit path did neither, so the process
exited underneath a review that was still dispatching tools. On hermes-hub, 142 of 142 CLI review
forks in one 4.6 h window never completed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# A parent agent with a live review fork, an in-flight "API call" that returns after the CLI has
# exited, and then the fork's first tool dispatch through a real pool, which is where the
# reported failure happened. An atexit handler keeps finalization running past the API return, the
# way the real CLI's MCP and terminal teardown did (4.4 s on the MacBook).
_SCRIPT = textwrap.dedent(
    """
    import atexit, sys, threading, time
    sys.path.insert(0, {repo!r})
    # Warm the real tool executor before exit, matching a session that already ran tools.
    # Its inherited submit then reproduces "cannot schedule new futures", rather than a
    # lazy concurrent.futures import failing with "can't register atexit after shutdown".
    from tools.daemon_pool import DaemonThreadPoolExecutor
    import cli
    from agent.background_review import finish_background_review_run, prepare_background_review_run

    class Fork:
        def __init__(self):
            self.interrupted = threading.Event()
        def hard_interrupt(self, message=None, tool_reason=None):
            print("FORK_INTERRUPTED", tool_reason, "|", message, flush=True)
            self.interrupted.set()

    class Parent:
        session_id = "s-review-exit"
        _session_messages = []
        def shutdown_memory_provider(self, *_a):
            pass

    parent, fork = Parent(), Fork()
    parent._background_review_lock = threading.Lock()
    run = prepare_background_review_run(parent)
    started = threading.Event()

    def review():
        try:
            assert run.begin_request(fork)
            started.set()
            if fork.interrupted.wait(1.0):  # the in-flight API call; returns after main exits
                print("REVIEW_STOPPED_BEFORE_TOOLS", flush=True)
                return
            try:
                with DaemonThreadPoolExecutor(1) as pool:
                    pool.submit(lambda: None).result()
                print("TOOL_DISPATCHED", flush=True)
            except RuntimeError as exc:
                print("TOOL_REFUSED", exc, flush=True)
        finally:
            finish_background_review_run(parent, run)

    atexit.register(time.sleep, 2.5)
    threading.Thread(target=review, daemon=True, name="bg-review").start()
    started.wait(5)
    cli._active_agent_ref = parent
    for step, _exc in cli._CLEANUP_STEPS:  # resource teardown is not under test here
        setattr(cli, step, lambda: None)
    cli._run_cleanup(notify_session_finalize=False)
    print("CLI_EXITED", flush=True)
    """
)


def test_cli_exit_cancels_in_flight_review_before_it_dispatches_a_tool(tmp_path):
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_EXIT_WATCHDOG_S": "0"}
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(repo=str(REPO))],
        capture_output=True, text=True, timeout=120, check=False, env=env, cwd=str(tmp_path),
    )
    out = proc.stdout + proc.stderr
    assert "CLI_EXITED" in proc.stdout, out
    assert "TOOL_REFUSED" not in out, out
    assert "TOOL_DISPATCHED" not in out, out
    assert "REVIEW_STOPPED_BEFORE_TOOLS" in proc.stdout, out
    assert "FORK_INTERRUPTED session ended | session ended (cli exit)" in proc.stdout, out
    # The cancel ran before teardown: the fork was interrupted before the CLI reported exit.
    assert proc.stdout.index("FORK_INTERRUPTED") < proc.stdout.index("CLI_EXITED"), out


def test_cli_exit_without_a_review_is_a_no_op(monkeypatch):
    import cli as cli_mod

    parent = types.SimpleNamespace(session_id="s", _session_messages=[], shutdown_memory_provider=lambda *_a: None)
    monkeypatch.setattr(cli_mod, "_active_agent_ref", parent)
    cli_mod._cancel_background_review_on_exit()  # no review attributes at all: must not raise
    monkeypatch.setattr(cli_mod, "_active_agent_ref", None)
    cli_mod._cancel_background_review_on_exit()


def _cli_capturing_agent_kwargs(monkeypatch, *, single_query: bool):
    import cli as cli_mod
    from hermes_cli import gateway_agent

    cli = cli_mod.HermesCLI(compact=True)
    cli._session_db = object()
    cli._resumed = False
    cli.conversation_history = []
    cli._install_tool_callbacks = lambda: None
    cli._ensure_runtime_credentials = lambda: True
    cli._ensure_gateway_session = lambda: None  # no gateway here: the kwargs are the contract
    if single_query:
        cli._single_query_mode = True
    captured = {}

    def _fake_agent(*_a, **kw):
        captured.update(kw)
        return types.SimpleNamespace()

    # _init_agent builds the gateway-backed agent (hermes_cli.gateway_agent.GatewayAgent), not run_agent.AIAgent.
    monkeypatch.setattr(gateway_agent, "GatewayAgent", _fake_agent)
    assert cli._init_agent() is True
    return captured


def test_single_query_cli_agent_skips_background_review(monkeypatch):
    assert _cli_capturing_agent_kwargs(monkeypatch, single_query=True).get("skip_background_review") is True


def test_interactive_cli_agent_keeps_background_review(monkeypatch):
    assert not _cli_capturing_agent_kwargs(monkeypatch, single_query=False).get("skip_background_review")
