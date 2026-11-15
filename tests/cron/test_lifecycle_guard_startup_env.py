"""Startup files are executed input, not ordinary environment data."""
import shlex
import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


SHAPES = [
    "BASH_ENV=start.sh bash -c true",
    "env BASH_ENV=./start.sh bash -c true",
    "BASH_ENV='start.sh' command bash -c true",
    "env -i PATH=/usr/bin:/bin BASH_ENV=start.sh bash -c true",
    "env BASH_ENV=start.sh bash clean.sh",
    "ENV=start.sh bash --posix -ic true",
    "env ENV=./start.sh sh -ic true",
]


def run_bash(tmp_path, command):
    bash = shutil.which("bash")
    assert bash, "real Bash required for startup proof"
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", command], cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
        timeout=10, check=False,
    )


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("command", SHAPES)
def test_startup_script_marker_and_clean_twin(tmp_path, command):
    startup = tmp_path / "start.sh"
    marker = tmp_path / "ran"
    (tmp_path / "clean.sh").write_text("true\n")
    startup.write_text("touch ran\n")
    result = run_bash(tmp_path, command)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), result.stderr
    marker.unlink()
    startup.write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    startup.write_text("touch ran\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)
    assert not marker.exists(), "scanner must not execute startup files"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("prefix", ["BASH_ENV=start.sh", "env BASH_ENV=start.sh"])
def test_startup_declaration_precedes_child_payload(tmp_path, prefix):
    (tmp_path / "start.sh").write_text("declare -i x\n")
    payload = tmp_path / "payload.sh"
    payload.write_text("touch ran\nprintf 0\n")
    payload.chmod(0o755)
    command = prefix + " bash -c " + shlex.quote(r"x=a[\$\(./payload.sh\)]")
    result = run_bash(tmp_path, command)
    assert (tmp_path / "ran").exists(), result.stderr
    payload.write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    payload.write_text("true\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("command", [
    "BASH_ENV=start.sh true", "env BASH_ENV=start.sh printf ok",
    "bash -c true BASH_ENV=start.sh", "ENV=start.sh bash -c true",
    "ENV=start.sh sh -c true", "BASH_ENV=start.sh bash --posix -c true",
])
def test_environment_data_is_not_startup(tmp_path, command):
    (tmp_path / "start.sh").write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
def test_startup_relative_commands_keep_invocation_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/start.sh").write_text("sh ./payload.sh\n")
    payload = tmp_path / "payload.sh"
    payload.write_text("touch ran\n")
    command = "BASH_ENV=sub/start.sh bash -c true"
    result = run_bash(tmp_path, command)
    assert (tmp_path / "ran").exists(), result.stderr
    payload.write_text("hermes gateway restart\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)
    payload.write_text("true\n")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (False, None)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("value", ["$PWD/start.sh", "$(sh ./payload.sh)"])
def test_dynamic_startup_paths_fail_closed_without_execution(tmp_path, value):
    (tmp_path / "start.sh").write_text("touch ran\n")
    (tmp_path / "payload.sh").write_text("touch ran\n")
    command = "BASH_ENV=" + shlex.quote(value) + " bash -c true"
    result = run_bash(tmp_path, command)
    assert (tmp_path / "ran").exists(), result.stderr
    (tmp_path / "ran").unlink()
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "startup file expansion" in refusal
    assert not (tmp_path / "ran").exists()


def test_startup_remote_read_uses_same_walk(tmp_path):
    seen = []

    def remote(path):
        seen.append(path)
        return "hermes gateway restart\n"

    assert scan_gateway_lifecycle("BASH_ENV=remote.sh bash -c true", cwd=str(tmp_path),
                                  read_remote_script=remote) == (True, None)
    assert seen == [str(tmp_path / "remote.sh")]
