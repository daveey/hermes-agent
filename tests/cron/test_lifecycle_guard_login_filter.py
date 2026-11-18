"""Quoted interpreter input is not shell code merely because stdout is trimmed."""
import itertools
import shlex
import subprocess
import sys

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.fixture
def data_file(tmp_path):
    path = tmp_path / "large.db"
    path.write_bytes(b"x" * 2_200_000)
    return path


def program(path, producer="python3 -", suffix=" | tail -12", prelude=""):
    body = f"import sqlite3, os\nconn = sqlite3.connect(os.path.expanduser({str(path)!r}))\n"
    return f"bash -l -s <<'EOF'\n{prelude}{producer} <<'PY'{suffix}\n{body}PY\nEOF\n"


@pytest.mark.parametrize("producer,suffix", itertools.product(
    ["python3 -", ".venv/bin/python -", "timeout 60 python3 -", "env python3 -", "nice python3 -"],
    ["", " | tail -12", " 2>&1 | tail -12", " | head -30", " | tail -n 12 | head -n 3"],
))
def test_login_interpreter_filter_is_data(data_file, producer, suffix):
    assert scan_gateway_lifecycle(program(data_file, producer, suffix)) == (False, None)


@pytest.mark.parametrize("suffix", [" | bash", " | bash -l", " | tail -12 | bash", "; bash", " && bash"])
def test_shell_suffix_stays_visible(data_file, suffix):
    assert scan_gateway_lifecycle(program(data_file, suffix=suffix))[0]


@pytest.mark.parametrize("prelude", ["alias python3=cat\n", "alias tail=bash\n", "tail() { bash; }\n"])
def test_explicit_rebinding_stays_visible(data_file, prelude):
    assert scan_gateway_lifecycle(program(data_file, prelude=prelude))[0]


def test_login_shell_action_still_refused():
    assert scan_gateway_lifecycle("bash -l -s <<'EOF'\nhermes gateway restart\nEOF\n")[0]


def test_multihop_diagnostics_shape(data_file):
    command = program(data_file, "timeout 60 .venv/bin/python -", " 2>&1 | tail -12")
    command = command.replace("bash -l -s <<'EOF'",
        "timeout 140 ssh ubuntu@hub 'ssh worker \"bash -l -s\"' <<'EOF' 2>&1 | head -30")
    command = command.replace("timeout 60",
        "timeout 90 hermes kanban diagnostics --task t_x 2>&1 | head -14\n"
        "cd ~/.hermes/hermes-agent 2>/dev/null && timeout 60")
    assert scan_gateway_lifecycle(command) == (False, None)


@pytest.mark.parametrize("line", [
    "python3 - <<'PY' | tail -12", "python3 - <<'PY' 2>&1 | head -n 3",
])
def test_filter_certificate_not_used_by_text_scanners(line):
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    source = line + "\nbody\nPY\n"
    assert strip_inert_heredoc_bodies(source) == source
    assert "body" not in strip_inert_heredoc_bodies(source, nested=True)


@pytest.mark.parametrize("line", [
    "python3 - <<'A' <<'B' | tail -12", "python3 - <<",
    "python3 - <<'A' | unknown -12", "python3 - <<'A' | tail -12 extra",
])
def test_uncertain_filter_certificate(line):
    from tools.shell_heredoc import _numeric_filter_suffix

    assert not _numeric_filter_suffix(line)


@pytest.mark.parametrize("suffix", [
    " | unknown", " | tail -12; bash", " | tail -12 && bash", " | tail -12 || bash",
    " | tail -12 & bash", " | tail -12 extra", " | tail -n $N", " | tail -12 >out",
    " | tail -12 <<'OTHER'", " | head -30 | unknown", " |& tail -12",
])
def test_unknown_suffix_not_certified(data_file, suffix):
    assert scan_gateway_lifecycle(program(data_file, suffix=suffix))[0]


@pytest.mark.parametrize("prelude", ["alias head=bash\n", "head() { bash; }\n"])
def test_head_rebinding_not_certified(data_file, prelude):
    assert scan_gateway_lifecycle(program(data_file, suffix=" | head -30", prelude=prelude))[0]


@pytest.mark.parametrize("shell,delimiter,suffix", itertools.product(
    ["bash -s", "bash -l -s", "bash --login -s"],
    ["'PY'", '"PY"', "\\PY", "PY"],
    [" | head -30", " 2>&1 | tail -12", " | tail -n 12 | head -n 3"],
))
def test_generated_data_shapes(data_file, shell, delimiter, suffix):
    command = program(data_file, suffix=suffix).replace("bash -l -s", shell)
    command = command.replace("<<'PY'", "<<" + delimiter)
    assert scan_gateway_lifecycle(command) == (False, None)
    # Run the same generated source with startup files disabled and the test Python.
    executable = command.replace(shell, "bash --noprofile --norc " + shell[5:], 1)
    executable = executable.replace("python3 -", f"{shlex.quote(sys.executable)} -", 1)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-s"], input=executable,
        capture_output=True, encoding="utf-8", timeout=10, check=True,
    )
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("filter_name", ["head", "tail"])
def test_real_bash_filter_and_alias_controls(tmp_path, filter_name):
    # Harmless marker: the scanned file is deliberately too large, not destructive.
    script = tmp_path / "marker.sh"
    script.write_text("printf RAN\n#" + "x" * 2_200_000, encoding="utf-8")
    invocation = f"bash {str(script)!r}"
    bodies = [
        (f"{shlex.quote(sys.executable)} -", "", f"print({invocation!r})", False),
        ("cat", f"{filter_name}() {{ bash; }}\n", invocation, True),
        ("python3 -", "shopt -s expand_aliases\nalias python3=cat\n", invocation, True),
    ]
    for producer, prelude, body, runs in bodies:
        sink = "bash" if "alias python3" in prelude else f"{filter_name} -n 12"
        inner = f"{prelude}{producer} <<'PY' | {sink}\n{body}\nPY\n"
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-l", "-s"], input=inner,
            capture_output=True, encoding="utf-8", timeout=10, check=True,
        )
        assert (result.stdout == "RAN") is runs
        command = "bash -l -s <<'EOF'\n" + inner + "EOF\n"
        assert scan_gateway_lifecycle(command)[0] is runs

