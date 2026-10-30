"""[daveey] A crash record never quotes the previous run's reply, in the log layout the managed client really writes.

Run 10258 of t_01a188a461c0178c (01:25 PDT 10-30, core 11834422cd, which already cut the log at the
previous exit trailer) was booked with run 10246's final message. The managed worker client prints its
reply on stdout (block-buffered: the dispatcher redirects it to the per-task log file) and ``Session:``
plus the exit trailer on stderr (flushed). So the reply reached the file at interpreter exit, AFTER the
run's own trailer, and the next run's "own part" (everything after the previous trailer) began with it.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_worker_log import current_run_log

# Shape of ~/.hermes/kanban/logs/t_01a188a461c0178c.log lines 1630-1639 (written by the pre-fix client).
PREVIOUS_REPLY = "The card has gone back to a fresh run that starts on iroh and the two Swift apps now."
LIVE_LAYOUT = (
    "Session: local-f001\n"
    "\n[kanban-worker-exit] rc=0\n"
    f"{PREVIOUS_REPLY}\n"
    "Session: local-d566\n"
    "Kanban owner refused: Kanban owner silent for 188s: Gateway draining: restart_wait\n"
    "\n[kanban-worker-exit] rc=1\n"
)


def test_current_run_starts_at_its_own_session_line():
    own = current_run_log(LIVE_LAYOUT)
    assert PREVIOUS_REPLY not in own
    assert "Kanban owner silent for 188s" in own


def test_crash_output_of_live_layout_omits_previous_reply(monkeypatch):
    monkeypatch.setattr(kbd._kb, "read_worker_log", lambda task_id, tail_bytes=None, board=None: LIVE_LAYOUT)
    out = kbd._worker_final_output("t_x", board="default")
    assert "Swift apps" not in out
    assert "restart_wait" in out


def test_first_run_reply_is_kept():
    """Control: a card's only run (no earlier trailer) keeps its reply, in either order."""
    assert PREVIOUS_REPLY in current_run_log(f"Session: local-a\n{PREVIOUS_REPLY}\n\n[kanban-worker-exit] rc=0\n")
    assert PREVIOUS_REPLY in current_run_log(f"Session: local-a\n\n[kanban-worker-exit] rc=0\n{PREVIOUS_REPLY}\n")


def test_client_writes_its_reply_before_its_exit_trailer(tmp_path):
    """The real client's streams, redirected to one file as the dispatcher does, keep reply < trailer."""
    repo = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(repo)!r})
        import gateway.session_kanban as sk
        from hermes_cli import kanban_worker_client as c
        sk.worker_result = lambda board_db, params: {{'last_output': 'REPLY-TEXT', 'exit_code': 0}}
        def _submit():
            print('Session: local-x', file=sys.stderr)
            return c._exit_code({{'status': 'terminal'}}, {{}}, 'db')
        c._submit = _submit
        sys.exit(c.main())
    """)
    log = tmp_path / "t.log"
    with log.open("w") as fh:
        rc = subprocess.run([sys.executable, "-c", script], stdout=fh, stderr=subprocess.STDOUT,
                            env={k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
                            timeout=120, check=False).returncode
    text = log.read_text()
    assert rc == 0, text
    assert text.index("REPLY-TEXT") < text.index("[kanban-worker-exit] rc=0"), text
