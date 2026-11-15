"""A `|` + shell name inside a data sink's quoted pattern is not a pipe into a shell
(self-heal lifecycleguard-f82a8e8dd311).

A hub chat's read-only ``grep -n -E "...|\\[bash, ?\\"-[lc]|..." tools/environments/local.py ...``
was refused with "the scan depth limit was reached (depth 8)": the quote-blind sink regex took
``|\\[bash`` inside the grep pattern for a pipe, so every adjacent quote pair was walked as printed
shell code. The escaped ``\\"`` shifted pairing, an unquoted span put ``local.py`` in command
position, and the walk recursed local.py -> ~/.bashrc -> bash_completion past depth 8.
"""

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_output import iter_printed_shell_payloads

# The incident command verbatim, except the script paths (moved under tmp_path).
_INCIDENT = (
    'grep -rn "oom_score_adj\\|OOMScoreAdjust\\|choom" --include=*.py tools 2>/dev/null'
    ' | grep -v "/tests/" | head -6 | cut -c1-200;'
    ' echo "=== where local terminal commands get wrapped";'
    ' grep -n -E "def (_wrap|_build|_prepare|_compose)[a-z_]*command|def _run_bash|def execute\\(|'
    '\\[bash, ?\\"-[lc]|_find_bash\\(\\)" {s0} {s0b} 2>/dev/null | head -10 | cut -c1-200;'
    ' grep -rn "HERMES_KANBAN_TASK" tools/environments/*.py 2>/dev/null | head -5 | cut -c1-200'
)


def _chain(tmp_path, depth=12):
    """s0.sh sources s1.sh ... : executing s0 walks past the depth limit (as local.py did)."""
    for i in range(depth):
        (tmp_path / f"s{i}.sh").write_text(f". {tmp_path}/s{i + 1}.sh\n", encoding="utf-8")
    (tmp_path / f"s{depth}.sh").write_text("true\n", encoding="utf-8")
    (tmp_path / "s0b.sh").write_text("true\n", encoding="utf-8")
    return _INCIDENT.replace("{s0}", str(tmp_path / "s0.sh")).replace("{s0b}", str(tmp_path / "s0b.sh"))


def test_quoted_grep_pattern_pipe_is_no_sink(tmp_path):
    command = _chain(tmp_path)
    assert list(iter_printed_shell_payloads(command)) == []


def test_quoted_grep_pattern_pipe_is_allowed(tmp_path):
    command = _chain(tmp_path)
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


# Real sinks around the same pattern keep the full (fail-closed) walk.
_REAL_SINKS = {
    "pipe-to-bash-after": 'echo "bash {s0}" | grep -E "a|\\[bash" | bash',
    "grep-output-to-bash": 'grep -h "bash" {s0b} | bash; echo "bash {s0}"',
    "subst-in-pattern": 'grep -E "$(echo x)|\\[bash" {s0b}; echo "bash {s0}" | bash',
    "unquoted-pipe-bash": 'echo "bash {s0}" | bash',
}


def test_real_sinks_still_fail_closed(tmp_path):
    _chain(tmp_path)
    for name, shape in _REAL_SINKS.items():
        command = shape.replace("{s0}", str(tmp_path / "s0.sh")).replace(
            "{s0b}", str(tmp_path / "s0b.sh"))
        unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe is True and refusal is not None, (name, refusal)


def test_shell_sink_callers_unchanged():
    command = 'grep -E "a|\\[bash" f; echo "bash x"'
    assert "bash x" in [payload for payload, _ in
                        iter_printed_shell_payloads(command, shell_sink=True)]
