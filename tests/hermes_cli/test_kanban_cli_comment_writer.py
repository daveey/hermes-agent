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


# `ssh <Mac> 'hermes kanban comment …'` from an agent's terminal: macOS sshd (AcceptEnv LANG LC_*)
# drops every HERMES_* variable, so the remote login shell carries only the LC_ marker; sshd-session
# and launchd are root's (skipped).
SID = "local-" + "ab" * 32
SSH_FROM_AGENT: Table = {800: (700, {"HOME": "/Users/d", "LANG": "en_US.UTF-8", "LC_HERMES_AGENT": SID}),
                         700: (1, None), 1: (0, None)}


def test_ssh_session_from_an_agent_is_an_agent():
    assert kcw.agent_ancestor(read=_me({"PATH": "/usr/bin"}, 800, SSH_FROM_AGENT)) == "800:LC_HERMES_AGENT"
    kind, session, evidence = kcw.cli_comment_writer(read=_me({}, 800, SSH_FROM_AGENT),
                                                     env={"LC_HERMES_AGENT": SID})
    assert (kind, session, evidence) == ("agent", SID, "800:LC_HERMES_AGENT")


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_ssh_marker_is_human(value):
    table = {800: (700, {"HOME": "/Users/d", "LC_HERMES_AGENT": value}), 700: (1, None), 1: (0, None)}
    assert kcw.agent_ancestor(read=_me({}, 800, table)) is None
    assert kcw.cli_comment_writer(read=_me({}, 800, table), env={"LC_HERMES_AGENT": value})[0] == "human"


def test_person_ssh_session_is_human():
    """A person's own ssh into the Mac forwards LANG/LC_* too, but their shell set no marker."""
    table = {800: (700, {"HOME": "/Users/d", "LANG": "en_US.UTF-8", "LC_CTYPE": "UTF-8"}), 700: (1, None),
             1: (0, None)}
    assert kcw.cli_comment_writer(read=_me({}, 800, table), env={})[0] == "human"


@pytest.mark.parametrize("value,session", [(SID, SID), ("20261026_170127_f07c32", "20261026_170127_f07c32"),
                                           ("1", None), ("true", None), ("", None)])
def test_ssh_marker_names_the_writer_session_only_when_session_shaped(value, session):
    assert kcw.marker_session({"LC_HERMES_AGENT": value}) == session
    # HERMES_SESSION_ID, when the CLI inherited one, still wins.
    assert kcw.cli_comment_writer(read=_me({}, 800, SSH_FROM_AGENT),
                                  env={"HERMES_SESSION_ID": "sess-a", "LC_HERMES_AGENT": value})[1] == "sess-a"


@pytest.mark.parametrize("env,value", [({"HERMES_SESSION_ID": "sess-a"}, "sess-a"), ({}, "1"),
                                       ({"LC_HERMES_AGENT": SID}, SID),
                                       ({"HERMES_SESSION_ID": "sess-a", "LC_HERMES_AGENT": SID}, "sess-a"),
                                       ({"HERMES_SESSION_ID": "a b\nc"}, "1")])
def test_ssh_marker_value_is_the_session_id_else_1(env, value):
    assert kcw.stamp_ssh_agent_marker(dict(env))["LC_HERMES_AGENT"] == value


# --- every agent tool subprocess exports the ssh-forwardable marker -----------------------------

def test_terminal_wrapper_exports_ssh_marker_with_the_session_id():
    """Real bash through the terminal tool's wrapper (every backend, remote ones included)."""
    from tools.environments.local import LocalEnvironment
    env = LocalEnvironment.__new__(LocalEnvironment)
    env._snapshot_ready = False
    env._session_id = "lcmark0"
    env._cwd_marker = "__HERMES_CWD_lcmark0__"
    env._snapshot_path = "/tmp/hermes-snap-lcmark0.sh"
    env._snapshot_passthrough_names = set()
    wrapped = env._wrap_command('echo "LC=$LC_HERMES_AGENT"', "/tmp")
    clean = {k: v for k, v in os.environ.items() if k not in ("LC_HERMES_AGENT", "HERMES_SESSION_ID")}
    run = lambda e: subprocess.run(["bash", "-c", wrapped], env=e, capture_output=True, text=True,
                                   timeout=30, check=False).stdout
    assert "LC=1\n" in run(clean)
    assert f"LC={SID}\n" in run({**clean, "HERMES_SESSION_ID": SID})


def test_terminal_snapshot_does_not_persist_the_ssh_marker():
    from tools.environments.base_session_env import _export_dump_excluding_session_vars
    snippet = _export_dump_excluding_session_vars("/tmp/x")
    assert " LC_HERMES_AGENT " in snippet.split("export -p")[0]


def test_local_subprocess_env_exports_ssh_marker(monkeypatch):
    from tools.environments.local import build_subprocess_env
    monkeypatch.delenv("LC_HERMES_AGENT", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", SID)
    assert build_subprocess_env()["LC_HERMES_AGENT"] == SID
    monkeypatch.delenv("HERMES_SESSION_ID")
    assert build_subprocess_env()["LC_HERMES_AGENT"] == "1"
    # The unscrubbed path stays byte-preserving: it carries the marker main() advertised.
    monkeypatch.setenv("LC_HERMES_AGENT", "1")
    assert build_subprocess_env(scrub_secrets=False)["LC_HERMES_AGENT"] == "1"


def test_execute_code_child_env_exports_ssh_marker(tmp_path, monkeypatch):
    from tools.code_execution_env import _build_child_env
    monkeypatch.delenv("LC_HERMES_AGENT", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", SID)
    env = _build_child_env(rpc_endpoint="fixture", rpc_token="fixture", tmpdir=str(tmp_path),
                           child_python=sys.executable)
    assert env["LC_HERMES_AGENT"] == SID


def test_entrypoints_advertise_the_ssh_marker(monkeypatch):
    from hermes_cli.main import _advertise_agent_env
    monkeypatch.delenv("LC_HERMES_AGENT", raising=False)
    _advertise_agent_env()
    assert os.environ["LC_HERMES_AGENT"] == "1"
    monkeypatch.setenv("LC_HERMES_AGENT", SID)  # an outer agent's (ssh-forwarded) marker is kept
    _advertise_agent_env()
    assert os.environ["LC_HERMES_AGENT"] == SID


def test_mcp_stdio_server_env_carries_the_ssh_marker(monkeypatch):
    from tools.mcp_tool_config import _build_safe_env
    monkeypatch.setenv("LC_HERMES_AGENT", "1")
    assert _build_safe_env(None)["LC_HERMES_AGENT"] == "1"


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
                               "HERMES_SESSION_ID", "LC_HERMES_AGENT")}
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


# --- automation runners and agent handles (third relay: hub-worker-exec over SSM) ---------------
# hub-worker-exec runs `runuser -u ubuntu -- bash -c ...` from ssm-document-worker, whose environment
# is amazon-ssm-agent's: no relay forwarded the caller's markers, and the root-owned runners were
# skipped as foreign, so the walk reached init clean and recorded `human`. A command started by a
# non-interactive automation runner is never a person typing; an interactive SSM session still is.

class _FakeProc:
    """psutil.Process for a modelled tree: (ppid, uids, environ or None=unreadable, name)."""
    table: dict = {}

    def __init__(self, pid):
        self.pid = pid
        self._ppid, self._uids, self._env, self._name = self.table[pid]

    def ppid(self):
        return self._ppid

    def uids(self):
        return self._uids

    def name(self):
        return self._name

    def environ(self):
        import psutil
        if self._env is None:
            raise psutil.AccessDenied(self.pid)
        return self._env


def _fake_psutil_tree(monkeypatch, rows: dict):
    """Model a process tree for the REAL reader (``_read_proc`` + names): root rows are unreadable."""
    import psutil
    uid = os.getuid()  # windows-footgun: ok (POSIX-only tests, skipped elsewhere)
    if uid == 0:
        pytest.skip("root reads every environ")
    table = {}
    for pid, (ppid, owner, env, name) in rows.items():
        uids = (0, 0, 0) if owner == "root" else (uid, uid, uid)
        table[pid] = (ppid, uids, None if owner == "root" else env, name)
    real_process, real_stat = psutil.Process, os.stat
    monkeypatch.setattr(_FakeProc, "table", table)
    # Only modelled pids are fake (the CLI's own DB code may consult psutil/os.stat for others).
    monkeypatch.setattr(psutil, "Process", lambda pid=None: _FakeProc(pid) if pid in table else real_process(pid))
    monkeypatch.setattr(kcw.os, "stat", lambda path, *a, **k: SimpleNamespace(st_uid=uid)
                        if str(path).startswith("/proc/") else real_stat(path, *a, **k))


CLEAN = {"HOME": "/home/ubuntu", "PATH": "/usr/bin"}
INIT = {1: (0, "root", None, "systemd")}
# The hub-worker-exec tree observed on worker-5 (comment 16922).
SSM_SEND_COMMAND = {
    900: (901, "ubuntu", CLEAN, "python3"), 901: (902, "ubuntu", CLEAN, "bash"),
    902: (903, "root", None, "runuser"), 903: (904, "root", None, "ssm-document-worker"),
    904: (905, "root", None, "ssm-agent-worker"), 905: (1, "root", None, "amazon-ssm-agent"), **INIT}


posix_only = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids")


@posix_only
def test_ssm_send_command_tree_is_an_agent_at_ssm_document_worker(monkeypatch):
    _fake_psutil_tree(monkeypatch, SSM_SEND_COMMAND)
    assert kcw.agent_ancestor(900) == "903:runner:ssm-document-worker"


@posix_only
@pytest.mark.parametrize("runner", ["amazon-ssm-agent", "amazon-ssm-agen", "ssm-document-worker",
                                    "cron", "crond", "atd"],
                         ids=["ssm-agent", "ssm-agent-comm-truncated", "ssm-document-worker", "cron",
                              "crond", "atd"])
def test_tree_rooted_at_an_automation_runner_is_an_agent(monkeypatch, runner):
    _fake_psutil_tree(monkeypatch, {900: (901, "ubuntu", CLEAN, "python3"),
                                    901: (910, "ubuntu", CLEAN, "sh"),
                                    910: (1, "root", None, runner), **INIT})
    assert kcw.agent_ancestor(900) == f"910:runner:{kcw.RUNNER_FOR_NAME[runner[:15]]}"


@posix_only
def test_interactive_ssm_session_stays_human(monkeypatch):
    """``aws ssm start-session``: a person's shell under ssm-session-worker, whose own ancestors are
    the same ssm-agent-worker/amazon-ssm-agent that start SendCommand documents."""
    _fake_psutil_tree(monkeypatch, {900: (901, "ubuntu", CLEAN, "python3"),
                                    901: (920, "ubuntu", CLEAN, "bash"),
                                    920: (904, "root", None, "ssm-session-worker"),
                                    904: (905, "root", None, "ssm-agent-worker"),
                                    905: (1, "root", None, "amazon-ssm-agent"), **INIT})
    assert kcw.agent_ancestor(900) is None


@posix_only
def test_human_login_shell_under_real_reader_stays_human(monkeypatch):
    _fake_psutil_tree(monkeypatch, {900: (901, "ubuntu", CLEAN, "python3"),
                                    901: (930, "ubuntu", CLEAN, "bash"),
                                    930: (931, "root", None, "sshd-session"),
                                    931: (1, "root", None, "sshd"), **INIT})
    assert kcw.agent_ancestor(900) is None


@posix_only
def test_cli_writer_records_runner_evidence(monkeypatch):
    me = os.getpid()
    _fake_psutil_tree(monkeypatch, {me: (901, "ubuntu", CLEAN, "python3"),
                                    **{k: v for k, v in SSM_SEND_COMMAND.items() if k != 900}})
    assert kcw.cli_comment_writer(env={}) == ("agent", None, "903:runner:ssm-document-worker")


@posix_only
def test_cli_comment_over_ssm_send_command_records_agent(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    me = os.getpid()
    tid = _task()
    _fake_psutil_tree(monkeypatch, {me: (901, "ubuntu", CLEAN, "python3"),
                                    **{k: v for k, v in SSM_SEND_COMMAND.items() if k != 900}})
    p = _payload(tid, _cli_comment(tid, "relayed agent note"))
    assert p["writer_kind"] == "agent"


AGENT_HANDLES = ["worker-5/t_01a1d55f5bbdae39", "hermes-hub/t_0123456789abcdef", "David-MBP/t_01a1d4faf1ecda4b",
                 "hub-chat-a1b2c3", "hub-chat-0123456789abcdef"]
NOT_HANDLES = ["default", "david", "worker-5/t_01a1d55f", "worker-5/t_01A1D55F5BBDAE39", "hub-chat-a1b2c",
               "hub-chat-zzzzzz", "x/t_01a1d55f5bbdae39/extra", "/t_01a1d55f5bbdae39", "t_01a1d55f5bbdae39",
               "hub-chat-a1b2c3 ", "my hub-chat-a1b2c3"]


@pytest.mark.parametrize("author", AGENT_HANDLES)
def test_agent_handle_author_records_agent_over_a_clean_walk(author, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger=kcw.__name__):
        assert kcw.cli_comment_writer(read=_me({}, 500, HUMAN_SHELL), env={}, author=author) == (
            "agent", None, "author_handle")
    assert [r.getMessage() for r in caplog.records if "author handle" in r.getMessage()] == [
        "comment_writer: author handle overrides human walk"]


@pytest.mark.parametrize("author", NOT_HANDLES)
def test_other_authors_with_a_clean_walk_stay_human(author, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger=kcw.__name__):
        assert kcw.cli_comment_writer(read=_me({}, 500, HUMAN_SHELL), env={}, author=author) == (
            "human", None, "clean-ancestry")
    assert not [r for r in caplog.records if "author handle" in r.getMessage()]


def test_handle_author_does_not_relabel_an_agent_walk(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger=kcw.__name__):
        kind, _s, evidence = kcw.cli_comment_writer(read=_me({}, 600, AGENT_TERMINAL), env={},
                                                    author="hub-chat-a1b2c3")
    assert (kind, evidence) == ("agent", "600:HERMES_SESSION_ID")
    assert not [r for r in caplog.records if "author handle" in r.getMessage()]


def test_cli_comment_with_handle_author_from_clean_shell_records_agent(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "_read_proc", _me({"HOME": "/h"}, 500, HUMAN_SHELL))
    tid = _task()
    parser = argparse.ArgumentParser(prog="hermes")
    kanban.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "comment", tid, "relayed", "--author", "worker-5/t_01a1d55f5bbdae39"])
    assert kanban.kanban_command(args) == 0
    p = _payload(tid, _last_comment(tid))
    assert p["writer_kind"] == "agent"


@posix_only
def test_unreadable_process_name_only_skips_the_runner_rule(monkeypatch):
    """A name psutil can't read is no runner, and no reason to call the walk inconclusive."""
    import psutil
    _fake_psutil_tree(monkeypatch, {900: (901, "ubuntu", CLEAN, "python3"),
                                    901: (1, "ubuntu", CLEAN, "bash"), **INIT})

    def denied(self):
        raise psutil.AccessDenied(self.pid)
    monkeypatch.setattr(_FakeProc, "name", denied)
    assert kcw.agent_ancestor(900) is None


def test_none_author_is_not_a_handle():
    assert kcw.is_agent_handle(None) is False


def test_monkeypatched_reader_reads_no_real_names(monkeypatch):
    """A fake ``_read_proc`` (fixed pids) must not be paired with REAL process names: pid 400 on a
    CI host may well be cron."""
    monkeypatch.setattr(kcw, "_read_proc", _me({"HOME": "/h"}, 500, HUMAN_SHELL))
    monkeypatch.setattr(kcw, "_proc_name", lambda pid: pytest.fail(f"real name read for fake pid {pid}"))
    assert kcw.agent_ancestor() is None


# --- systemd units (hub timers, gateway plugins): the comment process's own cgroup v2 leaf -------
# 14 hub automation comments from systemd timers and the gateway's own services recorded human: a
# `.service` unit is started by the service manager, never typed by a person in it; a login/ssh
# session lives in a `session-N.scope`, and other `.scope`s (terminal apps) keep the walk.

SYSTEM_SERVICE = "/system.slice/hermes-hub-sync.service"
USER_SERVICE = "/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service"
SSH_SESSION = "/user.slice/user-1000.slice/session-3.scope"
APP_SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Terminal-1234.scope"


@pytest.mark.parametrize("path,unit", [(SYSTEM_SERVICE, "hermes-hub-sync.service"),
                                       (USER_SERVICE, "hermes-gateway.service"),
                                       ("/user.slice/user-1000.slice/user@1000.service/hermes-hub-watch.service",
                                        "hermes-hub-watch.service")],
                         ids=["system-slice", "user-app-slice", "user-manager-direct"])
def test_systemd_service_cgroup_records_agent_over_a_clean_walk(path, unit):
    assert kcw.cli_comment_writer(read=_me({}, 500, HUMAN_SHELL), env={}, cgroup=lambda: path) == (
        "agent", None, f"runner:systemd:{unit}")


@pytest.mark.parametrize("path", [SSH_SESSION, APP_SCOPE, None, "/", "",
                                  "/system.slice/session-9.scope",          # a scope, even under system.slice
                                  "/machine.slice/libvirtd-x.service",       # neither system.slice nor user@
                                  "/user.slice/user-1000.slice/user@1000.service",  # the manager's own node
                                  "/user.slice/user-1000.slice/user@1000.service/app.slice",
                                  "/system.slice/docker.service/sub"],     # inside a delegated subtree
                         ids=["session-scope", "app-scope", "no-v2-line", "root", "empty", "system-scope",
                              "machine-slice", "user-manager-node", "app-slice-node", "delegated-subtree"])
def test_other_cgroups_keep_the_human_walk(path):
    assert kcw.cli_comment_writer(read=_me({}, 500, HUMAN_SHELL), env={}, cgroup=lambda: path) == (
        "human", None, "clean-ancestry")


def test_app_scope_keeps_an_agent_walk_unchanged():
    assert kcw.cli_comment_writer(read=_me({}, 600, AGENT_TERMINAL), env={}, cgroup=lambda: APP_SCOPE) == (
        "agent", None, "600:HERMES_SESSION_ID")


def test_service_cgroup_does_not_relabel_an_agent_walk():
    kind, _s, evidence = kcw.cli_comment_writer(read=_me({}, 600, AGENT_TERMINAL), env={},
                                                cgroup=lambda: SYSTEM_SERVICE)
    assert (kind, evidence) == ("agent", "600:HERMES_SESSION_ID")


@pytest.mark.parametrize("text,path", [
    ("0::/system.slice/cron.service\n", "/system.slice/cron.service"),
    ("12:pids:/user.slice\n1:name=systemd:/x.service\n0::" + USER_SERVICE + "\n", USER_SERVICE),
    ("12:pids:/system.slice/x.service\n", None),   # cgroup v1 only: no unified line
    ("", None)], ids=["v2", "hybrid", "v1-only", "empty"])
def test_cgroup_v2_path_parses_the_unified_line(text, path):
    assert kcw.cgroup_v2_path(text) == path


def test_monkeypatched_reader_reads_no_real_cgroup(monkeypatch):
    """With a substituted reader the real process's cgroup (a test run inside a systemd service) is
    not consulted, like process names."""
    monkeypatch.setattr(kcw, "_read_proc", _me({"HOME": "/h"}, 500, HUMAN_SHELL))
    monkeypatch.setattr(kcw, "_self_cgroup", lambda: pytest.fail("real cgroup read"))
    assert kcw.cli_comment_writer(env={}) == ("human", None, "clean-ancestry")


def test_real_reader_consults_the_real_cgroup(monkeypatch):
    calls = []
    monkeypatch.setattr(kcw, "agent_ancestor", lambda **kw: None)
    monkeypatch.setattr(kcw, "_self_cgroup", lambda: calls.append(1) or SYSTEM_SERVICE)
    assert kcw.cli_comment_writer(env={}) == ("agent", None, "runner:systemd:hermes-hub-sync.service")
    assert calls == [1]


def test_cli_comment_from_a_systemd_service_records_agent(worker_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "agent_ancestor", lambda **kw: None)
    monkeypatch.setattr(kcw, "_self_cgroup", lambda: USER_SERVICE)
    tid = _task()
    assert _payload(tid, _cli_comment(tid, "timer note"))["writer_kind"] == "agent"


def test_typed_slash_command_inside_a_service_stays_human(worker_home, monkeypatch):
    """The gateway is a systemd service; ``/kanban comment`` a person typed into it is still theirs."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kcw, "agent_ancestor", lambda **kw: None)
    monkeypatch.setattr(kcw, "_self_cgroup", lambda: USER_SERVICE)
    tid = _task()
    assert "Comment added" in kanban.run_slash(f"comment {tid} typed by me", human_typed=True)
    assert _payload(tid, _last_comment(tid))["writer_kind"] == "human"
