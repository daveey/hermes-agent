"""Output process substitutions execute producer text, just like shell pipes."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell


_PRODUCERS = {
    "heredoc": "python3 <<'EOF' {redirect}\nprint('bash {script}')\nEOF\n",
    "printf": "printf '%s\\n' \"$(python3 <<'EOF'\nprint('bash {script}')\nEOF\n)\" {redirect}\n",
    "echo": "echo \"$(python3 <<'EOF'\nprint('bash {script}')\nEOF\n)\" {redirect}\n",
    "echo-paren": "echo \"($(python3 <<'EOF'\nprint('bash {script}')\nEOF\n))\" {redirect}\n",
}
_REDIRECTS = {
    "spaced": ("", "> >({consumer})", "wait\n"),
    "explicit-stdout": ("", "1> >({consumer})", "wait\n"),
    "descriptor": ("exec 3> >({consumer})\n", ">&3", "exec 3>&-\nwait\n"),
}


def _command(producer, redirect, consumer, script):
    prefix, target, suffix = _REDIRECTS[redirect]
    return (prefix.format(consumer=consumer)
            + _PRODUCERS[producer].format(
                redirect=target.format(consumer=consumer), script=script)
            + suffix)


@pytest.mark.parametrize("producer", _PRODUCERS)
@pytest.mark.parametrize("redirect", _REDIRECTS)
@pytest.mark.parametrize("consumer", ["bash", "sh", "env bash"])
def test_output_process_shell_refuses_oversized_script(tmp_path, producer, redirect, consumer):
    script = tmp_path / "large.sh"
    script.write_text("# padding\n" * 150_000, encoding="utf-8")
    command = _command(producer, redirect, consumer, script)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(command, encoding="utf-8")
    assert output_reaches_shell(command)
    # Independently require the direct sink: echo-paren used to match only by
    # accident through the command-position `($(` anchor inside its quoted word.
    assert output_reaches_shell(command.replace("$(python3", "(python3"))
    unsafe, refusal = scan_gateway_lifecycle(f"bash -l {wrapper}", cwd=str(tmp_path))
    assert unsafe and refusal and "scan cap" in refusal, (command, refusal)


@pytest.mark.parametrize("producer", _PRODUCERS)
@pytest.mark.parametrize("redirect", _REDIRECTS)
@pytest.mark.parametrize("consumer", ["bash", "sh", "env bash", "cat", "tee copied"])
def test_generated_real_bash_marker(tmp_path, producer, redirect, consumer):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("requires bash")
    marker = tmp_path / "marker"
    script = tmp_path / "payload.sh"
    script.write_text(f"printf RAN > '{marker}'\n", encoding="utf-8")
    command = _command(producer, redirect, consumer, script)
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    if consumer in {"cat", "tee copied"}:
        assert not marker.exists()
        assert "bash " in result.stdout
    else:
        assert marker.read_text(encoding="utf-8") == "RAN"


@pytest.mark.parametrize("consumer", ["cat", "tee copied"])
@pytest.mark.parametrize("producer", ["heredoc", "printf", "echo"])
@pytest.mark.parametrize("redirect", _REDIRECTS)
def test_output_process_data_stays_data(tmp_path, consumer, producer, redirect):
    script = tmp_path / "large.sh"
    script.write_text("# padding\n" * 150_000, encoding="utf-8")
    command = _command(producer, redirect, consumer, script)
    assert not output_reaches_shell(command)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(command, encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash -l {wrapper}", cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("consumer", [
    "bash", "dash", "zsh", "ksh", "mksh", "tcsh", "csh", "fish", "ash", "sh",
    "busybox sh", "ssh host", "su", "sudo -s", "sudo -is", "eval",
    "env bash", "env A=1 /bin/sh", "sudo bash", "command bash", "timeout 5 bash",
])
def test_process_shell_sink_vocabulary(consumer):
    assert output_reaches_shell(f"producer > >({consumer})")


@pytest.mark.parametrize("text", [
    "producer > >(cat)", "producer > >(tee file)",
    "producer > >(bashrc)", "producer > >(bash.sh)",
    "producer > >(cat); bash clean.sh", "producer > >(cat) bash",
])
def test_process_sink_boundary_controls(text):
    assert not output_reaches_shell(text)
