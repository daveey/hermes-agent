"""`hermes kanban gc` says on stdout why each scratch tree was kept. [daveey]

mini, 2026-11-14: the daily pass kept four aged trees whose archive was refused and removed 0
workspaces, and `mac-worktree-gc.log` said nothing about any of them in 4028 lines. Subcommands
log to files only, so the `Scratch preserved: ...` records never reached the stdout the GC
wrapper captures and relays, and the refusals were INFO lines in an agent.log that had rotated.
"""
import logging
import re
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_ops
from hermes_cli import kanban_scratch_archive as sa
from hermes_cli import kanban_scratch_backoff as bo
from hermes_cli import kanban_worktree_gc as gc
from tests.hermes_cli import test_kanban_scratch_gc_archive as helpers
from tests.hermes_cli.test_kanban_scratch_gc_archive import OLD, _task

board = helpers.board
remote = helpers.remote

pytestmark = pytest.mark.usefixtures("board")

# The relay in mac-worktree-gc.py full-matches the detail against this alphabet (plus the `?`
# that stands in for anything else).
DETAIL = re.compile(r"[A-Za-z0-9_./:=,()' ?-]{1,200}")


def _refused(remote):
    tid, path = _task(remote=remote)
    helpers.refuse_forever(path)
    return tid, path


def _keep_lines(capsys):
    """This pass's stdout: the kept-tree lines, after checking nothing else was printed with them."""
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith("GC complete: "), lines
    kept = lines[:-1]
    assert all(line.startswith("Scratch preserved: ") for line in kept), kept
    return kept


def test_tree_inside_retention_is_named_on_stdout(remote, capsys):
    finished = int(time.time()) - 3600
    tid, path = _task(remote=remote, completed_at=finished)
    assert helpers._gc() == 0
    assert _keep_lines(capsys) == [
        f"Scratch preserved: reason=retention_pending path={path} task={tid} "
        f"eligible_at={finished + 7 * 86400}; deadline is archival eligibility only, "
        "all safety guards still apply"]
    assert path.exists()


def test_fresh_archive_refusal_then_backoff_skip_are_named_on_stdout(remote, capsys, caplog):
    tid, path = _refused(remote)
    caplog.set_level(logging.INFO, logger="hermes_cli")
    assert helpers._gc() == 0
    entry = bo.Refusals("default").entries[tid]
    head = (f"Scratch preserved: reason=archive_refused path={path} task={tid} "
            f"attempts=1 retry_at={int(entry['retry_at'])}; ")
    (fresh,) = _keep_lines(capsys)
    assert fresh.startswith(head)
    detail = fresh[len(head):]
    assert DETAIL.fullmatch(detail) and "linked worktree or submodule" in detail
    assert detail == bo.keep_detail(entry["reason"])
    # The file log keeps its INFO line: the stdout line is added next to it, not instead of it.
    assert f"Keeping aged scratch workspace for task {tid}: {path} (" in caplog.text

    caplog.clear()
    assert helpers._gc() == 0  # inside the backoff window: not re-archived, still named
    assert _keep_lines(capsys) == [fresh]
    assert "archive refused 1 time(s)" in caplog.text
    assert path.exists()


def test_refused_detach_is_named_on_stdout(remote, capsys, monkeypatch, caplog):
    tid, path = _task(remote=remote)

    def refuse(*_args, **_kwargs):
        raise sa.ArchiveRefused("owner changed\nwhile archiving \u2603 {x}")

    monkeypatch.setattr(sa, "remove_archived", refuse)
    caplog.set_level(logging.INFO, logger="hermes_cli")
    assert helpers._gc() == 0
    assert _keep_lines(capsys) == [
        f"Scratch preserved: reason=detach_refused path={path} task={tid}; "
        "owner changed?while archiving ? ?x?"]
    assert f"Keeping archived scratch workspace for task {tid} (or detached recovery tree)" in caplog.text
    assert path.exists()


def test_refusal_without_backoff_state_reads_as_a_first_attempt(remote, caplog):
    tid, path = _refused(remote)
    with kbc.connect_closing() as conn:
        assert gc._archive_and_remove_scratch(conn, tid, path, OLD) == 0
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Scratch preserved: ")]
    assert line.startswith(f"Scratch preserved: reason=archive_refused path={path} task={tid} "
                           "attempts=1 retry_at=None; ")


def test_keep_detail_is_one_line_in_the_relay_alphabet_cut_to_200():
    safe = "aZ09_./:=,()' -"
    assert bo.keep_detail(safe) == safe
    assert bo.keep_detail("a\nb\tc;d\"e[f]g{h}i\u2603j?k\\l") == "a?b?c?d?e?f?g?h?i?j?k?l"
    assert bo.keep_detail("x" * 500) == "x" * 200
    assert bo.keep_detail(OSError(13, "Permission denied")) == "?Errno 13? Permission denied"


def test_a_line_break_in_a_kept_path_cannot_forge_a_second_stdout_line(capsys):
    root = kb.workspaces_root()
    forged = "Scratch preserved: reason=owner_unverified path=forged"
    odd = root / f"odd\n{forged}"
    try:
        odd.mkdir(parents=True)
    except OSError:
        pytest.skip("this filesystem cannot hold a line break in a name")
    assert helpers._gc() == 0
    assert _keep_lines(capsys) == [
        f"Scratch preserved: reason=owner_unverified path={root}/odd?{forged}; "
        "no local task workspace record; moved or unknown ownership; no deletion deadline"]


def test_other_warnings_of_the_pass_stay_off_stdout(remote, capsys):
    """Only the kept-tree family is printed: a wrapper reads the last stdout line as the result."""
    _task(remote=remote)
    _task(remote=remote, completed_at=OLD - 3600)
    ticks = iter([0.0, 0.0, 5.0])  # started, first check (in budget), second check (spent)
    budget = bo.Budget(1, clock=lambda: next(ticks, 9.0))
    seen = []
    watch = logging.Handler()
    watch.emit = lambda record: seen.append(record.getMessage())
    package = logging.getLogger("hermes_cli")
    package.addHandler(watch)
    try:
        with kanban_ops._keep_lines_on_stdout(), kbc.connect_closing() as conn:
            assert gc.gc_task_scratch(conn, older_than_days=7, budget=budget) == 1
    finally:
        package.removeHandler(watch)
    assert any("Scratch GC budget reached" in message for message in seen)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("fails", [False, True])
def test_stdout_handler_lasts_only_for_the_workspace_pass(monkeypatch, capsys, fails):
    package = logging.getLogger("hermes_cli")
    before = list(package.handlers)
    during = []

    def scratch(conn, **_kwargs):
        during.append([h for h in package.handlers if h not in before])
        if fails:
            raise RuntimeError("pass died")
        return 0

    monkeypatch.setattr(gc, "gc_task_scratch", scratch)
    if fails:
        with pytest.raises(RuntimeError):
            helpers._gc()
    else:
        assert helpers._gc() == 0
    (added,) = during
    assert [type(h) for h in added] == [kanban_ops._KeepLines]
    assert package.handlers == before
    logging.getLogger("hermes_cli.kanban_worktree_gc").warning("Scratch preserved: reason=after_the_pass")
    assert "after_the_pass" not in capsys.readouterr().out

