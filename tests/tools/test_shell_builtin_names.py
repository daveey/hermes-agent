"""Real Bash proof for quote-removed builtin name operands."""
import subprocess

import pytest

from tools.shell_arith_sink import substitution_in_arithmetic

SINKS = [
    "a=(1); unset 'a[$(P)]'",
    "a=(1); unset $'a[$(P)]'",
    "a=(1); unset a'[$(P)]'",
    r"a=(1); unset 'a['\$'(P)]'",
    r"a=(1); unset a[\$\(P\)]",
    "printf -v 'a[$(P)]' x",
    r"printf -v a[\$\(P\)] x",
    "a=(1); [[ -v 'a[$(P)]' ]]",
    "a=(1); test -v 'a[$(P)]'",
    "declare 'a[$(P)]=1'",
    r"declare a\[\$\(P\)\]=1",
    "typeset 'a[$(P)]=1'",
    "f() { local 'a[$(P)]=1'; }; f",
    "read 'a[$(P)]' <<< x",
    "read -d -p 'a[$(P)]' <<< x",
    "a=(1); A=1 \\\n unset 'a[$(P)]'",
    "a=(1); $\"unset\" 'a[$(P)]'",
    "a=(1); unset 2>&1 'a[$(P)]'",
    "printf -v'a[$(P)]' x",
    "a=(1); unset -- 'a[$(P)]'",
    r"a=(1); unset $'a[\x24(P)]'",
]
DATA = [
    "export 'a[$(P)]=1'",
    "readonly 'a[$(P)]=1'",
    "mapfile -t 'a[$(P)]' <<< x",
    r"[[ -v a[\$\(P\)] ]]",
    "unset -f 'a[$(P)]'",
    "declare -p 'a[$(P)]'",
    "declare -f 'a[$(P)]'",
    "read -a 'a[$(P)]' <<< x",
    "printf '%s ' -v 'a[$(P)]'",
    "declare 'a[0]=foo[$(P)]'",
]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("shape, expected", [(s, True) for s in SINKS] + [(s, False) for s in DATA])
def test_literal_builtin_name_marker(tmp_path, shape, expected):
    payload = tmp_path / "payload"
    payload.write_text("#!/bin/sh\nprintf ran > marker\nprintf 0\n", encoding="utf-8")
    payload.chmod(0o700)
    command = shape.replace("P", "./payload")
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", command],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
    )
    assert (tmp_path / "marker").exists() is expected, result.stderr
    assert substitution_in_arithmetic(command, date_exempt=True) is expected


@pytest.mark.parametrize("command", [
    "echo 'a[$(value)]'",
    "printf -v x %s 'a[$(value)]'",
    "unset x > 'a[$(value)]'",
    "declare 'a=literal[$(value)]'",
    "read -p 'a[$(value)]' x",
    "command -v unset 'a[$(value)]'",
])
def test_literal_name_data(command):
    assert not substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.parametrize("shape", [s for s in SINKS if "\\" not in s])
def test_literal_name_numeric_date(shape):
    command = shape.replace("P", "date +%s")
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert substitution_in_arithmetic(command, date_exempt=False)


@pytest.mark.parametrize("shape", SINKS)
def test_literal_name_reaches_lifecycle_refusal(tmp_path, shape):
    from cron.lifecycle_guard import scan_gateway_lifecycle
    from tests.cron.test_lifecycle_guard_arith_sink import _big_log

    payload = _big_log(tmp_path)
    command = shape.replace("P", "bash " + str(payload))
    # Escaped ordinary words must preserve the producer's space through quote removal.
    if r"\(" in shape:
        command = command.replace("bash ", r"bash\ ")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "larger than the scan cap" in refusal


CASE_PREFIX_SINKS = [
    "a=(1); unset \"$(case x in x) printf x;; esac)\" 'a[$(P)]'",
    "a=(1); unset x$(case x in x) printf x;; esac) 'a[$(P)]'",
    "a=(1); unset `case x in x) printf x;; esac` 'a[$(P)]'",
    "declare \"x=$(case x in x) printf x;; esac)\" 'a[$(P)]=1'",
]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("shape", CASE_PREFIX_SINKS)
def test_uncertain_prefix_cannot_hide_literal_name(tmp_path, shape):
    test_literal_builtin_name_marker(tmp_path, shape, True)


@pytest.mark.parametrize("opener, closer", [("$(", ")"), ("`", "`")])
def test_uncertain_prefix_has_bounded_scan_work(monkeypatch, opener, closer):
    from tools import shell_prefix_expansions as expansions

    original = expansions._opener
    calls = 0
    command = 'unset "' + opener + "x" * 1000 + " case x in x) :;; esac" + closer + '"' + " 'x'" * 1000

    def counted(*args):
        nonlocal calls
        calls += 1
        assert calls < len(command) * 4, "uncertain word repeatedly rescanned"
        return original(*args)

    from tools import shell_builtin_names

    monkeypatch.setattr(expansions, "_opener", counted)
    monkeypatch.setattr(shell_builtin_names, "_opener", counted)
    substitution_in_arithmetic(command, date_exempt=True)


def test_nested_expansions_share_linear_boundary_scan(monkeypatch):
    from tools import shell_prefix_expansions as expansions

    original = expansions._opener
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return original(*args)

    monkeypatch.setattr(expansions, "_opener", counted)
    command = "echo $(" * 200 + "echo 'x'" + ")" * 200
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert calls < len(command) * 3
