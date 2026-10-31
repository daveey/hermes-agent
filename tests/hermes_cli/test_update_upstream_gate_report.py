"""What `hermes update` shows of the upstream-sync gate's output: the verdict stays visible.

The gate (scripts/ci/upstream_sync.py) prints evidence lines under a red sync's REFUSED line
(scripts/ci/upstream_sync_diagnostics.py). The updater used to print only the last 4 lines, so those
evidence lines pushed the REFUSED line, its run URL and the failing job out of view. The gate here is a
spawned fixture script, so the real subprocess path runs."""
from hermes_cli import update_cmd_git


def _gate(tmp_path, stdout_lines, code):
    gate = tmp_path / "scripts" / "ci" / "upstream_sync.py"
    gate.parent.mkdir(parents=True)
    gate.write_text(f"import sys\nprint({chr(10).join(stdout_lines)!r})\nsys.exit({code})\n")
    return gate


def test_refused_line_and_evidence_survive_long_diagnostics(tmp_path, capsys):
    diagnostics = ["  required job failure: Full suite / e2e https://ci.example/job/21 — 9 failing test(s)",
                   *[f"      - tests/e2e/test_{n}.py::test_x" for n in range(9)],
                   "  main baseline: bbbbbbbbbb (the sync's first parent, on main)",
                   "  e2e: main-baseline failure: 9 failing test(s) also fail on main bbbbbbbbbb",
                   "  nightly-e2e owner for this exact failing set: t_owner (running)"]
    _gate(tmp_path, ["LANDED: an earlier candidate", "REFUSED: the full suite failed on aaaaaaaaaa: "
                     "https://ci.example/run/20", *diagnostics], 1)
    assert update_cmd_git._sync_fork_with_upstream(["git"], tmp_path) is False
    out = capsys.readouterr().out
    assert "REFUSED: the full suite failed on aaaaaaaaaa: https://ci.example/run/20" in out
    assert "required job failure: Full suite / e2e" in out and "t_owner (running)" in out
    assert "LANDED: an earlier candidate" not in out  # starts at the last verdict


def test_short_gate_output_is_still_its_last_four_lines(tmp_path, capsys):
    _gate(tmp_path, ["one", "LANDED x", "two", "NOT YET: staged y", "three"], 2)
    assert update_cmd_git._sync_fork_with_upstream(["git"], tmp_path) is False
    out = capsys.readouterr().out
    assert "one" not in out and all(s in out for s in ("LANDED x", "two", "NOT YET: staged y", "three"))


def test_report_is_capped():
    lines = ["REFUSED: red", *[f"  line {n}" for n in range(100)]]
    shown = update_cmd_git._gate_report("\n".join(lines))
    assert shown[0] == "REFUSED: red" and len(shown) == update_cmd_git._GATE_REPORT_LINES + 1
    assert shown[-1] == f"… {101 - update_cmd_git._GATE_REPORT_LINES} more line(s)"
