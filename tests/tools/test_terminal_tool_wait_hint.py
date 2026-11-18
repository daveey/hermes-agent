"""A foreground sleep/poll command gets a wait_hint when a process or card could be waited on."""

import json
from types import SimpleNamespace

import pytest

from tools import terminal_tool_wait_hint as wh

CARD = "t_01a1d774f7486668"
OWN = "t_01a1cdf5047304a9"
OK = json.dumps({"output": "done", "exit_code": 0, "error": None})
SLEEP_TAIL = "sleep 540; tail suite.log"


def _proc(pid="proc_4dae56ca81f6", command="scripts/run_tests.sh tests/ > suite.log 2>&1", *, notify=False, started_at=100.0):
    return SimpleNamespace(id=pid, command=command, notify_on_complete=notify, started_at=started_at)


@pytest.fixture
def running(monkeypatch):
    """Set the tracked running processes the hint sees for task 'task-1'."""
    procs = []
    monkeypatch.setattr(wh, "_running_processes", lambda task_id: list(procs) if task_id == "task-1" else [])
    return procs


class TestIsSleepWait:
    @pytest.mark.parametrize("command", [
        "sleep 540; tail -c 600 run.log",
        "sleep 60",
        "cd /tmp && sleep 175; cat out.txt",
        "sleep 2m; date -u",
        "date; sleep 120 && ls",
        "(sleep 300; tail log)",
        "{ sleep 90; cat out.txt; }",
        "if [ -f done ]; then cat out; else sleep 90; fi",
        "for i in $(seq 1 19); do [ -f done ] && break; sleep 10; done; tail log",
        "while pgrep -f ship.sh >/dev/null; do sleep 10; done; tail ship.log",
        "until grep -q '^rc=' out.txt; do\n  sleep 5\ndone",
    ])
    def test_sleep_waits(self, command):
        assert wh.is_sleep_wait(command)

    @pytest.mark.parametrize("command", [
        "sleep 5; curl -s localhost:8080/health",                      # settle delay
        "sleep 0.5 && ls",
        "for f in a b; do echo $f; done; sleep 2; ls",                 # short sleep after, not in, the loop
        "cat > run.sh <<'EOF'\nsleep 600\nEOF",                         # heredoc body: nothing slept
        "cat > run.sh <<EOF\nwhile true; do\nsleep 5\ndone\nEOF\nchmod +x run.sh",
        'git commit -m "retry; sleep 90 less"',                        # quoted text
        "grep -n 'x|sleep 120' notes.txt",
        "echo 'while true; do sleep 5; done' > loop.sh",
        "pytest tests/test_sleep.py -k 'sleep 600'",
        "grep -rn 'time.sleep(120)' src",
        "sleep $N; tail log",                                          # computed duration: not judged
        "for i in 1 2; do sleep 0.1; done; echo ok",                   # a pacing delay, not a poll loop
        "for h in a b; do ssh $h uptime & done; sleep 5; wait",        # `done` after `&`
        "while read x; do { echo $x; } done < f; sleep 2; ls",
        "cat > run.sh <<\\EOF\nsleep 600\nEOF",
        "cat > run.sh <<'END-OF'\nsleep 600\nEND-OF",
        "cat > a <<A; cat > b <<B\nsleep 600\nA\nsleep 700\nB\nls",    # two heredocs on one line
        "# don't forget\ngit commit -m 'wip; sleep 90 dropped'",        # apostrophe inside a comment
        "echo 'a'\\''b; sleep 90 '",
        "ls # nothing; sleep 600",
        "git commit -m fix: do sleep 90 less often",                   # do/then/else as plain words
        "echo waiting then sleep 120 seconds",
        "sleep 60 " + "x" * 9000,                                      # over the size bound: not examined
        "sleep " + "9" * 400 + "; ls",                                 # not a duration (and no float overflow)
        "echo $'it\\'s; sleep 90 '",                                    # ANSI-C quoting
        "(sleep 90 &) ; echo started",                                 # backgrounded: does not hold the call
        "sleep 61 &",
        "echo ${HOME} sleep 90",                                       # words after an expansion
        "echo $(date) sleep 90",
        "echo ${#a} 'x\nsleep 90\ny'",                                 # `${#` is not a comment
        'echo "$(echo "a; sleep 90")"',                                # quotes nested in a substitution
        "",
    ])
    def test_not_sleep_waits(self, command):
        assert not wh.is_sleep_wait(command)


class TestAwaitedCardId:
    @pytest.mark.parametrize("command", [
        f"sleep 400; hermes kanban show {CARD} | grep status",
        f"sleep 400; hermes kanban --board runners show {CARD}",
        f"sleep 150; sqlite3 -readonly ~/.hermes/kanban.db \"select status from tasks where id='{CARD}'\"",
        f"sleep 150; sqlite3 -readonly ~/.hermes/kanban/sync-shadow/default.db \"select status from tasks where id='{CARD}'\"",
    ])
    def test_reads_one_card(self, command):
        assert wh.awaited_card_id(command) == CARD

    @pytest.mark.parametrize("command", [
        f"sleep 90; hermes kanban comment {CARD} 'will show later'",
        f"sleep 90; hermes kanban complete {CARD} --show",
        f"sleep 90; cat kanban.db.bak; echo {CARD}",
        f"sleep 90; ls ~/.hermes/kanban.db; echo {CARD}",
        f"sleep 90; echo 'run hermes kanban show {CARD}'; hermes kanban unblock {CARD}",
        f"sleep 90; hermes kanban complete {OWN} --note show {CARD}",
        f"sleep 90; hermes kanban create --title show {CARD}",
        f"sleep 90; git -C ~/kanban show {CARD}",
        f"sleep 90; echo \"kanban.db 'select id={CARD}\"",
        f"sleep 90; sqlite3 ~/.hermes/kanban.db \"update tasks set x=1 where id in (select '{CARD}')\"",
        f"sleep 90; sqlite3 ~/.hermes/kanban.db \"select count(*) from tasks where parent_id='{CARD}'\"",
    ])
    def test_not_a_status_read(self, command):
        assert wh.awaited_card_id(command) is None

    def test_two_cards_is_not_one_wait(self):
        assert wh.awaited_card_id(f"sleep 120; hermes kanban show {CARD}; hermes kanban show {OWN}") is None

    def test_card_id_in_a_path_is_a_workspace_not_a_status_read(self):
        assert wh.awaited_card_id(f"sleep 405; tail -1 ~/.hermes/kanban/workspaces/{CARD}/probe.log") is None
        assert wh.awaited_card_id(f"sleep 90; hermes kanban list; cat ~/.hermes/kanban/workspaces/{CARD}/out.log") is None
        assert wh.awaited_card_id(f"sleep 90; hermes kanban show {CARD}; cat ~/.hermes/kanban/workspaces/{OWN}/out.log") == CARD

    def test_a_workers_own_card_is_not_a_card_to_park_on(self):
        assert wh.awaited_card_id(f"sleep 120; hermes kanban show {OWN}", own_card=OWN) is None
        assert wh.awaited_card_id(f"sleep 120; hermes kanban show {CARD}", own_card=OWN) == CARD


class TestWaitHint:
    def test_running_process_names_the_wait_call(self, running):
        running.append(_proc())
        hint = wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task=OWN)
        assert "process_manage(action='wait', session_id='proc_4dae56ca81f6', timeout=600)" in hint
        assert "scripts/run_tests.sh" in hint

    def test_the_offered_wait_is_no_longer_than_the_command_took(self, running):
        """An unrecognised server (`python app.py`) may be named; following the hint then blocks at
        most as long as the command just did, measured, never the 400 s default window."""
        running.append(_proc("proc_app", "python app.py"))
        assert "session_id='proc_app', timeout=61)" in wh.wait_hint("sleep 60; tail -20 server.log", "task-1", 61.4, kanban_task="")
        loop = "until grep -q 'Running on' app.log; do sleep 5; done"
        assert "timeout=45)" in wh.wait_hint(loop, "task-1", 45.9, kanban_task="")

    @pytest.mark.parametrize("command", [
        "until grep -q 'Running on' app.log; do sleep 5; done",     # readiness loop that hit after 5-15 s
        "for i in 1 2 3; do sleep 5; tail -1 run.log; done",
        "[ -f done ] || sleep 3600; tail log",                      # untaken branch: slept 0 s
        "f() { sleep 90; }; echo defined",                          # function body: slept 0 s
        "false && sleep 90; echo skipped",
        f"[ -f done ] || sleep 600; hermes kanban show {CARD}",
    ])
    @pytest.mark.parametrize("elapsed", [0.1, 15.0, 29.9, float("nan"), None, "600", True])
    def test_a_command_that_returned_quickly_gets_no_hint(self, running, command, elapsed):
        """Whatever the text says, a call that did not hold for half a minute did not wait."""
        running.append(_proc("proc_app", "python app.py"))
        assert wh.wait_hint(command, "task-1", elapsed, kanban_task=OWN) is None
        assert wh.annotate(OK, command, background=False, task_id="task-1", elapsed_seconds=elapsed) is OK

    def test_newest_process_is_named_not_the_oldest(self, running):
        running.extend([_proc("proc_build", "make all", started_at=10.0), _proc("proc_suite", started_at=50.0)])
        hint = wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="")
        assert "session_id='proc_suite'" in hint and "proc_build" not in hint

    @pytest.mark.parametrize("server", [
        "npm run dev", "pnpm dev", "python -m http.server 8000", "uvicorn app:app --port 8000",
        "vite --host", "tail -f server.log", "cargo watch -x test", "bin/server --port 9",
    ])
    def test_a_recognised_server_is_skipped(self, running, server):
        """A command on the server list is not offered; newer than the job, it is still skipped. (A
        server the list does not know is covered by the bounded timeout, tested below.)"""
        running.append(_proc("proc_server", server, started_at=90.0))
        assert wh.wait_hint("sleep 60; tail -20 server.log", "task-1", 600, kanban_task="") is None
        running.append(_proc("proc_suite", started_at=20.0))
        assert "session_id='proc_suite'" in wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="")

    @pytest.mark.parametrize("command", [
        "sleep 300; gh run view 123",
        "sleep 90; docker compose ps",
        "sleep 120; ssh box tail -1 run.log",
        "sleep 60; python -c \"import requests; requests.get('http://localhost:8000/health')\"",
        "sleep 60; http :8080/health",
    ])
    def test_a_sleep_that_then_looks_elsewhere_gets_no_process_hint(self, running, command):
        running.append(_proc())
        assert wh.wait_hint(command, "task-1", 600, kanban_task="") is None

    @pytest.mark.parametrize("command", [
        "sleep 60; curl -s localhost:8080/health",
        "for i in $(seq 1 30); do curl -sf localhost:3000/health && break; sleep 1; done",
        "sleep 90; nc -z 127.0.0.1 5432",
    ])
    def test_a_service_probe_gets_no_process_hint(self, running, command):
        """The running process is a server that never exits: a wait on it would block the whole window."""
        running.append(_proc("proc_server", "npm run dev"))
        assert wh.wait_hint(command, "task-1", 600, kanban_task="") is None

    def test_a_process_that_notifies_on_exit_gets_no_hint(self, running):
        """Its own notes say not to wait or poll again; the hint must not contradict them."""
        running.append(_proc(notify=True))
        assert wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="") is None

    def test_a_notifying_process_is_skipped_for_one_that_does_not(self, running):
        running.extend([_proc("proc_notify", notify=True, started_at=90.0), _proc("proc_plain", started_at=20.0)])
        assert "session_id='proc_plain'" in wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="")

    def test_secrets_in_the_process_command_are_redacted(self, running):
        secret = "sk-ant-api03-" + "A" * 40
        running.append(_proc(command=f"ANTHROPIC_API_KEY={secret} python run.py"))
        hint = wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="")
        assert "proc_4dae56ca81f6" in hint and secret not in hint

    def test_a_secret_straddling_the_shortening_point_does_not_leak(self, running):
        """Redact first, shorten second: a token cut at 80 characters no longer matches a pattern."""
        from agent.redact import redact_sensitive_text

        secret = "ghp_" + "a1B2c3D4e5" * 4
        command = "x" * 60 + f" GITHUB_TOKEN={secret} python run.py"
        running.append(_proc(command=command))
        hint = wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task="")
        assert redact_sensitive_text(command, code_file=True)[:80] in hint
        assert secret[:12] not in hint

    def test_no_running_process_and_no_card_gives_no_hint(self, running):
        assert wh.wait_hint(SLEEP_TAIL, "task-1", 600, kanban_task=OWN) is None

    def test_another_tasks_process_gives_no_hint(self, running):
        running.append(_proc())
        assert wh.wait_hint(SLEEP_TAIL, "task-2", 600, kanban_task=OWN) is None

    def test_no_task_id_gives_no_process_hint(self, running):
        running.append(_proc())
        assert wh.wait_hint(SLEEP_TAIL, None, 600, kanban_task=OWN) is None

    def test_not_a_sleep_gives_no_hint_even_with_a_running_process(self, running):
        running.append(_proc())
        assert wh.wait_hint("tail suite.log", "task-1", 600, kanban_task=OWN) is None

    def test_worker_card_wait_names_the_park(self, running):
        hint = wh.wait_hint(f"sleep 400; hermes kanban show {CARD}", "task-1", 600, kanban_task=OWN)
        assert "awaiting={'kind': 'card', 'ref': '" + CARD + "'}" in hint
        assert "kanban_schedule" in hint and "process_manage" not in hint

    def test_worker_reading_its_own_card_gets_no_park_hint(self, running):
        assert wh.wait_hint(f"sleep 400; hermes kanban show {OWN}", "task-1", 600, kanban_task=OWN) is None

    def test_chat_card_wait_points_at_the_completion_notice(self, running):
        hint = wh.wait_hint(f"sleep 400; hermes kanban show {CARD}", "task-1", 600, kanban_task="")
        assert CARD in hint and "end the turn" in hint
        assert "awaiting" not in hint  # a chat cannot park

    def test_worker_is_detected_from_the_environment(self, running, monkeypatch):
        command = f"sleep 400; hermes kanban show {CARD}"
        monkeypatch.setenv("HERMES_KANBAN_TASK", OWN)
        assert "awaiting" in wh.wait_hint(command, "task-1", 600)
        monkeypatch.delenv("HERMES_KANBAN_TASK")
        assert "end the turn" in wh.wait_hint(command, "task-1", 600)

    def test_process_and_card_both_apply(self, running):
        running.append(_proc())
        hint = wh.wait_hint(f"sleep 400; hermes kanban show {CARD}", "task-1", 600, kanban_task=OWN)
        assert "process_manage" in hint and "awaiting" in hint


class TestAnnotate:
    def test_adds_the_hint_and_keeps_the_result(self, running):
        running.append(_proc())
        out = json.loads(wh.annotate(OK, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600))
        assert out["output"] == "done" and out["exit_code"] == 0
        assert "process_manage" in out["wait_hint"]

    def test_unchanged_without_a_hint(self, running):
        assert wh.annotate(OK, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600) is OK
        running.append(_proc())
        assert wh.annotate(OK, "ls", background=False, task_id="task-1", elapsed_seconds=600) is OK

    def test_background_call_is_unchanged(self, running):
        running.append(_proc())
        assert wh.annotate(OK, SLEEP_TAIL, background=True, task_id="task-1", elapsed_seconds=600) is OK

    @pytest.mark.parametrize("result", ["not json", json.dumps(["a"]), json.dumps({"error": "refused"})])
    def test_unrecognised_result_is_unchanged(self, running, result):
        running.append(_proc())
        assert wh.annotate(result, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600) is result

    def test_non_string_command_is_unchanged(self, running):
        running.append(_proc())
        assert wh.annotate(OK, None, background=False, task_id="task-1", elapsed_seconds=600) is OK

    def test_a_failing_lookup_never_costs_the_result(self, monkeypatch):
        def boom(task_id):
            raise RuntimeError("registry down")

        monkeypatch.setattr(wh, "_running_processes", boom)
        assert wh.annotate(OK, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600) is OK


class TestRealResultShapes:
    """The envelopes terminal_tool() really returns for a command that did not simply finish."""

    def _unchanged(self, running, result):
        running.append(_proc())
        assert wh.annotate(result, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600) is result

    def test_refused_command_is_unchanged(self, running):
        from tools.terminal_tool import _error_json

        self._unchanged(running, _error_json("Command blocked: gateway lifecycle", status="blocked"))

    def test_fatal_error_is_unchanged(self, running):
        from tools.terminal_tool import _fatal_error_json

        self._unchanged(running, _fatal_error_json(RuntimeError("backend exploded")))

    def test_degraded_backend_is_unchanged(self, running):
        self._unchanged(running, json.dumps({"output": "", "exit_code": -1, "error": "unreachable", "status": "degraded"}))

    def test_yielded_sleep_is_not_told_to_wait_on_itself(self, running):
        """A user message mid-command turns the running sleep into a tracked process of this task."""
        running.append(_proc("proc_self", SLEEP_TAIL))
        yielded = json.dumps({"output": "", "exit_code": None, "error": None, "status": "yielded_to_background",
                              "session_id": "proc_self", "note": "respond to the user now"})
        assert wh.annotate(yielded, SLEEP_TAIL, background=False, task_id="task-1", elapsed_seconds=600) is yielded

    def test_still_running_result_without_a_status_is_unchanged(self, running):
        self._unchanged(running, json.dumps({"output": "", "exit_code": None, "error": None, "session_id": "proc_x"}))

    def test_command_that_never_produced_an_exit_status_is_unchanged(self, running):
        """exit_code -1 without a status: execution failed before the command exited."""
        from tools.terminal_tool import _error_json

        self._unchanged(running, _error_json("Command execution failed: backend gone"))

    def test_trailing_ampersand_sleep_is_not_told_to_wait_on_itself(self, running):
        """`sleep 61 &` in a foreground call is spawned as a tracked background process."""
        running.append(_proc("proc_self", "sleep 61", started_at=999.0))
        spawned = json.dumps({"output": "Background process started", "session_id": "proc_self", "pid": 4242,
                              "exit_code": 0, "error": None, "auto_backgrounded": "Ran in the background"})
        assert wh.annotate(spawned, "date; sleep 61 &", background=False, task_id="task-1", elapsed_seconds=600) is spawned

    @pytest.mark.parametrize("marker", ["session_id", "pid", "auto_backgrounded", "promoted_from_foreground"])
    def test_any_sign_of_a_spawn_keeps_the_result_unchanged(self, running, marker):
        """A foreground call above the timeout cap is promoted to a background process the same way."""
        self._unchanged(running, json.dumps({"output": "Background process started", "exit_code": 0, "error": None, marker: "x"}))

    @pytest.mark.parametrize("envelope", [
        {"output": "", "exit_code": 124, "error": "Command timed out after 180 seconds"},   # backend exception path
        {"output": "[Command timed out after 180s]", "exit_code": 124, "error": None},      # cut by Hermes
        {"output": "[Command interrupted]", "exit_code": 130, "error": None},               # user interrupt
        {"output": "[host is exiting: command not started]", "exit_code": 130, "error": None},
        {"output": "", "exit_code": 1, "error": "something went wrong"},
    ])
    def test_cut_short_or_errored_command_is_unchanged(self, running, envelope):
        """The sleep did not run its course, so there is no "instead" to offer."""
        self._unchanged(running, json.dumps(envelope))

    @pytest.mark.parametrize("envelope", [
        {"output": "tail: suite.log: No such file", "exit_code": 1, "error": None},
        {"output": "Managed Modal exec poll failed: ConnectionError", "exit_code": 1, "error": None},   # backend failure
        {"output": "", "exit_code": 2, "error": None},
    ])
    def test_non_zero_exit_is_unchanged(self, running, envelope):
        """A non-zero status is also how some backends report their own failure; only a clean finish counts."""
        self._unchanged(running, json.dumps(envelope))


class TestScanCost:
    """The scan runs on the tool thread after the command finished: it must not hold the result."""

    @pytest.mark.parametrize("unit", ["kanban --a ", "kanban\t--a\n", "kanban --a kanban ", "kanban --a=b ", "kanban show "])
    def test_card_show_is_fast_at_the_size_bound(self, unit):
        """`kanban --a kanban --a ...`: a flag's value can be the word `kanban`, so an unbounded flag
        loop ran to the end of the text from every start (about 70 ms here, 0.6 s on a slower host).
        Best of 5 wall-clock samples against a loose bound: about 2 ms idle, 37 ms worst with every
        core oversubscribed 2.5x. This only guards against a gross regression; the flag bound itself
        is pinned exactly by the next test, not by timing."""
        import time

        text = (unit * (wh.MAX_COMMAND_CHARS // len(unit) + 1))[:wh.MAX_COMMAND_CHARS]
        best = float("inf")
        for _ in range(5):
            started = time.perf_counter()
            wh._CARD_SHOW_RE.findall(text)
            best = min(best, time.perf_counter() - started)
        assert best < 0.25, f"{best:.3f}s at the size bound"

    def test_card_show_still_reads_past_several_flags(self):
        flags = " ".join(f"--f{i} v{i}" for i in range(wh.MAX_CARD_SHOW_FLAGS))
        assert wh.awaited_card_id(f"sleep 90; hermes kanban {flags} show {CARD}") == CARD
        assert wh.awaited_card_id(f"sleep 90; hermes kanban {flags} --one-more x show {CARD}") is None

    @pytest.mark.parametrize("command", [
        "sleep 1; " + "while " * 500 + "do " * 500 + "; a" * 500,
        "sleep 1\n" + "cat <<EOF\n" * 600,
        "sleep 1; " + "'" * 3999,
        "sleep 1; " + "for i in 1; do sleep 1; done; " * 250,
        "sleep 60; ./kanban " + " ".join(f"--opt{i} v{i}" for i in range(400)) + " run",   # was exponential
        "sleep 60; kanban " + "--a " * 1500,
        "sleep 60; kanban " + "--a b " * 1200 + "run",
        "sleep 60; kanban " + "--a=b " * 1200 + "run",
        "sleep 60; sqlite3 " + "kanban.db " * 700,
        "sleep 60; " + "kanban show " * 600,
        "sleep 60; x kanban.db '" + "select " * 900,
        "sleep 1; " + "$(" * 3000,
        "sleep 1; " + "${" * 3000,
        "sleep 1; " + '"$(' * 2000,
    ])
    def test_crafted_commands_are_scanned_quickly(self, command):
        import time

        assert len(command) <= wh.MAX_COMMAND_CHARS
        started = time.monotonic()
        wh.is_sleep_wait(command)
        wh.awaited_card_id(command)
        assert time.monotonic() - started < 1.0


class TestHandlerPath:
    """The real entry: the registered terminal handler, with a real tracked background process."""

    def test_terminal_handler_adds_the_hint_for_a_live_background_process(self, tmp_path, monkeypatch):
        from tools import terminal_tool as tt
        from tools.process_registry import process_registry

        with monkeypatch.context() as scoped:  # scoped, so the suite's own hermetic patches stay in place
            scoped.setattr(tt, "terminal_tool", lambda **kw: OK)  # no command ran: nothing was timed
            scoped.setattr(wh, "_running_processes", lambda task_id: [_proc()])
            assert "wait_hint" not in json.loads(tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-handler"))
        monkeypatch.setattr(tt, "terminal_tool", lambda **kw: (wh.note_execute_seconds(600.0), OK)[1])
        session = process_registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="task-handler")
        session.notify_on_complete = False
        try:
            out = json.loads(tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-handler"))
            assert f"session_id='{session.id}'" in out["wait_hint"]
            plain = tt._handle_terminal({"command": "ls"}, task_id="task-handler")
            assert "wait_hint" not in json.loads(plain)
            other = tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="another-task")
            assert "wait_hint" not in json.loads(other)
            session.notify_on_complete = True
            notified = tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-handler")
            assert "wait_hint" not in json.loads(notified)
        finally:
            process_registry.kill_process(session.id)
        after = json.loads(tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-handler"))
        assert "wait_hint" not in after  # the process is gone: nothing better to wait on

    def test_real_terminal_call_with_a_trailing_ampersand_gets_no_hint(self, tmp_path, monkeypatch):
        """Unstubbed: the real terminal tool backgrounds `sleep 61 &` itself; that sleep is the only
        running process of the task and must not be offered as something to wait on."""
        from tools import terminal_tool as tt
        from tools.process_registry import process_registry

        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setattr(wh, "MIN_ELAPSED_SECONDS", 0)  # the spawn returns at once: test the envelope gate alone
        out = json.loads(tt._handle_terminal({"command": "sleep 61 &", "workdir": str(tmp_path)}, task_id="task-amp"))
        try:
            assert out.get("session_id") and out.get("auto_backgrounded"), out
            assert "wait_hint" not in out
        finally:
            for proc in process_registry.running_owned_by("task-amp"):
                process_registry.kill_process(proc.id)

    def test_real_terminal_call_promoted_past_the_foreground_cap_gets_no_hint(self, tmp_path, monkeypatch):
        """Unstubbed: timeout above the foreground cap makes the tool start the sleep in the background,
        while another silent job of the task is running."""
        from tools import terminal_tool as tt
        from tools.process_registry import process_registry

        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setattr(wh, "MIN_ELAPSED_SECONDS", 0)  # the spawn returns at once: test the envelope gate alone
        other = process_registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="task-promoted")
        other.notify_on_complete = False
        try:
            out = json.loads(tt._handle_terminal(
                {"command": "sleep 61; echo hi", "workdir": str(tmp_path), "timeout": tt.FOREGROUND_MAX_TIMEOUT + 100},
                task_id="task-promoted"))
            assert out.get("promoted_from_foreground"), out
            assert "wait_hint" not in out
        finally:
            for proc in process_registry.running_owned_by("task-promoted"):
                process_registry.kill_process(proc.id)

    def test_terminal_handler_leaves_a_refusal_alone(self, tmp_path, monkeypatch):
        from tools import terminal_tool as tt
        from tools.process_registry import process_registry

        refused = tt._error_json("Command blocked", status="blocked")
        monkeypatch.setattr(tt, "terminal_tool", lambda **kw: (wh.note_execute_seconds(600.0), refused)[1])
        session = process_registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="task-refused")
        session.notify_on_complete = False
        try:
            assert tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-refused") is refused
        finally:
            process_registry.kill_process(session.id)


    def test_only_the_commands_own_run_time_counts(self, monkeypatch):
        """A call that spent 40 s before the command (approval, sandbox start, retries) and 1 s in it
        did not wait: the handler's wall time is not what is measured."""
        from tools import terminal_tool as tt

        monkeypatch.setattr(wh, "_running_processes", lambda task_id: [_proc()])
        monkeypatch.setattr(tt, "terminal_tool", lambda **kw: (wh.note_execute_seconds(1.2), OK)[1])
        clock = iter([1000.0, 1041.0, 1042.0, 1043.0])
        monkeypatch.setattr(tt.time, "monotonic", lambda: next(clock, 1050.0))
        assert tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-clock") is OK
        monkeypatch.setattr(tt, "terminal_tool", lambda **kw: (wh.note_execute_seconds(47.5), OK)[1])
        out = json.loads(tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-clock"))
        assert "timeout=47)" in out["wait_hint"] and "(47s)" in out["wait_hint"]

    def test_a_time_left_by_an_earlier_call_is_not_reused(self, monkeypatch):
        from tools import terminal_tool as tt

        monkeypatch.setattr(wh, "_running_processes", lambda task_id: [_proc()])
        wh.note_execute_seconds(600.0)  # e.g. an earlier call whose result was never annotated
        monkeypatch.setattr(tt, "terminal_tool", lambda **kw: OK)
        assert tt._handle_terminal({"command": "sleep 60; tail suite.log"}, task_id="task-stale") is OK
        assert wh.take_execute_seconds() is None

    def test_real_foreground_command_is_timed_by_its_execution(self, tmp_path, monkeypatch):
        """Unstubbed: `sleep 1` through the real terminal tool, with a real tracked job running. The
        timed value is the command's own run (about 1 s), which the lowered floor lets through."""
        from tools import terminal_tool as tt
        from tools.process_registry import process_registry

        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setattr(wh, "MIN_SLEEP_SECONDS", 1)
        job = process_registry.spawn_local("sleep 30", cwd=str(tmp_path), task_id="task-real")
        job.notify_on_complete = False
        try:
            monkeypatch.setattr(wh, "MIN_ELAPSED_SECONDS", 30)
            quick = json.loads(tt._handle_terminal({"command": "sleep 1; echo hi", "workdir": str(tmp_path)}, task_id="task-real"))
            assert quick["exit_code"] == 0 and "wait_hint" not in quick  # ran 1 s: under the real floor
            monkeypatch.setattr(wh, "MIN_ELAPSED_SECONDS", 0.9)
            out = json.loads(tt._handle_terminal({"command": "sleep 1; echo hi", "workdir": str(tmp_path)}, task_id="task-real"))
            # About 1 s; a loaded runner may stretch it, never to the 30 s a whole-suite stall would need.
            import re
            m = re.search(rf"session_id='{job.id}', timeout=(\d+)\)", out["wait_hint"])
            assert m and 1 <= int(m.group(1)) < 20, out["wait_hint"]
        finally:
            process_registry.kill_process(job.id)

    def test_a_failing_time_note_never_reruns_the_command(self, tmp_path, monkeypatch):
        """The note is taken inside the retry loop: if it raised, a finished command would run again."""
        from tools import terminal_tool as tt

        def boom(seconds):
            raise RuntimeError("thread-local gone")

        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setattr(wh, "note_execute_seconds", boom)
        marker = tmp_path / "runs.txt"
        out = json.loads(tt._handle_terminal({"command": f"echo ran >> {marker}; echo ok", "workdir": str(tmp_path)}, task_id="task-note"))
        assert out["exit_code"] == 0 and "ok" in out["output"]
        assert marker.read_text().count("ran") == 1
