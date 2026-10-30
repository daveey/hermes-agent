"""[daveey] A crash record quotes only the failed run's own output, never an earlier run's.

The per-task worker log is append-only across runs. Run 10187 of t_01a18977df212d21 died at start
having printed only ``Session: …``; its ``crashed`` event and chat notice quoted the PREVIOUS run's
final message ("…set it to needs_input. Reply with one of: a … b …"), so it read as a re-ask.
"""
import pytest

from hermes_cli import kanban_db_dispatch as kbd

PREVIOUS = (
    "I posted these findings as a comment on the task and set it to needs_input. Reply with one of:\n"
    "- a: leave Desktop alone.\n"
    "\nResume this session with:\n  hermes --resume 20261029_212549_b67f3c\n"
    "\n[kanban-worker-exit] rc=0\n"
)


def _output(monkeypatch, text):
    monkeypatch.setattr(kbd._kb, "read_worker_log", lambda task_id, tail_bytes=None, board=None: text)
    return kbd._worker_final_output("t_x", board="default")


@pytest.mark.parametrize("own_trailer", ["", "\n[kanban-worker-exit] rc=1\n"])
def test_silent_failed_run_does_not_quote_the_previous_runs_message(monkeypatch, own_trailer):
    out = _output(monkeypatch, PREVIOUS + "Session: local-0c64\n" + own_trailer)
    assert "needs_input" not in out and "Reply with one of" not in out


@pytest.mark.parametrize("own_trailer", ["", "\n[kanban-worker-exit] rc=1\n"])
def test_failed_run_quotes_its_own_start_failure_cause(monkeypatch, own_trailer):
    own = ("Session: local-0c64\nKanban owner refused: Kanban execution unknown; retained by owner "
           "(Worker could not start: ModelRoutingError: Class selectors require provider auto)\n")
    out = _output(monkeypatch, PREVIOUS + own + own_trailer)
    assert "Worker could not start: ModelRoutingError" in out
    assert "needs_input" not in out


def test_single_run_log_keeps_its_final_message(monkeypatch):
    """Control: the first run of a card (no earlier trailer) still reports its own final text."""
    out = _output(monkeypatch, PREVIOUS)
    assert "Reply with one of" in out
