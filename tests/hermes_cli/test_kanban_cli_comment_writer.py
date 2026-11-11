"""``hermes kanban comment`` records who ran it: an agent's tool subprocess or a person. [daveey]

Regression: ``_cmd_comment`` hardcoded ``writer_kind='human'``, so an agent's terminal running
``hermes kanban comment`` (a chat agent's terminal tool, coach plugins spawned by the gateway,
execute_code scripts, other harnesses) reached workers as "from the operator". The signal is the
agent-harness marker in the start-time environment of the CLI process or any ancestor; no input
asserts human, and an inconclusive walk is not human.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import kanban
from hermes_cli import kanban_comment_writer as kcw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
import tools.kanban_tools as kt
from tests.tools import test_kanban_comment_injection as inj
from tests.waiting import wait_until

worker_home = inj.worker_home
AGENT_HEADER = "on your kanban task from another agent session (delivered mid-run)"
OPERATOR_HEADER = "on your kanban task from the operator (delivered mid-run)"
Table = dict  # pid -> (ppid, start-time environ or None for another user's unreadable process)


def _tree(procs: Table):
    def read(pid):
        if pid not in procs:
            raise kcw.Inconclusive(f"pid {pid} gone")
        return procs[pid]
    return read


def _me(env, ppid: int, rest: Table):
    return _tree({os.getpid(): (ppid, env), **rest})


# A person's login shell (macOS Terminal / ssh): no process carries a marker; root's login/sshd and
# launchd/init are another user's (unreadable, skipped).
HUMAN_SHELL: Table = {500: (400, {"HOME": "/Users/d", "SHELL": "/bin/zsh"}), 400: (1, None), 1: (0, None)}
# The terminal tool's shell: started by the Hermes agent with the harness markers (and the chat's
# session id); the agent process itself was launched by launchd with none.
AGENT_TERMINAL: Table = {
    600: (550, {"HERMES_AGENT": "true", "AI_AGENT": "hermes-agent", "HERMES_SESSION_ID": "sess-a"}),
    550: (1, {"HOME": "/Users/d"}), 1: (0, None)}


def _payload(tid: str, cid: int) -> dict:
    with kbc.connect_closing() as conn:
        for e in kb.list_events(conn, tid):
            if e.kind == "commented" and (e.payload or {}).get("comment_id") == cid:
                return e.payload
    raise AssertionError(f"no commented event for comment {cid}")


def _task() -> str:
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="t")


def _last_comment(tid: str) -> int:
    with kbc.connect_closing() as conn:
        return kb.list_comments(conn, tid)[-1].id


def _cli_comment(tid: str, text: str) -> int:
    """``hermes kanban comment <tid> <text>`` through the real CLI parser and dispatcher."""
    parser = argparse.ArgumentParser(prog="hermes")
    kanban.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "comment", tid, *text.split()])
    assert kanban.kanban_command(args) == 0
    return _last_comment(tid)


# --- the signal ---------------------------------------------------------------------------------

@pytest.mark.parametrize("marker", kcw.AGENT_ENV_MARKERS)
def test_each_marker_marks_an_agent(marker):
    assert kcw.env_agent_marker({marker: "x"}) == marker
    assert kcw.env_agent_marker({marker: "  "}) is None
    assert kcw.env_agent_marker({"HOME": "/h"}) is None


def test_human_shell_has_no_agent_ancestor():
    assert kcw.agent_ancestor(read=_me({"HOME": "/h"}, 500, HUMAN_SHELL)) is None


def test_agent_terminal_is_found_through_the_parent_shell():
    # The CLI's own start env lacks the markers (``env -i`` / ``unset`` inside the agent's shell):
    # the shell that ran it still started with them.
    assert kcw.agent_ancestor(read=_me({"PATH": "/usr/bin"}, 600, AGENT_TERMINAL)) == "600:HERMES_SESSION_ID"


def test_gateway_service_descendant_is_an_agent():
    # A plugin subprocess of the systemd/launchd gateway (coach notes), even with a scrubbed env.
    gw = {700: (1, {"HERMES_SUPERVISED_CHILD": "1", "HERMES_HOME": "/h"}), 1: (0, None)}
    assert kcw.agent_ancestor(read=_me({}, 700, gw)) == "700:HERMES_SUPERVISED_CHILD"


@pytest.mark.parametrize("table", [
    {10: (11, {}), 11: (10, {})},                    # loop
    {10: (99, {})},                                  # an ancestor vanished mid-walk
], ids=["loop", "vanished"])
def test_an_incomplete_walk_is_not_human(table):
    with pytest.raises(kcw.Inconclusive):
        kcw.agent_ancestor(read=_me({}, 10, table))


def test_depth_cap_is_not_human(monkeypatch):
    monkeypatch.setattr(kcw, "MAX_ANCESTRY", 3)
    with pytest.raises(kcw.Inconclusive):
        kcw.agent_ancestor(read=_me({}, 10, {10: (11, {}), 11: (12, {}), 12: (1, {}), 1: (0, None)}))


def test_inconclusive_records_agent():
    def broken(pid):
        raise kcw.Inconclusive("psutil unavailable")
    kind, _session, evidence = kcw.cli_comment_writer(read=broken, env={"HERMES_AGENT": "true"})
    assert kind == "agent" and evidence.startswith("inconclusive:")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids")
def test_same_user_unreadable_process_is_inconclusive(monkeypatch):
    """psutil: a process that is not plainly this user's is skipped; this user's is inconclusive."""
    import psutil

    class P:
        def __init__(self, uids):
            self._uids = uids

        def ppid(self):
            return 1

        def environ(self):
            raise psutil.AccessDenied(4242)

        def uids(self):
            return self._uids

    uid = os.getuid()  # windows-footgun: ok (POSIX-only test, skipped elsewhere)
    if uid == 0:
        pytest.skip("root reads every environ")
    table = {4242: (uid, uid, uid), 1: (0, 0, 0), 77: (uid, 0, 0),  # 77: macOS setuid `login`
             88: (uid + 1, uid + 1, uid + 1)}  # 88: a third user's process (`sudo -u other`)
    monkeypatch.setattr(psutil, "Process", lambda pid: P(table[pid]))
    monkeypatch.setattr(kcw.os, "stat", lambda path: SimpleNamespace(st_uid=uid))
    with pytest.raises(kcw.Inconclusive):
        kcw._read_proc(4242)
    assert kcw._read_proc(1) == (1, None)
    assert kcw._read_proc(77) == (1, None)
    # Only root is foreign: from `sudo -u other`, the agent's own processes are a third uid's, and
    # skipping them would walk past the agent to a clean init.
    monkeypatch.setattr(kcw.os, "stat", lambda path: SimpleNamespace(st_uid=uid + 1))
    with pytest.raises(kcw.Inconclusive):
        kcw._read_proc(88)
    # Linux non-dumpable same-uid process (sshd-session): /proc/<pid>/environ is root's.
    monkeypatch.setattr(kcw.os, "stat", lambda path: SimpleNamespace(st_uid=0 if "4242" in path else uid))
    assert kcw._read_proc(4242) == (1, None)


NONDUMPABLE_PARENT = """
import ctypes, subprocess, sys
assert ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0  # PR_SET_DUMPABLE = 0
child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
print(child.pid, flush=True)
sys.stdin.readline()
child.communicate(b"", timeout=30)
"""


@pytest.mark.skipif(not sys.platform.startswith("linux") or getattr(os, "geteuid", lambda: 0)() == 0,
                    reason="Linux non-dumpable semantics; root reads every environ")
def test_human_shell_under_a_real_nondumpable_parent_is_human():
    """A person's ssh shell: its parent ``sshd-session`` runs as the user but is non-dumpable, so its
    environ can't be read. That parent is skipped, not inconclusive, and the walk stays human."""
    clean = {k: v for k, v in os.environ.items() if not kcw.env_agent_marker({k: v})}
    parent = subprocess.Popen([sys.executable, "-c", NONDUMPABLE_PARENT], env=clean, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert parent.stdout is not None
        child = int(parent.stdout.readline())
        wait_until(lambda: _is_python(child), timeout=30)
        assert kcw._read_proc(parent.pid) == (os.getpid(), None)
        assert kcw.agent_ancestor(child, read=_bounded_read) is None
    finally:
        parent.communicate("\n", timeout=30)


def _bounded_read(pid):
    """The real reader, stopped at this test process (whose own ancestry may be an agent's)."""
    if pid == os.getpid():
        return 0, {}
    return kcw._read_proc(pid)


def _is_python(pid) -> bool:
    import psutil
    try:
        return "python" in " ".join(psutil.Process(pid).cmdline())
    except psutil.Error:
        return False


def test_real_process_start_env_is_read_and_later_setdefault_does_not_count():
    """Real processes: a child spawned with a marker is an agent; a child spawned without one that
    sets HERMES_AGENT in its own os.environ afterwards (what every hermes entrypoint does) is not."""
    clean = {k: v for k, v in os.environ.items() if not kcw.env_agent_marker({k: v})}
    code = "import os, sys; os.environ.setdefault('HERMES_AGENT', 'true'); sys.stdin.read()"
    human = subprocess.Popen([sys.executable, "-c", code], env=clean, stdin=subprocess.PIPE)
    agent = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                             env={**clean, "HERMES_KANBAN_WORKER_TASK": "t_x"})
    try:
        wait_until(lambda: _is_python(human.pid) and _is_python(agent.pid), timeout=30)
        assert kcw.agent_ancestor(human.pid, read=_bounded_read) is None
        assert kcw.agent_ancestor(agent.pid, read=_bounded_read) == f"{agent.pid}:HERMES_KANBAN_WORKER_TASK"
    finally:
        for p in (human, agent):
            p.communicate(b"", timeout=30)


# --- the CLI path end to end --------------------------------------------------------------------

def test_cli_comment_from_agent_terminal_records_agent(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", "sess-a")
    monkeypatch.setattr(kcw, "_read_proc", _me({"PATH": "/usr/bin"}, 600, AGENT_TERMINAL))
    tid = _task()
    p = _payload(tid, _cli_comment(tid, "Please re-check it yourself"))
    assert p["writer_kind"] == "agent"
    assert p["writer_session"] == "sess-a"


def test_cli_comment_from_human_shell_records_human(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    # In-process, os.environ has HERMES_AGENT (main() sets it for every hermes command): it must
    # not count; only the start-time environments do.
    monkeypatch.setenv("HERMES_AGENT", "true")
    monkeypatch.setattr(kcw, "_read_proc", _me({"HOME": "/h"}, 500, HUMAN_SHELL))
    tid = _task()
    p = _payload(tid, _cli_comment(tid, "go ahead"))
    assert p["writer_kind"] == "human"
    assert "writer_session" not in p


def test_typed_slash_command_is_human_inside_an_agent_process(worker_home, monkeypatch):
    """``/kanban comment`` typed into the composer runs in the (marked) agent process itself."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({"HERMES_AGENT": "true"}, 600, AGENT_TERMINAL))
    tid = _task()
    assert "Comment added" in kanban.run_slash(f"comment {tid} why are you asking me?", human_typed=True)
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "human"


def test_programmatic_run_slash_is_not_human(worker_home, monkeypatch):
    """An agent's script importing ``run_slash`` gets the ancestry check, not the typed-line pass."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({}, 600, AGENT_TERMINAL))
    tid = _task()
    assert "Comment added" in kanban.run_slash(f"comment {tid} re-check it yourself")
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "agent"


@pytest.mark.asyncio
async def test_gateway_platform_slash_comment_is_human(worker_home, monkeypatch):
    """A Telegram/Discord user's ``/kanban comment`` (the gateway process is a marked agent host)."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({"HERMES_SUPERVISED_CHILD": "1"}, 1, {1: (0, None)}))
    tid = _task()
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", thread_id=None,
                             user_id="u1")
    event = SimpleNamespace(text=f"/kanban comment {tid} stop and ask me first", source=source,
                            message_id="1", reply_to_message_id=None)
    out = await GatewayRunner._handle_kanban_command(runner, event)
    assert "Comment added" in out, out
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,is_bot,internal", [("API_SERVER", False, False), ("WEBHOOK", False, False),
                                                      ("DISCORD", True, False), ("TELEGRAM", False, True)],
                         ids=["api", "webhook", "bot-author", "internal-event"])
async def test_gateway_untyped_slash_comment_is_not_human_by_fiat(worker_home, monkeypatch, platform, is_bot,
                                                                  internal):
    """An API client's or webhook body's "/kanban comment", another bot's message or an internal event
    is not a person typing."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({"HERMES_SUPERVISED_CHILD": "1"}, 1, {1: (0, None)}))
    tid = _task()
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(platform=getattr(Platform, platform), chat_id="c1", chat_type="dm",
                             thread_id=None, user_id="u1", is_bot=is_bot)
    event = SimpleNamespace(text=f"/kanban comment {tid} approve it", source=source, internal=internal,
                            message_id="1", reply_to_message_id=None)
    out = await GatewayRunner._handle_kanban_command(runner, event)
    assert "Comment added" in out, out
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "agent"


def test_mcp_stdio_server_env_carries_the_harness_marker(monkeypatch):
    """A stdio MCP server is the agent's tool: its `hermes kanban comment` must find the marker."""
    from tools.mcp_tool_config import _build_safe_env
    monkeypatch.setenv("HERMES_AGENT", "true")
    monkeypatch.setenv("AI_AGENT", "hermes-agent")
    env = _build_safe_env(None)
    assert kcw.env_agent_marker(env) == "HERMES_AGENT"


def test_desktop_gui_launch_env_drops_the_harness_marker(monkeypatch):
    """Electron (and so the Desktop terminal pane, a person's shell) must not start with it."""
    from hermes_cli.main_desktop import _desktop_launch_env
    monkeypatch.setenv("HERMES_AGENT", "true")
    monkeypatch.setenv("AI_AGENT", "hermes-agent")
    env, _flags = _desktop_launch_env(argparse.Namespace())
    assert "HERMES_AGENT" not in env and "AI_AGENT" not in env


def test_desktop_relaunch_env_drops_agent_markers(monkeypatch):
    """Desktop reopened after an update run from an agent/backend starts its terminal pane clean."""
    from hermes_cli.top_level_env import clean_launch_env
    marked = {k: "1" for k in ("HERMES_AGENT", "AI_AGENT", "HERMES_DESKTOP", "HERMES_SUPERVISED_CHILD",
                               "HERMES_SESSION_ID")}
    assert kcw.env_agent_marker(clean_launch_env({"HOME": "/h", **marked})) is None


def test_cli_composer_slash_comment_is_human(worker_home, monkeypatch):
    from hermes_cli.cli_commands_mixin import CLICommandsMixin
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({"HERMES_AGENT": "true"}, 600, AGENT_TERMINAL))
    tid = _task()
    CLICommandsMixin._handle_kanban_command(object(), f"/kanban comment {tid} typed at the composer")
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "human"


@pytest.mark.parametrize("forge", [["--writer-kind", "human"], ["--human"], ["--_slash_typed"],
                                   ["--human-typed"]])
def test_no_cli_input_asserts_human(forge):
    parser = argparse.ArgumentParser(prog="hermes")
    kanban.build_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "comment", "t_1", "hi", *forge])


def test_agent_cli_note_reaches_worker_as_another_agent(worker_home, monkeypatch):
    tid, _run = inj._running_worker(monkeypatch)
    agent = inj.FakeAgent()
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is False  # seed
    monkeypatch.setattr(kcw, "_read_proc", _me({}, 600, AGENT_TERMINAL))
    inj._as_other_session(lambda: _cli_comment(tid, "re-check (c) yourself, then complete"))
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    note = agent.steers[0]
    assert note.startswith("New note " + AGENT_HEADER), note
    assert "from the operator" not in note


def test_human_cli_note_reaches_worker_from_the_operator(worker_home, monkeypatch):
    tid, _run = inj._running_worker(monkeypatch)
    agent = inj.FakeAgent()
    inj._unthrottle()
    kt.inject_new_comments_from_env(agent)  # seed
    monkeypatch.setattr(kcw, "_read_proc", _me({}, 500, HUMAN_SHELL))
    inj._as_other_session(lambda: _cli_comment(tid, "stop the burners"))
    inj._unthrottle()
    assert kt.inject_new_comments_from_env(agent) is True
    assert agent.steers[0].startswith("New note " + OPERATOR_HEADER), agent.steers[0]


def test_real_cli_subprocess_in_terminal_tool_shell_records_agent(worker_home):
    """A real ``env -i hermes kanban comment`` inside the terminal tool's real wrapper script, run by
    bash with the env the local backend gives it (the agent's os.environ, which carries
    HERMES_AGENT). The wrapper is a multi-command script, so bash stays the CLI's marked parent."""
    from tools.environments.local import LocalEnvironment
    tid = _task()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
            "HERMES_HOME": os.environ["HERMES_HOME"], "PYTHONPATH": root}
    inner = " ".join(f"{k}='{v}'" for k, v in base.items())
    env = LocalEnvironment.__new__(LocalEnvironment)
    env._snapshot_ready = False
    env._session_id = "provtest0"
    env._cwd_marker = "__HERMES_CWD_provtest0__"
    env._snapshot_path = "/tmp/hermes-snap-provtest0.sh"
    env._snapshot_passthrough_names = set()
    wrapped = env._wrap_command(
        f"env -i {inner} '{sys.executable}' -m hermes_cli.main kanban comment {tid} agent shell note", root)
    proc = subprocess.run(["bash", "-c", wrapped], env={**base, "HERMES_AGENT": "true"},
                          capture_output=True, text=True, timeout=180, check=False, cwd=root)
    assert "Comment added" in proc.stdout, proc.stdout + proc.stderr
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "agent"
