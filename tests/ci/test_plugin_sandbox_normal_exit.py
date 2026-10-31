"""Real bwrap regression: cleanup must precede neither namespace exit nor writers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tests.ci import test_plugin_validation_sandbox as helpers
from tests.ci.test_plugin_validation_sandbox import SCRIPT, needs_isolation

pytestmark = pytest.mark.platforms("linux")


@pytest.fixture
def work(tmp_path):
    yield from helpers.work.__wrapped__(tmp_path)


@needs_isolation
def test_normal_exit_quiesces_namespace_before_remove_tree(work):
    # Both sides handshake through FIFOs in the private overlay. The daemon is
    # released only at remove_tree entry, never by a scheduling sleep.
    home = work / "overlay"
    home.mkdir()
    payload = home / "payload.py"
    payload.write_text(textwrap.dedent('''\
        import os
        import sys
        from pathlib import Path
        home = Path(sys.argv[1])
        os.mkfifo(home / "release")
        ready_r, ready_w = os.pipe()
        if os.fork() == 0:
            os.close(ready_r)
            os.setsid()
            if os.fork() != 0:
                os._exit(0)
            gate = os.open(home / "release", os.O_RDWR)
            (home / "namespace").write_text(os.readlink("/proc/self/ns/pid"))
            os.write(ready_w, b"r")
            os.close(ready_w)
            os.read(gate, 1)
            (home / "after-remove-entry").write_text("writer ran")
            os.write(2, b"AFTER_REMOVE_WRITE\\n")
            os._exit(0)
        os.close(ready_w)
        assert os.read(ready_r, 1) == b"r"
        os.wait()
    '''))
    probe = work / "probe.py"
    probe.write_text(textwrap.dedent('''\
        import errno
        import importlib.util
        import json
        import os
        import sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("sandbox", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        original = mod.remove_tree
        observations = []
        def remove(path):
            copy = path / "copy0" / "overlay"
            observation = {"reader_alive": False, "namespace_alive": []}
            try:
                fd = os.open(copy / "release", os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                assert exc.errno == errno.ENXIO, exc
            else:
                observation["reader_alive"] = True
                try:
                    os.write(fd, b"g")
                except BrokenPipeError:
                    pass
                finally:
                    os.close(fd)
            namespace = (copy / "namespace").read_text()
            for entry in Path("/proc").glob("[0-9]*/ns/pid"):
                try:
                    if os.readlink(entry) == namespace:
                        observation["namespace_alive"].append(str(entry))
                except FileNotFoundError:
                    pass
                except PermissionError:
                    pass  # unrelated host processes, not this uid's sandbox
            observation["late_file"] = (copy / "after-remove-entry").exists()
            observations.append(observation)
            original(path)
        mod.remove_tree = remove
        statuses = []
        for _ in range(30):
            statuses.append(mod.main(["--no-network", "--overlay", sys.argv[2],
                "--overlay-mode", "copy", "--", "/usr/bin/python3", "-I", "-S",
                sys.argv[2] + "/payload.py", sys.argv[2]]))
        print(json.dumps({"observations": observations, "statuses": statuses}))
    '''))
    result = subprocess.run([sys.executable, "-I", str(probe), str(SCRIPT), str(home)],
                            env={**os.environ, "RUNNER_TEMP": str(work)},
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["statuses"] == [0] * 30, result.stderr
    assert len(evidence["observations"]) == 30
    assert not any(o["reader_alive"] or o["namespace_alive"] or o["late_file"]
                   for o in evidence["observations"]), evidence
    assert "AFTER_REMOVE_WRITE" not in result.stderr, result.stderr
    assert not list(work.glob("plugin-sandbox-*"))
