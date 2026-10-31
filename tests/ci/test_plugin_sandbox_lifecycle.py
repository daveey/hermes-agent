"""Non-namespace controls for sandbox lifecycle ownership (real Linux processes)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tests.ci import test_plugin_validation_sandbox as helpers

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def work(tmp_path):
    yield from helpers.work.__wrapped__(tmp_path)


def test_normal_exit_waits_for_an_orphaned_writer(work):
    home = work / "overlay"
    home.mkdir()
    fake = work / "bwrap"
    fake.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os, sys
        argv = sys.argv
        copy = argv[argv.index("--bind") + 1]
        os.mkfifo(copy + "/release")
        r, w = os.pipe()
        if os.fork() == 0:
            os.setsid()
            os.close(r)
            fd = os.open(copy + "/release", os.O_RDWR)
            os.write(w, b"r")
            os.close(w)
            os.read(fd, 1)
            os.write(2, b"LATE_WRITE\\n")
            os._exit(0)
        os.close(w)
        assert os.read(r, 1) == b"r"
        os._exit(7)
    '''))
    fake.chmod(0o755)
    probe = textwrap.dedent('''\
        import errno, importlib.util, json, os, sys
        spec = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real_remove = mod.remove_tree
        alive = []
        def remove(path):
            try:
                fd = os.open(path / "copy0" / "overlay" / "release", os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                assert exc.errno == errno.ENXIO
                alive.append(False)
            else:
                alive.append(True)
                os.write(fd, b"g")
                os.close(fd)
            real_remove(path)
        mod.remove_tree = remove
        status = mod.main(["--bwrap", sys.argv[2], "--no-network", "--overlay", sys.argv[3],
                           "--overlay-mode", "copy", "--", "true"])
        print(json.dumps({"status": status, "alive": alive}))
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(helpers.SCRIPT), str(fake), str(home)],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "RUNNER_TEMP": str(work)}, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": 7, "alive": [False]}
    assert "LATE_WRITE" not in result.stderr
    assert not list(work.glob("plugin-sandbox-*"))


def test_missing_quiescence_ack_retains_scratch(work):
    probe = textwrap.dedent('''\
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real_popen = mod.subprocess.Popen
        def lost_supervisor(argv, **kwargs):
            # A real process exits without acknowledging descendant quiescence.
            assert "--supervise" in argv
            return real_popen(["/bin/true"], **kwargs)
        mod.subprocess.Popen = lost_supervisor
        sys.exit(mod.main(["--no-network", "--", "true"]))
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(helpers.SCRIPT)],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "RUNNER_TEMP": str(work)}, check=False)
    assert result.returncode == helpers.sandbox.EXIT_SETUP, result.stderr
    assert "quiescence not confirmed; retaining scratch" in result.stderr
    assert len(list(work.glob("plugin-sandbox-*"))) == 1


def test_supervisor_leaves_callers_other_children_and_subreaper_state_alone(work):
    probe = textwrap.dedent('''\
        import ctypes, importlib.util, os, subprocess, sys
        spec = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        libc = ctypes.CDLL(None, use_errno=True)
        def subreaper():
            flag = ctypes.c_int()
            assert libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0
            return flag.value
        before = subreaper()
        sibling = subprocess.Popen(["sleep", "60"])
        try:
            assert mod.main(["--bwrap", "/bin/false", "--no-network", "--", "true"]) == 1
            assert sibling.poll() is None
            assert subreaper() == before
        finally:
            sibling.kill()
            sibling.wait()
    ''')
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(helpers.SCRIPT)],
                            capture_output=True, text=True, timeout=60,
                            env={**os.environ, "RUNNER_TEMP": str(work)}, check=False)
    assert result.returncode == 0, result.stderr
    assert not list(work.glob("plugin-sandbox-*"))
