"""``kanban show`` says what wakes a scheduled card, on the status line itself.

Regression for defect agent-3a632ca054: an agent read ``hermes kanban show`` through
``grep "status:|→"`` and ``tail``, which kept ``status: scheduled`` and the run line's raw
``SCHEDULED_UNTIL=…`` and dropped the separate ``wake check:`` field. It told David the card
"checks back at 18:05", but 18:05 was only the backstop: the card had a wake check that would
wake it the moment the awaited card landed. Every card-status surface (CLI text, ``--json``,
the kanban_show tool's worker_context) now states "wakes when X passes, at latest T". [daveey]
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stamp(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _park(kanban_home: Path, *, with_check: bool) -> tuple[str, int, str]:
    wake = int(time.time()) + 3 * 3600
    until = datetime.fromtimestamp(wake, tz=UTC).isoformat().replace("+00:00", "Z")
    probe = f"test -f {kanban_home}/mirror-landed"  # never written: the park-time dry run must not pass
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="fix card", assignee="alice")
    flags = f"--wake-check '{probe}'" if with_check else "--clock-only"
    out = kc.run_slash(f"block {tid} 'SCHEDULED_UNTIL={until} waiting on the mirror card' --kind time {flags}")
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled", out
    return tid, wake, probe


def _status_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if "status:" in line]


def test_status_line_names_the_wake_check_and_labels_the_deadline_a_backstop(kanban_home: Path) -> None:
    tid, wake, probe = _park(kanban_home, with_check=True)
    status = _status_lines(kc.run_slash(f"show {tid}"))
    assert len(status) == 1, status
    line = status[0]
    assert line.split()[1] == "scheduled"  # `awk '{print $2}'` parsers keep working
    assert f"wakes when `{probe}` passes" in line
    assert f"backstop {_stamp(wake)}" in line
    assert "next probe" in line


def test_status_line_without_a_check_says_wakes_at(kanban_home: Path) -> None:
    tid, wake, _ = _park(kanban_home, with_check=False)
    (line,) = _status_lines(kc.run_slash(f"show {tid}"))
    assert f"wakes at {_stamp(wake)}" in line
    assert "backstop" not in line and "wakes when" not in line


def test_json_has_a_wakes_object(kanban_home: Path) -> None:
    tid, wake, probe = _park(kanban_home, with_check=True)
    data = json.loads(kc.run_slash(f"show {tid} --json"))
    wakes = data["wakes"]
    assert wakes["check"] == probe
    assert wakes["backstop_at"] == wake
    assert isinstance(wakes["next_probe_at"], int)

    tid2, wake2, _ = _park(kanban_home, with_check=False)
    wakes2 = json.loads(kc.run_slash(f"show {tid2} --json"))["wakes"]
    assert wakes2 == {"check": None, "next_probe_at": None, "backstop_at": wake2}


def test_unscheduled_card_has_no_wake(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ready card", assignee="alice")
    out = kc.run_slash(f"show {tid}")
    (line,) = _status_lines(out)
    assert "wakes" not in line
    assert json.loads(kc.run_slash(f"show {tid} --json"))["wakes"] is None


def test_worker_context_states_the_wake_in_one_line(kanban_home: Path) -> None:
    tid, wake, probe = _park(kanban_home, with_check=True)
    with kbc.connect_closing() as conn:
        ctx = kb.build_worker_context(conn, tid)
    wakes = [line for line in ctx.splitlines() if line.startswith("Wakes:")]
    assert len(wakes) == 1, ctx
    assert f"when `{probe}` passes" in wakes[0] and f"backstop {_stamp(wake)}" in wakes[0]

    tid2, wake2, _ = _park(kanban_home, with_check=False)
    with kbc.connect_closing() as conn:
        ctx2 = kb.build_worker_context(conn, tid2)
    assert f"Wakes:    at {_stamp(wake2)}" in ctx2


def test_kanban_show_tool_returns_wakes(kanban_home: Path) -> None:
    from tools.kanban_show import show

    tid, wake, probe = _park(kanban_home, with_check=True)
    out = json.loads(show({"task_id": tid}))
    assert out["wakes"]["check"] == probe and out["wakes"]["backstop_at"] == wake
    assert f"when `{probe}` passes" in out["worker_context"]
