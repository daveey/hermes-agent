"""Generated stdin-inheritance controls for pipes and output substitutions."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell


_BODIES = ["cd .; {sink}", ":; {sink}", "( {sink} )", "\n{sink}\n",
           "true && {sink}", "false || {sink}", "{{ :; {sink}; }}",
           "if true; then {sink}; fi", "case x in x) {sink};; esac"]


@pytest.mark.parametrize("body", _BODIES)
@pytest.mark.parametrize("transport", ["> >( {body} )", "| ( {body} )"])
@pytest.mark.parametrize("sink", ["bash", "env bash", "cat", "tee copied"])
def test_generated_compound_stdin(tmp_path, body, transport, sink):
    script = tmp_path / "large.sh"
    marker = tmp_path / "marker"
    script.write_text(f"printf RAN > '{marker}'\n" + "# padding\n" * 150_000,
                      encoding="utf-8")
    # Keep the heredoc complete before the multiline consumer starts.
    producer = f"(python3 <<'EOF'\nprint('bash {script}')\nEOF\n)"
    command = producer + " " + transport.format(body=body.format(sink=sink)) + "\nwait\n"
    shell = sink in {"bash", "env bash"}
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(command, encoding="utf-8")
    _assert_execution(command, tmp_path, marker, shell)
    assert output_reaches_shell(command) is shell, command
    unsafe, reason = scan_gateway_lifecycle(f"bash -l {wrapper}", cwd=str(tmp_path))
    assert unsafe is shell, (command, reason)
    if shell:
        assert reason and "scan cap" in reason


def _assert_execution(command, tmp_path, marker, shell):
    bash = shutil.which("bash")
    if bash is None:
        return  # scanner policy remains unconditional without optional Bash
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", command],
                            cwd=tmp_path, capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert marker.exists() is shell, command
    if shell:
        assert marker.read_text(encoding="utf-8") == "RAN"


@pytest.mark.parametrize("command", [
    "p > >(cat); bash", "p > >(cat) && bash", "p > >(cat) || bash",
    "p | true && bash", "p | false || bash",
    "p > >(:; cat)", "p > >(true && tee file)",
    "echo '> >( :; bash )'", "echo \"> >( :; bash )\"",
    "p > >(cat) bash", "{ p > >(cat); bash; }",
    "p > >(cat > >(tee file)); bash",
])
def test_unfed_shell_controls(command):
    assert not output_reaches_shell(command)


def test_process_walk_is_explicitly_opted_in():
    from cron.lifecycle_guard_nested_shells import pipe_feeds_consumer

    command = "p > >(:; bash) | cat"
    assert not pipe_feeds_consumer(command, lambda words: "bash" in words)
    assert pipe_feeds_consumer(command, lambda words: "bash" in words, output_processes=True)


@pytest.mark.parametrize("body", _BODIES)
@pytest.mark.parametrize("opener", [">(", ">\\\n(", ">\\\n\\\n("])
def test_compound_sink_continuations(body, opener):
    assert output_reaches_shell("p > " + opener + " " + body.format(sink="bash") + " )")
