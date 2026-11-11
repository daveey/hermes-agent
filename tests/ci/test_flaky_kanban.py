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

    def create(self, title, body, key, *, distinct_from=None, distinct_reason=None):
        if key in self.keys and self.cards[self.keys[key]]["status"] != "archived":
            return self.keys[key]
        card = f"t_{len(self.cards) + 1}"
        self.cards[card] = {"title": title, "body": body, "status": "ready", "comments": [], "key": key}
        self.keys[key] = card
        return card

    def status(self, card):
        return self.cards.get(card, {}).get("status")

    def card(self, card, board=None):
        c = self.cards.get(card)
        return {"id": card, "status": c["status"], "title": c["title"], "body": c["body"]} if c else None

    def comment(self, card, body, board=None):
        self.cards[card]["comments"].append(body)

    def list_cards(self):
        return [{"id": cid, "status": c["status"], "title": c["title"], "body": c["body"]}
                for cid, c in self.cards.items()]

    def comments(self, card, board=None):
        return list(self.cards.get(card, {}).get("comments", []))

    def add_foreign(self, card, status, body, title="ci-watch: fix it", comments=()):
        """A card some other worker filed: not in the cron's state, no deflake key."""
        self.cards[card] = {"title": title, "body": body, "status": status, "comments": list(comments), "key": None}


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


def test_job_url_a_done_foreign_card_already_cites_is_not_refiled():
    """Regression: t_01a185af6885ba83 refiled job 123202653589, which done card t_01a1858daf93c588
    (filed by the CI-watch worker, so absent from the cron's state) had already fixed."""
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a = "tests/x.py::test_a"
    board.add_foreign("t_fixed", "done", f"`{a}` failed in https://job/9 ; fixed by abc123")

    log = fk.file_cards([_entry(a)], run_url="https://job/9", branch="main", board=board, state=state)
    assert list(board.cards) == ["t_fixed"]                     # nothing new opened
    assert board.cards["t_fixed"]["comments"] == []
    assert log == [f"skipped {a}: https://job/9 already cited by t_fixed (done)"]
    assert state["cards"][a] == {"card": "t_fixed", "generation": 0}

    # A later flake from a different job, after that fix, is a new generation linking the old card.
    log = fk.file_cards([_entry(a)], run_url="https://job/99", branch="main", board=board, state=state)
    new = state["cards"][a]["card"]
    assert new != "t_fixed" and log == [f"opened {new} {a}"]
    assert board.cards[new]["key"] == f"deflake:{a}#1"
    assert "t_fixed" in board.cards[new]["body"] and "https://job/99" in board.cards[new]["body"]


def test_github_run_url_plus_job_id_counts_as_citing_the_job_url():
    """The real done card cited `.../runs/40798982916 ... job 123202653589`, not the job URL."""
    run = "https://github.com/daveey/hermes/actions/runs/40798982916"
    a = "tests/tui_gateway/test_process_consumer_health.py::test_refused_turn_never_certifies[None]"
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    board.add_foreign("t_fixed", "done", f"Fix flake in {a}. Observed push {run} head 5e206f07aa job 123202653589 on gw.")
    board.add_foreign("t_other_job", "done", f"Fix flake in {a}. Observed {run} job 123202653580.")

    log = fk.file_cards([_entry(a)], run_url=f"{run}/job/123202653589", branch="main", board=board, state=state)
    assert log == [f"skipped {a}: {run}/job/123202653589 already cited by t_fixed (done)"]
    assert sorted(board.cards) == ["t_fixed", "t_other_job"]

    log = fk.file_cards([_entry(a)], run_url=f"{run}/job/123202653999", branch="main", board=board, state=state)
    assert log[0].startswith("opened ")          # same run, a different job: not the same incident


def test_job_url_cited_only_in_a_done_cards_comment_is_not_refiled():
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a = "tests/x.py::test_a"
    board.add_foreign("t_fixed", "done", f"investigating `{a}`", comments=["also seen in https://job/9"])
    board.add_foreign("t_open_old", "ready", f"older triage of {a}", comments=["seen in https://job/1"])

    log = fk.file_cards([_entry(a)], run_url="https://job/9", branch="main", board=board, state=state)
    assert sorted(board.cards) == ["t_fixed", "t_open_old"]
    assert log == [f"skipped {a}: https://job/9 already cited by t_fixed (done)"]
    assert board.cards["t_open_old"]["comments"] == ["seen in https://job/1"]  # the job match wins
    assert state["cards"][a] == {"card": "t_fixed", "generation": 0}


def test_job_url_cited_by_an_open_card_appends_there():
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a = "tests/x.py::test_a"
    board.add_foreign("t_open", "running", f"CI red: https://job/9 ({a})", title="ci-watch: main red")
    board.add_foreign("t_sibling", "ready", "Deflake `tests/x.py::test_b` from https://job/9")  # same job, other id

    log = fk.file_cards([_entry(a, "new tail")], run_url="https://job/9", branch="main", board=board, state=state)
    assert sorted(board.cards) == ["t_open", "t_sibling"]
    assert board.cards["t_sibling"]["comments"] == []
    assert log == [f"appended t_open {a}"]
    assert "new tail" in board.cards["t_open"]["comments"][-1]
    assert state["cards"][a] == {"card": "t_open", "generation": 0}


def test_open_foreign_card_for_the_test_id_is_commented_not_duplicated():
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    a = "tests/x.py::test_a"
    board.add_foreign("t_other", "ready", f"Deflake `{a}` (seen in https://job/1)")
    board.add_foreign("t_prefix", "ready", "Deflake `tests/x.py::test_ab`")   # different test id
    board.add_foreign("t_closed", "done", f"old fix for {a}")                  # closed, no job match

    log = fk.file_cards([_entry(a)], run_url="https://job/2", branch="main", board=board, state=state)
    assert sorted(board.cards) == ["t_closed", "t_other", "t_prefix"]
    assert log == [f"appended t_other {a}"]
    assert "https://job/2" in board.cards["t_other"]["comments"][0]
    assert board.cards["t_prefix"]["comments"] == []
    assert state["cards"][a]["card"] == "t_other"


def test_annotations_still_render_locally_with_escaping():
    """--annotate keeps printing FLAKY warnings for a local report (the cron no longer reads them)."""
    excerpt = "E   AssertionError: 100% starved\nline two"
    lines = fk.annotations([_entry("tests/x.py::test_a", excerpt), _entry("tests/y.py")])
    assert lines[0].startswith("::warning title=FLAKY (passed only on retry)::tests/x.py::test_a%0A")
    assert "100%25 starved" in lines[0]                      # workflow-command escaping
    assert lines[1] == "::warning title=FLAKY (passed only on retry)::tests/y.py%0AAssertionError: boom"


def test_artifacts_round_trip_through_the_github_poll(monkeypatch):
    """CI uploads metadata artifacts; the hub poll reads them back into one card per id."""
    from scripts.ci import flaky_artifact as fa
    from tests.ci.flaky_artifact_fakes import FakeGitHub, job, meta

    gh = FakeGitHub()
    gh.simple(run_id=5, tests=("tests/x.py::test_a", "tests/y.py"), art_id=100)
    gh.jobs[(5, 1)] = [job(9, 1, 1), job(10, name="Lint")]
    monkeypatch.setattr(fk, "_gh", gh.gh)
    monkeypatch.setattr(fa, "download_zip", gh.download)
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    log = fk.poll_github("o/r", "tok", board, state)
    assert [line.split()[0] for line in log] == ["opened", "opened", "POLL"]
    assert sorted(c["title"] for c in board.cards.values()) == ["Deflake tests/x.py::test_a", "Deflake tests/y.py"]
    card_a = board.cards[state["cards"]["tests/x.py::test_a"]["card"]]
    assert "https://github.com/o/r/actions/runs/5/job/9" in card_a["body"]
    assert state["cards"]["tests/y.py"] and state["runs"] == ["5/1"]
    assert not any("check-runs" in u or "logs" in u for u in gh.urls)          # annotations are not read

    # The same run attempt is never filed twice.
    assert fk.poll_github("o/r", "tok", board, state) == []
    assert len(board.cards) == 2


def test_poll_pages_past_newer_runs_to_reach_a_long_test_run(monkeypatch):
    """A long test run completes after more newer runs than one page holds.

    Regression: PR run 40764402730 (37 min of tests) finished behind 53 newer
    completed runs; the poll read only the newest 40, so its flakes never became cards.
    """
    from scripts.ci import flaky_artifact as fa
    from tests.ci.flaky_artifact_fakes import FakeGitHub

    gh = FakeGitHub()
    for i in range(149):                                # newer runs without Python test jobs
        gh.run(1000 - i)
    slow = 1000 - 149                                   # the slow test run is the oldest
    gh.simple(run_id=slow, tests=("tests/slow.py::test_flaky",), art_id=7)
    monkeypatch.setattr(fk, "_gh", gh.gh)
    monkeypatch.setattr(fa, "download_zip", gh.download)
    board, state = FakeBoard(), fk.load_state(Path("/nonexistent/state.json"))
    log = fk.poll_github("o/r", "tok", board, state)

    assert log[0] == f"opened {state['cards']['tests/slow.py::test_flaky']['card']} tests/slow.py::test_flaky"
    assert f"{slow}/1" in state["runs"] and len(state["runs"]) == len(gh.runs)
    assert log[-1] == "POLL complete=1 empty=0 unavailable=0 not_applicable=149"
    # Bounded by a creation window, not by a page count.
    assert all("created=%3E%3D" in u for u in gh.urls if "/actions/runs?" in u)


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
def test_cli_board_skips_a_job_a_done_card_already_cites(tmp_path, monkeypatch):
    """CliBoard.list_cards/comments on a real isolated board (through `hermes kanban`, not its DB)."""
    # These shell out to `hermes kanban`, which needs the repo env; a bare `uv run --with pytest` lacks python-dotenv.
    pytest.importorskip("dotenv", reason="needs the repo environment (hermes_cli imports python-dotenv)")
    home = tmp_path / "hermes-home"
    home.mkdir()
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_DELEGATED_CHILD_OWNER", "HERMES_SESSION_ID",
                "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    cli = [sys.executable, "-m", "hermes_cli.main", "kanban"]

    def run(*args, stdin=None):
        return subprocess.run([*cli, *args], input=stdin, capture_output=True, text=True, timeout=120,
                              check=True, cwd=_ROOT).stdout

    a, b = "tests/x.py::test_a", "tests/x.py::test_b"
    fixed = json.loads(run("create", "ci-watch: main red", "--body-file", "-", "--json",
                           stdin=f"`{a}` failed in https://job/9"))["id"]
    run("complete", fixed, "--summary", "fixed")
    noted = json.loads(run("create", f"triage {b}", "--body-file", "-", "--json", stdin="flaky"))["id"]
    run("comment", noted, "also seen in https://job/9")
    run("complete", noted, "--summary", "fixed")

    board = fk.CliBoard([sys.executable, "-m", "hermes_cli.main"])
    state = fk.load_state(tmp_path / "state.json")
    log = fk.file_cards([_entry(a), _entry(b)], run_url="https://job/9", branch="main", board=board, state=state)
    assert log == [f"skipped {a}: https://job/9 already cited by {fixed} (done)",
                   f"skipped {b}: https://job/9 already cited by {noted} (done)"]
    assert len(json.loads(run("list", "--archived", "--json"))) == 2

    log = fk.file_cards([_entry(a)], run_url="https://job/99", branch="main", board=board, state=state)
    assert log[0].startswith("opened ")
    new = state["cards"][a]["card"]
    assert fixed in json.loads(run("show", new, "--json"))["task"]["body"]


@pytest.mark.platforms("posix")
def test_flaky_test_passes_and_files_a_card_while_a_hard_failure_stays_red(tmp_path, monkeypatch):
    # These shell out to `hermes kanban`, which needs the repo env; a bare `uv run --with pytest` lacks python-dotenv.
    pytest.importorskip("dotenv", reason="needs the repo environment (hermes_cli imports python-dotenv)")
    # Real runner: copy it the way tests/scripts/test_run_tests_parallel.py does.
    root = tmp_path / "runner-root"
    (root / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(_ROOT / "scripts" / "run_tests_parallel.py", root / "scripts")
    shutil.copy2(_ROOT / "scripts" / "run_tests_host_slots.py", root / "scripts")
    shutil.copy2(_ROOT / "scripts" / "run_tests_parallel_rss.py", root / "scripts")
    shutil.copy2(_ROOT / "scripts" / "run_tests_launch.py", root / "scripts")
    shutil.copy2(_ROOT / "scripts" / "run_tests_fork_child.py", root / "scripts")
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


def test_card_body_requires_merge_before_done():
    """A deflake card is done when the fix is merged and green on main, not when a PR is opened."""
    body = fk._card_body({"test_id": "tests/x.py::test_a", "first_attempt_excerpt": "boom"}, "https://r/1", "main", None)
    assert "Done when the PR is merged to main and the test passes on main's next CI run." in body
    assert "Do not call\nkanban_complete while the PR is open." in body
