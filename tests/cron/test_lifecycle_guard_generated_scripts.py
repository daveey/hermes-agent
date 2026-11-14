"""A script the same command writes and then runs is scanned as written (t_01a1d88e9c759e09).

The referenced-script walk reads F from disk at scan time, so ``cat > F <<'R' … R; bash F`` with no
F yet (or a stale F) used to allow a body real Bash then runs. Each differential below runs the
command in real Bash: whenever the marker ran, the guard must have refused. The marker payload is
``bash <BIG>``, an oversized script the guard fails closed on when it is executed; BIG touches the
marker first. (A literal lifecycle command cannot run here: tests/conftest.py blocks the spawn.)
"""

import base64
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle

_WRITE_THEN_RUN = [
    "cat > F <<'R'\nBODY\nR\nbash F\n",
    "cat > F <<'R'\nBODY\nR\nsh F\n",
    "cat <<'R' > F\nBODY\nR\n. ./F\n",
    "cat >F <<-'R'\nBODY\nR\nsource ./F\n",
    "cat >> F <<'R'\nBODY\nR\nbash F\n",
    "cat > F <<'R'\nBODY\nR\nchmod +x F; ./F\n",
    "cat > F <<'R' && bash F\nBODY\nR\n",
    "tee F >/dev/null <<'R'\nBODY\nR\nbash F\n",
    "cat > ./sub/F <<'R'\nBODY\nR\nbash sub/F\n",
    "{ cat > F; } <<'R'\nBODY\nR\nbash F\n",
]


@pytest.fixture
def big(tmp_path):
    path = tmp_path / "BIG"
    path.write_text("touch executed\n" + "# padding\n" * 150000)
    (tmp_path / "sub").mkdir()
    return f"bash {path}"


def _run_bash(tmp_path, command):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True, timeout=20,
                   check=False)


def _differential(tmp_path, command):
    """The guard verdict, scanned BEFORE real Bash runs *command* (F does not exist yet unless the
    test seeded it), after asserting that Bash ran the marker."""
    unsafe, _reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    _run_bash(tmp_path, command)
    assert (tmp_path / "executed").exists(), "fixture must execute its marker"
    return unsafe


@pytest.mark.parametrize("transport", _WRITE_THEN_RUN)
def test_generated_script_is_scanned_as_written(tmp_path, big, transport):
    assert _differential(tmp_path, transport.replace("BODY", big)), (
        "real Bash executed a payload the guard allowed")


@pytest.mark.parametrize("transport", _WRITE_THEN_RUN[:3])
def test_stale_file_on_disk_does_not_hide_the_written_one(tmp_path, big, transport):
    (tmp_path / "F").write_text("echo old\n")
    assert _differential(tmp_path, transport.replace("BODY", big))


@pytest.mark.parametrize("template", [
    "printf '%s\\n' 'BODY' > F; bash F\n",
    "echo 'BODY' > F && bash F\n",
    "cat <<< 'BODY' > F; bash F\n",
])
def test_printed_and_herestring_writes_are_scanned(tmp_path, big, template):
    assert _differential(tmp_path, template.replace("BODY", big))


@pytest.mark.parametrize("template", [
    # Writers whose output the guard cannot predict, then executed: fail closed.
    "rev > F <<'R'\nREVERSED\nR\nbash F\n",
    "cat > F <<R\n$(printf '%s' 'BODY')\nR\nbash F\n",
    "base64 -d > F <<< B64; bash F\n",
    # A redirection with no command word, or a persistent `exec` redirection (review of #3001).
    "(echo 'BODY') > F; bash F\n",
    "{ echo 'BODY'; } > F; bash F\n",
    "exec > F; echo 'BODY'; exec > /dev/null; bash F\n",
    "exec 3>F; echo 'BODY' >&3; exec 3>&-; bash F\n",
    "if true; then echo 'BODY'; fi > F; bash F\n",
    # A generated file copied or moved before it runs.
    "cat > G <<'R'\nBODY\nR\ncp G F; bash F\n",
    "cat > G <<'R'\nBODY\nR\nmv G F && bash F\n",
    "cat > G <<'R'\nBODY\nR\ncp G sub/; bash sub/G\n",
    "cat > G <<'R'\nBODY\nR\ncp -t sub G; bash sub/G\n",
    "cat > G <<'R'\nBODY\nR\nln -s G F; bash F\n",
])
def test_unpredictable_writer_then_execute_fails_closed(tmp_path, big, template):
    command = (template.replace("REVERSED", big[::-1]).replace("BODY", big)
               .replace("B64", base64.b64encode(f"{big}\n".encode()).decode()))
    assert _differential(tmp_path, command)


@pytest.mark.parametrize("transport", _WRITE_THEN_RUN)
def test_generated_lifecycle_command_is_refused(tmp_path, transport):
    (tmp_path / "sub").mkdir()
    unsafe, _reason = scan_gateway_lifecycle(
        transport.replace("BODY", "hermes gateway restart"), cwd=str(tmp_path))
    assert unsafe


@pytest.mark.parametrize("command", [
    # Written, never executed: data.
    "cat > notes.md <<'EOF'\nTo apply: hermes gateway restart\nEOF\n",
    "curl -s https://example.invalid/x > out.json; cat out.json\n",
    "rev > F <<'R'\nanything\nR\n",
    # Written and executed, but benign.
    "cat > F <<'R'\necho hello\nR\nbash F\n",
    "printf '%s\\n' 'echo hi' > F; bash F\n",
    # A Python program is regex-scanned only and never fails closed, generated or not.
    "python3 -c 'print(1)' > gen.py; python3 gen.py\n",
    # `bash <> F` reads F as its program; it is not a write.
    "printf 'echo hi\\n' > F; bash <> F\n",
    # A redirection or copy whose target is never executed.
    "(echo hi) > out.log; exec 2> err.log; cat out.log\n",
    "cp a.txt b.txt; bash run.sh\n",
    # Known content after a prefix redirection is still read as written.
    "> F echo 'echo hi'; bash F\n",
])
def test_inert_or_benign_generated_files_are_allowed(tmp_path, command):
    unsafe, reason = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert not unsafe, reason
