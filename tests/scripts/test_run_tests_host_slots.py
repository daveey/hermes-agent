"""Host-wide test slots: concurrent runs share os.cpu_count() pytest processes (scripts/run_tests_host_slots.py)."""

from __future__ import annotations

import importlib.util
import os
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
