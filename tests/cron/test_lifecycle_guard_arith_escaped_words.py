"""Real Bash regressions: escaped substitutions in arithmetic words (t_01a1db0af44d1601).

``let`` / ``[[ … -eq … ]]`` / ``declare -i`` operands are dequoted before bash evaluates
them as arithmetic, and an array subscript there runs its command substitution. The raw
text shows no substitution (``let x=a[\\$\\(S\\)]``), so the referenced-script walk never
read ``S``: a script holding ``hermes gateway restart`` ran unscanned.
"""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_arith_words import iter_arithmetic_word_payloads

SHAPES = [
    r"let x=a[\`SCRIPT\`]",
    r"let x=a[\$\(SCRIPT\)]",
    r"let x=a\[\$\(SCRIPT\)\]",
    r"let x=a[$\(SCRIPT\)]",
    r"let x=a['$'\(SCRIPT\)]",
    r"let x=a[' '\$\(SCRIPT\)]",
    r"[[ 1 -eq a\[\$\(SCRIPT\)\] ]]",
    r"let a[\$\(SCRIPT\)]=1",
    r"builtin let x=a[\$\(SCRIPT\)]",
    r"if let x=a[\$\(SCRIPT\)]; then :; fi",
    r"declare -i x=a[\$\(SCRIPT\)]",
    r"[[ 1 -eq 1 && 2 -eq a\[\$\(SCRIPT\)\] ]]",
]

# Bash does not run SCRIPT for these: echoed words are data, and arithmetic with no
# subscript does not expand the substitution text it receives. `echo let x=a[\$\(S\)]`
# is inert too, but main's coarse printed-sink check already refuses it (fail-closed),
# so its twin is asserted at payload level in test_arithmetic_word_payloads.
CONTROLS = [
    r"echo x=a[\$\(SCRIPT\)]",
    r"let x=\$\(SCRIPT\)",
    r"declare x=a[\$\(SCRIPT\)]",
    r"[[ x == a\[\$\(SCRIPT\)\] ]]",
]


def _run_bash(tmp_path, command):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution proof")
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"touch '{marker}'\nprintf 0\n", encoding="utf-8")
    script.chmod(0o755)
    subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=False,
    )
    return marker.exists(), script


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SHAPES)
def test_escaped_arithmetic_word_scripts_are_scanned(tmp_path, shape):
    script = tmp_path / "payload.sh"
    command = shape.replace("SCRIPT", str(script))
    ran, script = _run_bash(tmp_path, command)
    assert ran, "bash must run the referenced script for this shape"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    # Clean twin: the same reference to a harmless script stays allowed.
    script.write_text("echo hello\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", CONTROLS)
def test_inert_escaped_words_are_not_followed(tmp_path, shape):
    script = tmp_path / "payload.sh"
    command = shape.replace("SCRIPT", str(script))
    ran, script = _run_bash(tmp_path, command)
    assert not ran
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize(("command", "payloads"), [
    (r"let x=a[\`S\`]", ["x=a[$(S)]"]),
    (r"let x=a[\$\(S\)] y=a[\$\(S\)]", ["x=a[$(S)]", "y=a[$(S)]"]),
    (r"let x=a[\$\(S\)] x=a[\$\(S\)]", ["x=a[$(S)]"]),
    (r"declare -ai x=a[\$\(S\)]", ["x=a[$(S)]"]),
    (r"declare -a x=a[\$\(S\)]", []),
    (r"declare -- x=a[\$\(S\)]", []),
    (r"test 1 -eq a[\$\(S\)]", ["a[$(S)]"]),
    (r"[[ a[\$\(S\)] -lt 1 ]]", ["a[$(S)]"]),
    (r"let x=1 y=\$\(S\)", []),
    (r"let x=a[1]", []),
    ("let", []),
    ("-eq", []),
    (r"-eq 1 a[\$\(S\)]", []),
    (r"xi=a[\$\(S\)]", []),
    (r"declare xi=a[\$\(S\)]", []),
    (r"echo let x=a[\$\(S\)]", []),
])
def test_arithmetic_word_payloads(command, payloads):
    assert list(iter_arithmetic_word_payloads(command)) == payloads


@pytest.mark.parametrize("command", [
    "echo hello world | grep x",
    "let x=a[1]",
    "echo $HOME",
    "echo `date`",
])
def test_prefilter_skips_tokenization(monkeypatch, command):
    from cron import lifecycle_guard

    def tripwire(_command):
        raise AssertionError("tokenized a command the prefilter should skip")

    monkeypatch.setattr(lifecycle_guard, "_iter_command_segments", tripwire)
    assert list(iter_arithmetic_word_payloads(command)) == []
