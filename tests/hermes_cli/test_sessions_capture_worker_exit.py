"""Real worker processes: imports ahead of the request, and the verified early exit."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from hermes_cli._launchers import runtime_command
from hermes_cli._startup_fast import project_root_str
from hermes_cli.sessions_capture_process import CaptureProcess, CaptureUnavailable
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms('linux')
ROOT = project_root_str()


def wire(value):
    request = json.dumps(value).encode()
    return struct.pack('!I', len(request)) + request


def one_frame(stdout):
    size = struct.unpack('!I', stdout[:4])[0]
    assert len(stdout) == 4 + size, stdout[:200]
    return json.loads(stdout[4:])


# A stand-in reader module that prints while it is imported, then records that it was.
NOISY_READERS = '''
import importlib.abc, importlib.util, sys
class Readers(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path=None, target=None):
        if name == 'hermes_cli.web_routers.profiles':
            return importlib.util.spec_from_loader(name, self)
    def exec_module(self, module):
        print('import-time noise')
        def sidebar(**selector): return {}
        sidebar.__wrapped__ = sidebar
        module.get_profiles_sessions = lambda **selector: {'sessions': [{'id': 'stub-row'}]}
        module.get_profiles_sessions_sidebar = module.get_profiles_session_stamps = sidebar
        open(sys.argv[1], 'w').close()
sys.meta_path.insert(0, Readers())
from hermes_cli import sessions_capture_worker
raise SystemExit(sessions_capture_worker.main())
'''


def test_reader_imports_precede_the_request_and_cannot_print_into_the_frame(tmp_path):
    imported = tmp_path / 'imported'
    child = subprocess.Popen([sys.executable, '-c', NOISY_READERS, str(imported)], cwd=ROOT,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # No request byte has been written: only a pre-request import can create the marker.
        wait_until(lambda: imported.exists() or child.poll() is not None, timeout=60, what='reader imports')
        assert imported.exists() and child.poll() is None
        stdout, stderr = child.communicate(wire({'name': 'sessions', 'selector': {}}), timeout=60)
    finally:
        child.kill()
        child.wait(5)
    assert one_frame(stdout) == {'sessions': [{'id': 'stub-row'}]}
    assert b'import-time noise' in stderr and child.returncode == 0


@pytest.mark.parametrize('request_bytes', [wire({'nope': 1}), wire({'name': 'bogus', 'selector': {}}),
                                           b'\x00\x00', struct.pack('!I', 17 * 1024)])
def test_invalid_request_still_exits_two_without_a_frame(request_bytes):
    done = subprocess.run([sys.executable, '-m', 'hermes_cli.sessions_capture_worker'], cwd=ROOT,
                          input=request_bytes, capture_output=True, timeout=120, check=False)
    assert done.returncode == 2 and done.stdout == b''


IMPORT_ONLY = '''
import json, sys
seen = []
def audit(event, args):
    if event == 'sqlite3.connect' or (event == 'open' and str(args[0]).endswith(('config.yaml', '.db'))):
        seen.append([event, str(args[0])])
sys.addaudithook(audit)
from hermes_cli import sessions_capture_worker
sessions_capture_worker.preimport()
found, seen = list(seen), []
import os, sqlite3
open(os.path.join(os.environ['HERMES_HOME'], 'config.yaml')).close()
sqlite3.connect(':memory:').close()
print(json.dumps({'seen': found, 'readers': 'hermes_cli.web_routers.profiles' in sys.modules,
                  'control': [event for event, _ in seen]}))
'''


def test_reader_imports_open_no_database_and_read_no_config(tmp_path):
    # A standby imports before any request authorises a read: the imports alone must touch no data.
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text('{}\n', encoding='utf-8')
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(tmp_path))
    done = subprocess.run([sys.executable, '-c', IMPORT_ONLY], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.splitlines()[-1]) == {
        'seen': [], 'readers': True, 'control': ['open', 'sqlite3.connect']}  # the control proves the audit sees both
    # Importing scaffolds the home's directories (as every Hermes process does) but creates no store.
    assert not [path.name for path in home.rglob('*') if path.name.endswith(('.db', '.db-wal', '.db-shm'))]
    assert (home / 'config.yaml').read_text(encoding='utf-8') == '{}\n'


# The canonical worker with an exit hook only the ordinary interpreter exit runs, plus one fault.
EXIT_PROBE = '''
import atexit, os, runpy, sys, threading
atexit.register(lambda: open({marker!r}, 'w').close())
fault = {fault!r}
if fault == 'switch':
    os.environ['HERMES_SESSIONS_CAPTURE_FAST_EXIT'] = '0'
elif fault == 'step':
    from hermes_cli import runtime_state
    def refuse():
        raise OSError('lease')
    runtime_state.release_process_leases = refuse
elif fault == 'descriptor':
    import logging
    logging.shutdown = lambda: os.open(os.devnull, os.O_RDONLY)
elif fault == 'thread':
    # Lives until the main thread is finishing, as a real worker thread with work left would.
    threading.Thread(target=threading.main_thread().join).start()
runpy.run_module('hermes_cli.sessions_capture_worker', run_name='__main__', alter_sys=True)
'''


@pytest.fixture
def store(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HOME', str(home))
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('canonical-row', source='cli', model='test')
    db.close()
    return tmp_path


@pytest.mark.parametrize('fault', [None, 'switch', 'step', 'descriptor', 'thread'])
def test_worker_exits_early_only_when_its_cleanup_is_verified(store, monkeypatch, fault):
    from hermes_cli import sessions_capture_process as module
    from tests.hermes_cli.test_sessions_native_startup import selected
    marker, diagnostics = store / 'ordinary-exit', store / 'child-stderr'
    original = module.spawn_owned

    def diagnostic_spawn(*args, **kwargs):
        with diagnostics.open('wb') as output:
            kwargs['stderr'] = output
            return original(*args, **kwargs)
    monkeypatch.setattr(module, 'spawn_owned', diagnostic_spawn)
    capture = CaptureProcess(seconds=30, command=runtime_command(
        Path(ROOT), code=EXIT_PROBE.format(marker=str(marker), fault=fault)))
    try:
        # One frame, EOF and exit code 0 are what the parent requires of either exit path.
        try:
            bodies = capture.capture_many([('sessions', selected(limit=20)), ('sessions_sidebar', {})])
        except CaptureUnavailable as error:
            pytest.fail(f'{error.reason}: {diagnostics.read_text()[-3000:]}')
    finally:
        capture.close()
    assert any(row['id'] == 'canonical-row' for row in json.loads(bodies[0])['sessions'])
    assert isinstance(bodies[1], bytes)
    assert marker.exists() == (fault is not None), diagnostics.read_text()[-3000:]
