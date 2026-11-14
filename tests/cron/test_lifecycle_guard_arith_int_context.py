"""Integer attributes survive eval parsing and may be established by opaque declarations."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


SHAPES = [
    r'let "a[\`SCRIPT\`]=1"',
    r"declare -i x; eval 'x=a[\$\(SCRIPT\)]'",
    r"declare -i x; eval 'x=a[\`SCRIPT\`]'",
    r"typeset -i x; eval 'x+=a[\$\(SCRIPT\)]'",
    r"f(){ local -i x; eval 'x=a[\$\(SCRIPT\)]'; }; f",
    r"d=declare; $d -i x; x=a[\$\(SCRIPT\)]",
    r"d=declare; ${d} -i x; x=a[\$\(SCRIPT\)]",
    r"o=-i; declare $o x; x=a[\$\(SCRIPT\)]",
    r"o=-i; builtin declare $o x; x=a[\$\(SCRIPT\)]",
    r"d=declare; builtin $d -i x; eval 'x=a[\$\(SCRIPT\)]'",
    r"source ./decl.sh; x=a[\$\(SCRIPT\)]",
    r". ./decl.sh; eval 'x=a[\$\(SCRIPT\)]'",
]
CONTROLS = [
    r"eval 'x=a[\$\(SCRIPT\)]'",
    r"declare x; eval 'x=a[\$\(SCRIPT\)]'",
    r"declare +i x; eval 'x=a[\$\(SCRIPT\)]'",
    r"declare -i x; eval 'x=\$\(SCRIPT\)'",
]


def _bash_marker(tmp_path, shape):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash required for execution proof")
    (tmp_path / "decl.sh").write_text("declare -i x\n", encoding="utf-8")
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"touch '{marker}'\nprintf 0\n", encoding="utf-8")
    script.chmod(0o755)
    command = shape.replace("SCRIPT", str(script))
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        timeout=10, check=False,
    )
    return marker, script, command, result


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SHAPES)
def test_integer_context_reaches_assignments(tmp_path, shape):
    marker, script, command, result = _bash_marker(tmp_path, shape)
    assert marker.exists(), result.stderr
    marker.unlink()
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    script.write_text(f"touch '{marker}'\nprintf 0\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    assert not marker.exists(), "scanning must never execute the inspected text"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", CONTROLS)
def test_noninteger_eval_controls(tmp_path, shape):
    marker, script, command, _result = _bash_marker(tmp_path, shape)
    assert not marker.exists()
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


def test_integer_context_is_per_scan(tmp_path):
    script = tmp_path / "payload.sh"
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assignment = rf"x=a[\$\({script}\)]"
    assert scan_gateway_lifecycle("declare -i x; " + assignment)[0]
    assert scan_gateway_lifecycle(assignment) == (False, None)
