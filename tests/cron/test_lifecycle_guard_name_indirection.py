"""Bash name operands and recursively evaluated assignment values must be scanned."""
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from cron.lifecycle_guard_arith_words import iter_arithmetic_word_payloads
from tests.cron.test_lifecycle_guard_arith_escaped_words import _run_bash


SHAPES = [
    r'x="a[\$(SCRIPT)]"; printf -v "$x" 1',
    r'x="a[\$(SCRIPT)]"; read "$x" <<< 1',
    r'x="a[\$(SCRIPT)]"; test -v "$x"',
    r'x="a[\$(SCRIPT)]"; [[ -v $x ]]',
    r'x="a[\$(SCRIPT)]"; declare "$x=1"',
    r'x="a[\$(SCRIPT)]"; declare -n r="$x"; echo $r',
    r'x="a[\$(SCRIPT)]"; echo ${!x}',
    r'x="a[\$(SCRIPT)]"; echo "${!x}"',
    r'x="a[\$(SCRIPT)]"; echo ${!x:-d}',
    *[f'x=\'a[$(SCRIPT)]\'; echo ${{!x{operator}}}' for operator in
      ('-d', '+d', '=d', '?e', '#p', '##p', '%p', '%%p', '/a/b', '//a/b', '^', '^^', ',', ',,', '@Q')],
    *[f'x=\'a[$(SCRIPT)]\'; {setup}; {unset} "$x"' for setup in
      ('a=(1)', 'a=1', 'read -a a <<< 1', 'declare -A a')
      for unset in ('unset', 'unset -v')],
    "declare 'a[$(SCRIPT)]=1'",
    r"declare a[\$\(SCRIPT\)]=1",
    "declare -a 'a[$(SCRIPT)]=1'",
    "typeset 'a[$(SCRIPT)]=1'",
    "f() { local 'a[$(SCRIPT)]=1'; }; f",
    "x='a[$(SCRIPT)]'; echo ${a[x]}",
    "x='a[$(SCRIPT)]'; a[x]=1",
    "x='a[$(SCRIPT)]'; a+=([x]=1)",
    "x='a[$(SCRIPT)]'; s=hello; echo ${s:x}",
    "x='a[$(SCRIPT)]'; set -- hi; echo ${@:x}",
    "x='a[$(SCRIPT)]'; echo $[x]",
    "x='a[$(SCRIPT)]'; [[ -v a[x] ]]",
    "x='a[$(SCRIPT)]'; declare 'a[x]=1'",
    "test -v 'a[$(SCRIPT)]'",
    r"test -v a[\$\(SCRIPT\)]",
    "[ -v 'a[$(SCRIPT)]' ]",
    "printf -v 'a[$(SCRIPT)]' 1",
    r"printf -v a[\$\(SCRIPT\)] 1",
    "read 'a[$(SCRIPT)]' <<< 1",
    "x='a[$(SCRIPT)]'; echo $((x))",
    r"x=a[\$\(SCRIPT\)]; echo $((x))",
    "x='a[$(SCRIPT)]'; (( x ))",
    r"x=a[\`SCRIPT\`]; (( x ))",
    "x='a[$(SCRIPT)]'; let y=x",
    "x='a[$(SCRIPT)]'; [[ x -eq 0 ]]",
    r"declare -i x; x=a[\$\(SCRIPT\)]",
    r"declare -a a=([0]=1); unset a[\$\(SCRIPT\)]",
    r"builtin printf -v a[\$\(SCRIPT\)] 1",
    r"command test -v a[\$\(SCRIPT\)]",
    r"if test -v a[\$\(SCRIPT\)]; then :; fi",
    r"read -r a[\$\(SCRIPT\)] <<< 1",
    r"x=a['$'\(SCRIPT\)]; y=x; echo $((y))",
    "declare x='a[$(SCRIPT)]'; echo $((x))",
    "x='a[$(SCRIPT)]'; test -v 'a[x]'",
    r"x=a[\$\(SCRIPT\)]; [ -v 'a[x]' ]",
    "x='a[$(SCRIPT)]'; printf -v 'a[x]' 1",
    "x='a[$(SCRIPT)]'; read 'a[x]' <<< 1",
    "x='a[$(SCRIPT)]'; declare -a a=([0]=1); unset 'a[x]'",
    "x='a[$(SCRIPT)]'; builtin test -v 'a[x]'",
    "x='a[$(SCRIPT)]'; command printf -v 'a[x]' 1",
]

# Bash versions that stop a shape before evaluating its subscript, as (exit status, stderr
# fragment). These remain scanner sinks: the guard must conservatively cover every Bash even when
# the local shell treats a shape as data. Bash 3.2 (macOS /bin/bash) has no `test -v`/`[[ -v ]]`
# and rejects subscripted `printf -v` names, namerefs, associative arrays, case/@Q indirect
# operators and subscripted `unset` of a scalar; Bash 5.3 no longer runs a substitution found in an
# `unset` subscript.
BASH_DATA = {
    "3.2": {
        "x='a[$(SCRIPT)]'; [[ -v a[x] ]]": (2, "conditional binary operator expected"),
        "test -v 'a[$(SCRIPT)]'": (2, "-v: unary operator expected"),
        r"test -v a[\$\(SCRIPT\)]": (2, "-v: unary operator expected"),
        "[ -v 'a[$(SCRIPT)]' ]": (2, "-v: unary operator expected"),
        "printf -v 'a[$(SCRIPT)]' 1": (2, "not a valid identifier"),
        r"printf -v a[\$\(SCRIPT\)] 1": (2, "not a valid identifier"),
        r"builtin printf -v a[\$\(SCRIPT\)] 1": (2, "not a valid identifier"),
        r"command test -v a[\$\(SCRIPT\)]": (2, "-v: unary operator expected"),
        r"if test -v a[\$\(SCRIPT\)]; then :; fi": (0, "-v: unary operator expected"),
        "x='a[$(SCRIPT)]'; test -v 'a[x]'": (2, "-v: unary operator expected"),
        r"x=a[\$\(SCRIPT\)]; [ -v 'a[x]' ]": (2, "-v: unary operator expected"),
        "x='a[$(SCRIPT)]'; printf -v 'a[x]' 1": (2, "not a valid identifier"),
        "x='a[$(SCRIPT)]'; builtin test -v 'a[x]'": (2, "-v: unary operator expected"),
        "x='a[$(SCRIPT)]'; command printf -v 'a[x]' 1": (2, "not a valid identifier"),
        r'x="a[\$(SCRIPT)]"; printf -v "$x" 1': (2, "not a valid identifier"),
        r'x="a[\$(SCRIPT)]"; test -v "$x"': (2, "-v: unary operator expected"),
        r'x="a[\$(SCRIPT)]"; [[ -v $x ]]': (2, "conditional binary operator expected"),
        r'x="a[\$(SCRIPT)]"; declare -n r="$x"; echo $r': (0, "-n: invalid option"),
        **{f"x='a[$(SCRIPT)]'; echo ${{!x{operator}}}": (1, "bad substitution")
           for operator in ('^', '^^', ',', ',,', '@Q')},
        **{f"x='a[$(SCRIPT)]'; a=1; {unset} \"$x\"": (1, "not an array variable")
           for unset in ('unset', 'unset -v')},
        **{f"x='a[$(SCRIPT)]'; declare -A a; {unset} \"$x\"": (0, "-A: invalid option")
           for unset in ('unset', 'unset -v')},
    },
    "5.3": {
        r"declare -a a=([0]=1); unset a[\$\(SCRIPT\)]": (1, "arithmetic syntax error"),
    },
}


@pytest.fixture(scope="module")
def bash_version():
    bash = shutil.which("bash")
    assert bash is not None, "Bash required for execution proof"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c",
         'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=True,
    )
    return result.stdout


def _run_bash_result(tmp_path, command):
    """``_run_bash`` that also returns Bash's result, to pin why a data shape did not run."""
    bash = shutil.which("bash")
    assert bash is not None, "Bash required for execution proof"
    marker = tmp_path / "ran"
    script = tmp_path / "payload.sh"
    script.write_text(f"touch '{marker}'\nprintf 0\n", encoding="utf-8")
    script.chmod(0o755)
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        encoding="utf-8", timeout=10, check=False,
    )
    return marker.exists(), script, result


def test_bash_data_shapes_are_scanned_shapes():
    for data in BASH_DATA.values():
        assert set(data) <= set(SHAPES)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", SHAPES)
def test_name_and_indirection_scripts_are_scanned(tmp_path, shape, bash_version):
    command = shape.replace("SCRIPT", str(tmp_path / "payload.sh"))
    ran, script, result = _run_bash_result(tmp_path, command)
    data = BASH_DATA.get(bash_version, {})
    if shape in data:
        status, diagnostic = data[shape]
        assert not ran, (result.returncode, result.stderr)
        assert result.returncode == status
        assert diagnostic in result.stderr
    else:
        assert ran, ("Bash must execute the harmless marker script", result.returncode, result.stderr)
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    script.write_text("echo hello\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("shape", [
    r"x=a[\$\(SCRIPT\)]; echo x",
    r"echo a[\$\(SCRIPT\)]",
    r"printf '%s' a[\$\(SCRIPT\)]",
    r"[[ -v a[\$\(SCRIPT\)] ]]",
    r"test -n a[\$\(SCRIPT\)]",
    r"echo 'x=a[$(SCRIPT)]'; echo $((1))",
    r"printf '%s' 'x=a[$(SCRIPT)]; ((x))'",
    "x='a[$(SCRIPT)]'; echo '${a[x]}'",
    "x='a[$(SCRIPT)]'; echo '${s:x}'",
    "x='a[$(SCRIPT)]'; echo '$[x]'",
    r"x='a[$(SCRIPT)]'; echo \${a[x]}",
    "x='a[$(SCRIPT)]'; echo $${a[x]}",
    "x='a[$(SCRIPT)]'; echo 'a[x]=1'",
    "x='a[$(SCRIPT)]'; echo '${s:-x}'",
    "declare x='a[$(SCRIPT)]'; echo x",
    "x='a[$(SCRIPT)]'; test -n 'a[x]'",
    "x='a[$(SCRIPT)]'; printf '%s' 'a[x]'",
    "x='a[$(SCRIPT)]'; echo 'a[x]'",
    "x='a[$(SCRIPT)]'; test -v x",
    "x='a[$(SCRIPT)]'; printf -v x 1",
    "x='a[$(SCRIPT)]'; read x <<< 1",
    "x='a[$(SCRIPT)]'; unset x",
    "x='a[$(SCRIPT)]'; echo '${!x}'",
    r"x='a[$(SCRIPT)]'; echo \${!x}",
    "x='a[$(SCRIPT)]'; echo $${!x}",
    "x='a[$(SCRIPT)]'; echo ${!x@}",
    "x='a[$(SCRIPT)]'; echo ${!x*}",
    "echo 'x=a[$(SCRIPT)]'; test -v 'a[x]'",
])
def test_inert_name_words_stay_allowed(tmp_path, shape):
    command = shape.replace("SCRIPT", str(tmp_path / "payload.sh"))
    ran, script = _run_bash(tmp_path, command)
    assert not ran
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
def test_unset_unknown_target_conservatively_scans(tmp_path):
    # No local target means no execution in this fixture, but inherited/sourced
    # state may supply it; do not infer that every expanded unset is inert.
    command = 'x=\'a[$(SCRIPT)]\'; unset "$x"'.replace("SCRIPT", str(tmp_path / "payload.sh"))
    ran, script = _run_bash(tmp_path, command)
    assert not ran
    script.write_text("hermes gateway restart\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    script.write_text("echo hello\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("prefix", ["mapfile -t", "readarray -t", "wait -p"])
def test_other_name_builtins_conservatively_recover_payload(prefix):
    # Bash versions differ in accepting subscripted names here; fail towards
    # reading the referenced file, without refusing clean contents.
    assert list(iter_arithmetic_word_payloads(prefix + r" a[\$\(S\)]")) == ["a[$(S)]"]


@pytest.mark.parametrize("command", [
    r"wait a[\$\(S\)]", r"printf '%s' a[\$\(S\)]", r"test -n a[\$\(S\)]",
])
def test_non_name_modes_do_not_recover_payload(command):
    assert list(iter_arithmetic_word_payloads(command)) == []
