"""The native live-test diagnostics must not crash the process they observe.

faulthandler's native watchdog walks unowned interpreter frames without the GIL,
so a thread retiring a short-lived code object mid-walk takes the gateway down
with SIGSEGV (CI: ``test_native_gateway_runtime_live`` exited -11). Run the real
fixture with an aggressive dump interval while worker threads churn dynamically
compiled code; the refcounted Python-frame observer must survive it.
"""
from pathlib import Path
import os
import subprocess
import sys


FIXTURE = Path(__file__).parent / 'fixtures' / 'stack_dump_daemon.py'

SCRIPT = r'''
import os, runpy, threading

before = set(threading.enumerate())
stop = threading.Event()

def worker():
    while not stop.is_set():
        namespace = {}
        exec(compile('def churn(n):\n if n: return churn(n-1)\nchurn(30)', '<churn>', 'exec'), namespace)
        namespace.clear()

def gateway(*args, **kwargs):
    threads = [threading.Thread(target=worker) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        stop.wait(float(os.environ['CHURN_SECONDS']))
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive()

runpy.run_module = gateway
runpy.run_path(os.environ['DUMP_FIXTURE'], run_name='__main__')
assert set(threading.enumerate()) == before, 'diagnostics thread outlived the fixture'
'''


def _run_fixture(home, seconds):
    env = {**os.environ, 'HERMES_HOME': str(home), 'DUMP_FIXTURE': str(FIXTURE),
           'UGW_STACK_DUMP_AFTER': '0.0001', 'CHURN_SECONDS': str(seconds),
           'PYTHONFAULTHANDLER': '1'}
    result = subprocess.run([sys.executable, '-c', SCRIPT], env=env,
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)


def test_stack_dump_survives_retiring_code_objects(tmp_path):
    # A /dev/null sink keeps each dump cheap, so thousands of walks overlap the
    # churn: the old native-watchdog fixture crashed 6 of 6 runs like this.
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs' / 'stacks.txt').symlink_to(os.devnull)
    _run_fixture(tmp_path, 3)


def test_stack_dump_records_thread_stacks(tmp_path):
    _run_fixture(tmp_path, 1)
    text = (tmp_path / 'logs' / 'stacks.txt').read_text(encoding='utf-8')
    assert '<churn>' in text and '--- Thread 0x' in text
