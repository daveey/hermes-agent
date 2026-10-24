"""scripts/ci/flaky_kanban.py: one deflake card per flaky test id (t_94f2c8ba).

The end-to-end case runs the real per-file runner on a probe suite with one
flaky and one hard-failing test, then files cards on a real (isolated) board
through the `hermes kanban` CLI: the flaky test passes and gets a card, the
hard failure keeps the run red and gets none.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("flaky_kanban", _ROOT / "scripts" / "ci" / "flaky_kanban.py")
fk = importlib.util.module_from_spec(_spec)
sys.modules["flaky_kanban"] = fk
_spec.loader.exec_module(fk)


class FakeBoard:
    def __init__(self):
        self.cards: dict[str, dict] = {}
        self.keys: dict[str, str] = {}

    def create(self, title, body, key):
        if key in self.keys and self.cards[self.keys[key]]["status"] != "archived":
            return self.keys[key]
        card = f"t_{len(self.cards) + 1}"
        self.cards[card] = {"title": title, "body": body, "status": "ready", "comments": [], "key": key}
        self.keys[key] = card
        return card

    def status(self, card):
        return self.cards.get(card, {}).get("status")

    def comment(self, card, body):
        self.cards[card]["comments"].append(body)


def _entry(test_id, excerpt="AssertionError: boom"):
    return {"test_id": test_id, "file": test_id.split("::")[0], "retried": "tests",
            "first_attempt_excerpt": excerpt}


def test_one_card_per_test_id_then_comments_while_it_is_open():
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a, b = "tests/x.py::test_a", "tests/x.py::T::test_b[p - q]"

    log = fk.file_cards([_entry(a), _entry(b), _entry(a)], run_url="https://r/1", branch="main",
                        board=board, state=state)
    assert [line.split()[0] for line in log] == ["opened", "opened"]
    assert sorted(c["title"] for c in board.cards.values()) == sorted([f"Deflake {a}", f"Deflake {b}"])
    assert board.cards[state["cards"][a]["card"]]["key"] == f"deflake:{a}"
    assert "https://r/1" in board.cards[state["cards"][a]["card"]]["body"]

    log = fk.file_cards([_entry(a, "second traceback")], run_url="https://r/2", branch="pr", board=board, state=state)
    assert log == [f"appended {state['cards'][a]['card']} {a}"]
    assert len(board.cards) == 2
    assert "https://r/2" in board.cards[state["cards"][a]["card"]]["comments"][0]


def test_flake_after_the_card_was_closed_opens_a_linked_new_card():
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a = "tests/x.py::test_a"
    fk.file_cards([_entry(a)], run_url="https://r/1", branch="main", board=board, state=state)
    first = state["cards"][a]["card"]
    board.cards[first]["status"] = "done"

    fk.file_cards([_entry(a)], run_url="https://r/2", branch="main", board=board, state=state)
    second = state["cards"][a]["card"]
    assert second != first
    assert board.cards[second]["key"] == f"deflake:{a}#1"
    assert first in board.cards[second]["body"]
    assert board.cards[first]["comments"] == []


def test_annotations_round_trip_through_the_github_poll(monkeypatch):
    """CI prints FLAKY annotations; the hub poll reads them back into one card per id."""
    excerpt = "E   AssertionError: 100% starved\nline two"
    lines = fk.annotations([_entry("tests/x.py::test_a", excerpt), _entry("tests/y.py")])
    assert lines[0].startswith("::warning title=FLAKY (passed only on retry)::tests/x.py::test_a%0A")
    assert "100%25 starved" in lines[0]                      # workflow-command escaping
    messages = [line.split("::", 2)[2].replace("%0A", "\n").replace("%25", "%") for line in lines]

    api = "https://api.github.com/repos/o/r"
    calls = []

    def fake_gh(url, token):
        calls.append(url)
        if url.startswith(f"{api}/actions/runs?"):
            return {"workflow_runs": [{"id": 5, "run_attempt": 1, "head_branch": "main",
                                       "html_url": "https://github.com/o/r/actions/runs/5"}]}
        if url.startswith(f"{api}/actions/runs/5/attempts/1/jobs"):
            return {"jobs": [{"id": 9, "name": "Python tests / Run tests (1/2)", "html_url": "https://job/9"},
                             {"id": 10, "name": "Lint", "html_url": "https://job/10"}]}
        if url.startswith(f"{api}/check-runs/9/annotations"):
            return [{"title": "FLAKY (passed only on retry)", "message": m} for m in messages] + [
                {"title": "other warning", "message": "tests/z.py::nope"}]
        raise AssertionError(url)

    monkeypatch.setattr(fk, "_gh", fake_gh)
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    log = fk.poll_github("o/r", "tok", board, state)
    assert [line.split()[0] for line in log] == ["opened", "opened"]
    assert sorted(c["title"] for c in board.cards.values()) == ["Deflake tests/x.py::test_a", "Deflake tests/y.py"]
    card_a = board.cards[state["cards"]["tests/x.py::test_a"]["card"]]
    assert "100% starved" in card_a["body"] and "https://job/9" in card_a["body"]
    assert state["cards"]["tests/y.py"] and state["runs"] == ["5/1"]
    assert not any("check-runs/10" in u for u in calls)      # only test jobs are read

    # The same run attempt is never filed twice.
    assert fk.poll_github("o/r", "tok", board, state) == []
    assert len(board.cards) == 2


def test_annotate_mode_tolerates_a_missing_report(tmp_path, capsys):
    assert fk.main(["--annotate", str(tmp_path / "absent.json")]) == 0
    assert "no flaky report" in capsys.readouterr().out


def test_state_round_trips_and_rejects_unknown_report_versions(tmp_path):
    path = tmp_path / "s.json"
    state = fk.load_state(path)
    state["cards"]["t"] = {"card": "t_1", "generation": 0}
    state["runs"].append("7/1")
    fk.save_state(path, state)
    assert fk.load_state(path)["cards"]["t"]["card"] == "t_1"
    with pytest.raises(ValueError):
        fk.read_report(json.dumps({"version": 2, "flaky": []}))


@pytest.mark.platforms("posix")
def test_flaky_test_passes_and_files_a_card_while_a_hard_failure_stays_red(tmp_path, monkeypatch):
    # Real runner: copy it the way tests/scripts/test_run_tests_parallel.py does.
    root = tmp_path / "runner-root"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(_ROOT / "scripts" / "run_tests_parallel.py", root / "scripts")
    shutil.copy2(_ROOT / "scripts" / "ci" / "list_os_marked_tests.py", root / "scripts" / "ci")
    marker = tmp_path / "flaked-once"
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_flaky_probe.py").write_text(textwrap.dedent(f"""
        from pathlib import Path

        def test_steady():
            assert True

        def test_flaky_under_load():
            m = Path({str(marker)!r})
            if not m.exists():
                m.write_text("x")
                assert False, "first attempt starved"
    """), encoding="utf-8")
    (suite / "test_hard_probe.py").write_text(
        "def test_real_regression():\n    assert 1 == 2, 'real regression'\n", encoding="utf-8")
    report = tmp_path / "flaky.json"

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_tests_parallel.py"),
         "--files", f"{suite / 'test_flaky_probe.py'}:{suite / 'test_hard_probe.py'}",
         "--file-retries", "1", "-j", "2", "--flaky-report", str(report)],
        cwd=tmp_path, capture_output=True, text=True, timeout=180, check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out                       # the hard failure keeps the run red
    assert "real regression" in out and "still failing" in out
    flaky = fk.read_report(report.read_text(encoding="utf-8"))
    assert [e["test_id"].rsplit("/", 1)[-1] for e in flaky] == ["test_flaky_probe.py::test_flaky_under_load"]

    # File it on an isolated real board through the hermes kanban CLI.
    home = tmp_path / "hermes-home"
    home.mkdir()
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_SESSION_ID",
                "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    board = fk.CliBoard([sys.executable, "-m", "hermes_cli.main"])
    state_path = tmp_path / "state.json"
    state = fk.load_state(state_path)
    fk.file_cards(flaky, run_url="https://ci/run/1", branch="main", board=board, state=state)
    fk.file_cards(flaky, run_url="https://ci/run/2", branch="main", board=board, state=state)

    listed = subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "list", "--json"],
                            capture_output=True, text=True, timeout=120, check=True, cwd=_ROOT)
    cards = json.loads(listed.stdout)
    assert len(cards) == 1, cards                         # one card, the second flake appended
    assert cards[0]["title"].startswith("Deflake ")
    assert cards[0]["title"].endswith("test_flaky_probe.py::test_flaky_under_load")
    assert "first attempt starved" in cards[0]["body"]
    shown = json.loads(subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "show", cards[0]["id"], "--json"],
        capture_output=True, text=True, timeout=120, check=True, cwd=_ROOT).stdout)
    assert any("https://ci/run/2" in c["body"] for c in shown["comments"])
