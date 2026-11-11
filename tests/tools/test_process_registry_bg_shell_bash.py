"""Background commands run with the foreground's bash semantics, whatever $SHELL is."""

import os
import shutil
import shlex
import subprocess
import time

import pytest

from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry

PROBE = ('echo "${BASH_VERSION:+bash}"; M="a b"; printf \'[%s]\' $M; '
         'false | true; echo " ${PIPESTATUS[0]}"')
EXPECT = "bash\n[a][b] 1"


def _run(command, monkeypatch, shell, home=None, wait=30):
    monkeypatch.setenv("SHELL", shell)
    if home:
        monkeypatch.setenv("HOME", str(home))
    reg = ProcessRegistry()
    try:
        s = reg.spawn_local(command, task_id="t-bg-bash")
        end = time.monotonic() + wait
        while not s.exited and time.monotonic() < end:
            time.sleep(0.1)
        assert s.exited, "background job did not exit"
        return s.output_buffer.strip()
    finally:
        reg.kill_all()


@pytest.mark.skipif(not os.path.exists("/bin/sh") or not shutil.which("bash"), reason="needs sh+bash")
def test_background_uses_bash_semantics_with_sh_login_shell(monkeypatch):
    assert _run(PROBE, monkeypatch, "/bin/sh").endswith(EXPECT)


@pytest.mark.skipif(not os.path.exists("/bin/zsh"), reason="no zsh on this host (run on daveey-mini / David-MBP)")
def test_background_uses_bash_semantics_with_zsh_login_shell(monkeypatch):
    assert _run(PROBE, monkeypatch, "/bin/zsh").endswith(EXPECT)


@pytest.mark.parametrize("shell", ["bash", "/bin/sh"])
@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_login_environment_still_loaded_and_bg_var_hidden(tmp_path, monkeypatch, shell):
    # The host's /etc/profile may reset HOME (Blacksmith does). Keep the real
    # login invocation, but load our explicit rc instead of trusting host policy.
    rc = tmp_path / "login.rc"
    rc.write_text("export HERMES_LOGIN_MARKER=from-login-rc\n")
    monkeypatch.delenv("HERMES_LOGIN_MARKER", raising=False)
    login = tmp_path / shell.rsplit("/", 1)[-1]
    executable = shlex.quote(shutil.which(shell))
    options = "--noprofile " if shell == "bash" else ""
    login.write_text(
        '#!/bin/sh\n[ "$1" = "-lic" ] || exit 97\n'
        f'exec {executable} {options}-lic {shlex.quote(". " + shlex.quote(str(rc)) + "; ")}"$2"\n'
    )
    login.chmod(0o700)
    out = _run('echo "marker=$HERMES_LOGIN_MARKER bg=${HERMES_BG_COMMAND:-unset}"',
               monkeypatch, str(login), home=tmp_path)
    assert out.endswith("marker=from-login-rc bg=unset")


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_poll_and_kill_still_work(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    reg = ProcessRegistry()
    try:
        s = reg.spawn_local("sleep 60", task_id="t-bg-kill")
        wait_until(lambda: reg.poll(s.id)["status"] == "running", what="job running")
        assert not s.exited
        assert s.pid and reg.poll(s.id)["status"] == "running"
        reg.kill_process(s.id)
        end = time.monotonic() + 15
        while not s.exited and time.monotonic() < end:
            time.sleep(0.1)
        assert s.exited
    finally:
        reg.kill_all()


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_foreground_unchanged():
    from tools.environments.local import LocalEnvironment
    env = LocalEnvironment(cwd="/tmp", timeout=30)
    try:
        r = env.execute(PROBE)
        assert r["output"].strip() == EXPECT
    finally:
        env.cleanup()
