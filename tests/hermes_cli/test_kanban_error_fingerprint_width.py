"""Same-error crashes group whatever their PIDs' digit counts. [daveey]

``error_fingerprint`` used to cut the text to 80 chars BEFORE replacing ``pid <n>``: a crash
text that runs past 80 chars (``pid <n> exited with code 1`` + the worker's last output) kept
one more output char per missing PID digit. Three workers of one systemic batch straddling a
digit boundary (pid 99999 / 100000) got different fingerprints, the batch never reached the
systemic count of 3, and nothing tripped: CI flake of
test_kanban_run_finalizer::test_systemic_batch_preserves_one_fix_owner_inside_transaction.
"""
import itertools

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_values import error_fingerprint
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board

_TEXT = "pid {} exited with code 1 Worker's last output: 'fixture worker_receipt_timeout'"


def test_fingerprint_ignores_pid_width_past_the_cut():
    prints = {error_fingerprint(_TEXT.format(pid)) for pid in (9999, 10000, 99999, 100000, 2156134)}
    assert prints == {"pid n exited with code 1 worker's last output: 'fixture worker_receipt_timeout'"}
    longer = _TEXT.replace("timeout'", "timeout and a much longer tail'")
    prints = {error_fingerprint(longer.format(pid)) for pid in (9, 99999, 100000, 2156134)}
    assert len(prints) == 1 and len(prints.pop()) == 80


def test_fingerprint_still_separates_different_errors_and_timestamps():
    assert error_fingerprint('pid 1 exited with code 1') != error_fingerprint('pid 1 exited with code 2')
    assert error_fingerprint('x' * 75 + ' 1794603625123') == error_fingerprint('x' * 75 + ' 1794603625')
    assert len(error_fingerprint('y' * 500)) == 80


def test_systemic_batch_trips_across_a_pid_digit_boundary(board, monkeypatch):
    conn, context = board
    contexts = [context]
    for index in range(2):
        tid = kb.create_task(conn, title=f'additional failed result {index}', assignee='default')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert task is not None
        contexts.append({**context, 'task_id': tid, 'run_id': task.current_run_id,
                         'claim_lock': task.claim_lock})
    for item in contexts:
        fixtures._failed_child(conn, item)
    # The real children's PIDs share a width here; give the batch the widths CI's did.
    widths = itertools.cycle((99999, 100000, 100001))
    real = kbd._classify_dead_worker

    def straddle(pid, *args, **kwargs):
        dead = real(pid, *args, **kwargs)
        dead.error_text = dead.error_text.replace(f'pid {pid} ', f'pid {next(widths)} ', 1)
        return dead

    monkeypatch.setattr(kbd, '_classify_dead_worker', straddle)
    sweep = kbd._reclaim_dead_workers(conn, failed_results_only=True)
    assert len(sweep.crash_details) == 3
    assert len({error_fingerprint(d.error_text) for *_, d in sweep.crash_details}) == 1
    assert set(sweep.auto_blocked) == {item['task_id'] for item in contexts}
    fixes = conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE 'systemic-crash:%'").fetchall()
    assert len(fixes) == 1
