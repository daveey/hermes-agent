"""Real-process activation: CLI arm, cross-process disarm/start race, FIFO refusal."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]

RACE_CHILD = r'''
import hermes_bootstrap
import json, os, sys
from hermes_cli import kanban_fairness_activation as activation
from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home

ready_w, go_r = int(sys.argv[1]), int(sys.argv[2])
original = runtime.activate
sessions = []
def paused(*a, **k):
    os.write(ready_w, b"R")          # authority was read; activation not yet done
    assert os.read(go_r, 1) == b"G"  # parent disarms, then releases us
    s = original(*a, **k)
    sessions.append(s)
    return s
runtime.activate = paused
try:
    result = activation.start(load_config(), home=get_hermes_home())
    print(json.dumps({"returned_none": result is None,
                      "stopping": [s.stopping.is_set() for s in sessions],
                      "pid": os.getpid()}))
finally:
    for s in sessions:
        s.close()
'''

FIFO_CHILD = r'''
import hermes_bootstrap
import json
from hermes_cli import kanban_fairness_activation as activation
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home
cfg, home = load_config(), get_hermes_home()
out = {}
try:
    activation.start(cfg, home=home)
    out["start"] = "returned"
except ValueError as exc:
    out["start"] = "ValueError"
out["boot"] = activation.boot(cfg, home=home)
print(json.dumps(out if out["boot"] is None else {"bad": 1}))
'''


def _env(home, user):
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(ROOT))
    return env


@pytest.fixture
def world(tmp_path):
    home, user = tmp_path / "home", tmp_path / "user"
    home.mkdir()
    user.mkdir()
    cfg = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "fixture",
                                           "directory": str(home / "capture")}}}
    (home / "config.yaml").write_text(json.dumps(cfg))
    return home, _env(home, user), home / "capture"


def _cli(env, *args, check=True):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "kanban_fairness.py"), "runtime", *args],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
    if check:
        assert r.returncode == 0, r.stderr
    return r


ARM = ("arm", "--board", "default", "--source-gateway", "gw", "--board-uid", "board")


def test_real_cli_arm_writes_descriptor_and_refuses_rearm(world):
    home, env, capture = world
    r = _cli(env, *ARM)
    assert json.loads(r.stdout)["runtime"] == "arm"
    value = json.loads((capture / "runtime-arm.json").read_text())
    assert (value["home"], value["board"], value["source_gateway"], value["board_uid"]) == \
        (str(home), "default", "gw", "board")
    again = _cli(env, *ARM, check=False)
    assert again.returncode != 0
    assert json.loads((capture / "runtime-arm.json").read_text()) == value


def test_cross_process_disarm_between_authority_read_and_activate(world):
    _home, env, capture = world
    _cli(env, *ARM)
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    proc = subprocess.Popen([sys.executable, "-c", RACE_CHILD, str(ready_w), str(go_r)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(ready_w, go_r))
    os.close(ready_w)
    os.close(go_r)
    try:
        import select
        assert select.select([ready_r], [], [], 30)[0], "child never reached activation barrier"
        assert os.read(ready_r, 1) == b"R"
        assert proc.poll() is None  # genuinely paused in the middle of start
        _cli(env, "disarm")  # real separate-process revocation
        assert (capture / "runtime-disarmed.json").is_file()
        assert not (capture / "runtime-arm.json").exists()
        os.write(go_w, b"G")
        out, err = proc.communicate(timeout=30)
    finally:
        for fd in (ready_r, go_w):
            os.close(fd)
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
    assert proc.returncode == 0, err.decode()
    answer = json.loads(out)
    assert answer["returned_none"] is True
    assert answer["stopping"] == [True]


def test_fifo_descriptor_refused_without_blocking(world):
    _home, env, capture = world
    _cli(env, *ARM)
    arm = capture / "runtime-arm.json"
    arm.unlink()
    os.mkfifo(arm)  # no writer will ever exist: a blocking open/read would hang
    proc = subprocess.run([sys.executable, "-c", FIFO_CHILD], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"start": "ValueError", "boot": None}
    disarm = _cli(env, "disarm", "--directory", str(capture), check=False)
    assert disarm.returncode != 0
    assert arm.exists() and not (capture / "runtime-disarmed.json").exists()
