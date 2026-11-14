"""Real Bash regressions: escaped substitutions assigned to integer variables (t_01a1db20266865b2).

After ``declare -i x`` (or ``typeset -i``/``local -i``), a later plain ``x=a[\\$\\(S\\)]`` is
dequoted to ``a[$(S)]`` and then evaluated as arithmetic, so the subscript runs ``S``.
The raw text shows no substitution and the word is no ``let``/``declare -i`` operand, so
the referenced-script walk never read ``S``: a script holding ``hermes gateway restart``
ran unscanned.
"""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_arith_words import iter_arithmetic_word_payloads

SHAPES = [
    r"declare -i x; x=a[\$\(SCRIPT\)]",
    r"typeset -i x; x=a[\$\(SCRIPT\)]",
    r"f(){ local -i x; x=a[\$\(SCRIPT\)]; }; f",
    r"declare -i x; x+=a[\$\(SCRIPT\)]",
    r"declare -ai x; x[0]=a[\$\(SCRIPT\)]",
    r"declare -i x; x=a[\`SCRIPT\`]",
    r"declare -i x; x=a[$\(SCRIPT\)]",
    "declare -i x\nx=a[\\$\\(SCRIPT\\)]",
    r"declare -i x && x=a[\$\(SCRIPT\)]",
    r"declare -xi x; x=a[\$\(SCRIPT\)]",
    r"declare -i -x x; x=a[\$\(SCRIPT\)]",
    r"declare -i x; declare x=a[\$\(SCRIPT\)]",
    r'eval "declare -i x"; x=a[\$\(SCRIPT\)]',
    r"declare -i x; for x in a[\$\(SCRIPT\)]; do :; done",
    r"declare -i x; x='a[$(SCRIPT)]'",
]

# Bash does not run SCRIPT for these: no integer attribute is set, the assigned
# word has no subscript, or the word is plain data.
CONTROLS = [
    r"x=a[\$\(SCRIPT\)]",
    r"declare x; x=a[\$\(SCRIPT\)]",
    r"declare +i x; x=a[\$\(SCRIPT\)]",
    r"declare -i x; x=\$\(SCRIPT\)",
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
def test_integer_assignment_scripts_are_scanned(tmp_path, shape):
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
def test_assignments_without_integer_arithmetic_are_inert(tmp_path, shape):
    script = tmp_path / "payload.sh"
    command = shape.replace("SCRIPT", str(script))
    ran, script = _run_bash(tmp_path, command)
    assert not ran
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_plain_assignment_yields_no_payload():
    assert list(iter_arithmetic_word_payloads(r"x=a[\$\(S\)]")) == []
    assert list(iter_arithmetic_word_payloads(r"declare x; x=a[\$\(S\)]")) == []


def test_integer_declaration_makes_assignments_payloads():
    assert list(iter_arithmetic_word_payloads(r"declare -i x; x=a[\$\(S\)]")) == ["x=a[$(S)]"]
    assert list(iter_arithmetic_word_payloads(r"local -i x; x+=a[\`S\`]")) == ["x+=a[$(S)]"]
