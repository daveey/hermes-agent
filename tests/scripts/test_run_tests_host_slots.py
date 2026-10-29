"""Host-wide test slots: concurrent runs share os.cpu_count() pytest processes (scripts/run_tests_host_slots.py)."""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from scripts import run_tests_host_slots as slots

pytestmark = pytest.mark.skipif(os.name != "posix", reason="host slots are POSIX flock locks")


@pytest.fixture
def slot_env(tmp_path, monkeypatch):
    # This test itself runs inside a runner slot, so the inherited HELD flag must be cleared.
    monkeypatch.delenv(slots.HELD_ENV, raising=False)
    monkeypatch.setenv("HERMES_TEST_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "2")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(tmp_path / "no-config.yaml"))  # never the real home
    return tmp_path / "slots"


def _hold(release: threading.Event, entered: threading.Event):
    with slots.host_test_slot():
        entered.set()
        release.wait(10)


def test_third_holder_waits_until_a_slot_frees(slot_env):
    release = threading.Event()
    holders = [threading.Event(), threading.Event()]
    threads = [threading.Thread(target=_hold, args=(release, e)) for e in holders]
    for t in threads:
        t.start()
    assert all(e.wait(5) for e in holders)
    waits: list[int] = []
    got = threading.Event()

    def third():
        with slots.host_test_slot(on_wait=lambda: waits.append(1)):
            got.set()

    t3 = threading.Thread(target=third)
    t3.start()
    assert not got.wait(0.5), "both slots are held: the third run must wait"
    release.set()
    assert got.wait(5)
    for t in (*threads, t3):
        t.join(5)
    assert waits == [1], "on_wait fires once per wait, not per poll"


def test_killed_holder_frees_its_slot(slot_env, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from scripts import run_tests_host_slots as s\n"
            "with s.host_test_slot():\n    print('held', flush=True); time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code, str(REPO_ROOT)], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ))
    try:
        assert child.stdout.readline().strip() == "held"
        got = threading.Event()
        t = threading.Thread(target=lambda: (slots.host_test_slot().__enter__(), got.set()), daemon=True)
        t.start()
        assert not got.wait(0.4), "the only slot is held by the child"
        child.send_signal(signal.SIGKILL)
        child.wait(5)
        assert got.wait(5), "a crashed run's slot is released with its file descriptor"
    finally:
        if child.poll() is None:
            child.kill()


def test_nested_run_skips_the_cap(slot_env, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    with slots.host_test_slot():
        monkeypatch.setenv(slots.HELD_ENV, "1")
        with slots.host_test_slot():  # would deadlock without the HELD skip
            pass


def test_zero_slots_disables_the_cap(slot_env, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "0")
    assert not slots.host_slots_enabled()
    with slots.host_test_slot():
        pass
    assert not slot_env.exists()


def _load_runner():
    spec = importlib.util.spec_from_file_location("run_tests_parallel", REPO_ROOT / "scripts" / "run_tests_parallel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_attempt_waits_for_a_slot_and_marks_the_child(slot_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    mod = _load_runner()
    test_file = tmp_path / "test_held.py"
    test_file.write_text("import os\n\ndef test_held():\n    assert os.environ.get('HERMES_TEST_SLOT_HELD') == '1'\n")
    release, entered = threading.Event(), threading.Event()
    blocker = threading.Thread(target=_hold, args=(release, entered))
    blocker.start()
    assert entered.wait(5)
    threading.Timer(1.0, release.set).start()
    started = time.monotonic()
    _, code, output, _, _ = mod._run_one_file_once(test_file, ["-p", "no:cacheprovider"], tmp_path, 120)
    blocker.join(5)
    assert time.monotonic() - started >= 0.9, "the attempt waited for the only slot"
    assert code == 0, output
    assert mod._SLOT_WAIT_NOTED.is_set(), "the run says once that it is waiting for slots"


# --- per-host config fallback + direct pytest runs [daveey] -----------------------------------

def test_host_config_sets_the_cap_when_the_env_does_not(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  default: x\ntests:\n  # MacBook: agents' tests share 4 cores\n  host_slots: 4\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    monkeypatch.delenv("HERMES_TEST_HOST_SLOTS", raising=False)
    assert slots.host_slot_count() == 4
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "2")  # env still wins
    assert slots.host_slot_count() == 2


def test_host_config_parses_without_pyyaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tests:\n  other: 1\n\n  host_slots: 3\nkanban:\n  host_slots: 9\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    monkeypatch.delenv("HERMES_TEST_HOST_SLOTS", raising=False)
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    assert slots.host_slot_count() == 3


def test_missing_or_unset_host_config_keeps_cpu_count(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TEST_HOST_SLOTS", raising=False)
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(tmp_path / "absent.yaml"))
    assert slots.host_slot_count() == (os.cpu_count() or 4)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tests:\n  other: 1\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    assert slots.host_slot_count() == (os.cpu_count() or 4)


def test_direct_pytest_run_holds_a_slot_for_its_session(slot_env, monkeypatch, tmp_path):
    """A bare `python -m pytest` of this tree (no run_tests.sh) waits for, and holds, a host slot."""
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    probe = tmp_path / "probe_slot.py"
    probe.write_text(
        "import os, sys, importlib.util\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'tests')!r})\n"
        "import conftest\n"
        "class C: pass\n"
        "c = C()\n"
        "conftest._take_host_test_slot(c)\n"
        "print('held', c._hermes_host_slot_fd is not None, os.environ.get('HERMES_TEST_SLOT_HELD'), flush=True)\n"
        "sys.stdin.readline()\n"
        "conftest._release_host_test_slot(c)\n"
        "print('released', flush=True)\n")
    env = {k: v for k, v in os.environ.items() if k != slots.HELD_ENV}
    first = subprocess.Popen([sys.executable, str(probe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env, cwd=str(REPO_ROOT))
    try:
        assert first.stdout.readline().split() == ["held", "True", "1"]
        second = subprocess.Popen([sys.executable, str(probe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=env, cwd=str(REPO_ROOT))
        time.sleep(1.0)
        assert second.poll() is None  # waiting: the only slot is taken
        first.stdin.write("\n"); first.stdin.flush()
        assert first.stdout.readline().strip() == "released"
        assert second.stdout.readline().split() == ["held", "True", "1"]
        second.communicate("\n", timeout=30)
    finally:
        for p in (first,):
            if p.poll() is None:
                p.kill()


def test_xdist_worker_budget_is_cores_over_slots(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TEST_XDIST_MAX", raising=False)
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "4")
    monkeypatch.setattr(slots.os, "cpu_count", lambda: 16)
    assert slots.xdist_worker_budget() == 4
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "32")
    assert slots.xdist_worker_budget() == 1, "never below one worker"
    monkeypatch.setenv("HERMES_TEST_XDIST_MAX", "3")
    assert slots.xdist_worker_budget() == 3, "the env override wins"


def test_host_config_sets_the_xdist_worker_budget(tmp_path, monkeypatch):
    """A laptop caps each holder's workers below cores // slots: tests/hermes_cli spawns ~15 processes/s per worker."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tests:\n  host_slots: 4\n  xdist_workers: 2\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    monkeypatch.delenv("HERMES_TEST_HOST_SLOTS", raising=False)
    monkeypatch.delenv("HERMES_TEST_XDIST_MAX", raising=False)
    monkeypatch.setattr(slots.os, "cpu_count", lambda: 16)
    assert slots.xdist_worker_budget() == 2, "config caps below cores // slots (4)"
    monkeypatch.setenv("HERMES_TEST_XDIST_MAX", "3")
    assert slots.xdist_worker_budget() == 3, "env still wins"
    monkeypatch.delenv("HERMES_TEST_XDIST_MAX")
    cfg.write_text("tests:\n  host_slots: 4\n")
    assert slots.xdist_worker_budget() == 4, "unset keeps cores // slots"
    cfg.write_text("tests:\n  host_slots: 4\n  xdist_workers: 9\n")
    assert slots.xdist_worker_budget() == 4, "config never raises above cores // slots"


def test_host_config_xdist_workers_parses_without_pyyaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tests:\n  host_slots: 4\n  xdist_workers: 2\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    assert slots._config_int("xdist_workers") == 2


def test_direct_pytest_n8_is_capped_to_the_slot_budget(tmp_path):
    """A direct `pytest -n 8` session holds one slot, so it may start only its share of the cores."""
    pytest.importorskip("xdist")
    env = {k: v for k, v in os.environ.items() if k not in (slots.HELD_ENV, "PYTEST_XDIST_WORKER")}
    env.update(HERMES_TEST_SLOT_DIR=str(tmp_path / "slots"), HERMES_TEST_HOST_SLOTS="4", HERMES_TEST_XDIST_MAX="2")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-n", "8",
         f"{Path(__file__).relative_to(REPO_ROOT)}::test_xdist_worker_budget_is_cores_over_slots"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "capping xdist workers 8 -> 2" in out, out
    assert "created: 2/2 workers" in out, out


# --- orphaned holders are reclaimed [daveey] ----------------------------------------------------
# hermes-hub 2026-10-29: two direct `pytest` runs whose worktrees had been removed hung for 6h45m holding
# 2 of the 4 slots; every other agent's run_tests.sh waited behind them.

_HOLDER = ("import sys, time; sys.path.insert(0, sys.argv[1]); from scripts import run_tests_host_slots as s\n"
           "fd = s.acquire_host_slot()\nprint('held', flush=True)\ntime.sleep(120)\n")


def _spawn_holder(cwd, code=_HOLDER):
    env = {k: v for k, v in os.environ.items() if k != slots.HELD_ENV}
    child = subprocess.Popen([sys.executable, "-c", code, str(REPO_ROOT)], stdout=subprocess.PIPE, text=True,
                             env=env, cwd=str(cwd))
    line = child.stdout.readline().split()
    assert line[:1] == ["held"], line
    return child, line


def _acquire_in_thread():
    got: list = []
    done = threading.Event()

    def take():
        got.append(slots.acquire_host_slot())
        done.set()

    threading.Thread(target=take, daemon=True).start()
    return got, done


def test_slot_held_from_a_deleted_tree_is_reclaimed(slot_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    tree = tmp_path / "worktree"
    tree.mkdir()
    child, _ = _spawn_holder(tree)
    try:
        got, done = _acquire_in_thread()
        assert not done.wait(0.5), "a live holder keeps its slot"
        shutil.rmtree(tree)
        assert done.wait(15), "a holder whose tree was deleted must not keep the slot"
        if Path("/proc/self/stat").exists():  # identity provable: the hung orphan is killed (it ignored SIGTERM)
            assert child.wait(10) == -signal.SIGKILL
        slots.release_host_slot(got[0])
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(5)


def test_slot_held_past_the_age_cap_is_reclaimed_without_killing(slot_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    monkeypatch.setenv(slots.MAX_AGE_ENV, "1")
    child, _ = _spawn_holder(tmp_path)
    try:
        got, done = _acquire_in_thread()  # the waiter re-checks every REAP_EVERY_S, so the 1 s cap passes
        assert done.wait(15), "a slot held past the cap is reclaimed"
        assert child.poll() is None, "an over-age holder in a live tree is left running"
        slots.release_host_slot(got[0])
    finally:
        child.kill()
        child.wait(5)


def test_slot_kept_by_an_inherited_fd_of_a_dead_holder_is_reclaimed(slot_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    stop = tmp_path / "stop"
    code = ("import os, sys, time; sys.path.insert(0, sys.argv[1]); from scripts import run_tests_host_slots as s\n"
            "fd = s.acquire_host_slot()\npid = os.fork()\n"
            "if pid == 0:\n"
            f"    end = time.time() + 60\n    while time.time() < end and not os.path.exists({str(stop)!r}):\n"
            "        time.sleep(0.1)\n    os._exit(0)\n"
            "print('held', pid, flush=True)\nos._exit(0)\n")
    child, _ = _spawn_holder(tmp_path, code)
    try:
        child.wait(10)
        got, done = _acquire_in_thread()
        assert done.wait(15), "the recorded holder exited; its forked child must not keep the slot"
        slots.release_host_slot(got[0])
    finally:
        stop.touch()  # the reparented grandchild exits on its own (it is outside this test's kill scope)


def test_live_young_holder_is_not_reclaimed(slot_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    child, _ = _spawn_holder(tmp_path)
    try:
        assert slots.reclaim_stale_slots(str(slot_env), 1) == []
        assert (slot_env / "0.lock").exists()
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(5)


def test_release_clears_the_holder_record(slot_env, monkeypatch):
    """A record left after release would name a dead pid as the holder of an older runner that writes none."""
    monkeypatch.setenv("HERMES_TEST_HOST_SLOTS", "1")
    fd = slots.acquire_host_slot()
    assert (slot_env / "0.lock").read_text().startswith("{")
    slots.release_host_slot(fd)
    assert (slot_env / "0.lock").read_text() == ""


def test_slot_max_age_env_and_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv(slots.MAX_AGE_ENV, raising=False)
    assert slots.slot_max_age_s() == 45 * 60
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tests:\n  slot_max_age_s: 600\n")
    monkeypatch.setenv("HERMES_TEST_SLOT_CONFIG", str(cfg))
    assert slots.slot_max_age_s() == 600
    monkeypatch.setenv(slots.MAX_AGE_ENV, "0")
    assert slots.slot_max_age_s() == 0, "env wins; 0 disables the age cap"
