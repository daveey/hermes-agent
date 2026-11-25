"""Exercise the real notification revision reader on the affected native runtime."""
import faulthandler
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from tui_gateway.notification_idle import state_revision
from tui_gateway import kanban_replica_observer

faulthandler.dump_traceback_later(5, exit=True)
print('RUNTIME', sys.version, sqlite3.sqlite_version, flush=True)
conn = sqlite3.connect(':memory:', check_same_thread=False)
lock = threading.Lock()
entered = threading.Event()
release = threading.Event()
errors = []

def authorize(*args):
    if not entered.is_set():
        entered.set()
        assert release.wait(2), 'revision reader never released the writer'
    return sqlite3.SQLITE_OK

conn.set_authorizer(authorize)

def write():
    try:
        with lock:
            conn.execute('CREATE TABLE evidence(value)')
    except BaseException as exc:
        errors.append(exc)

thread = threading.Thread(target=write)
thread.start()
assert entered.wait(2)
db = SimpleNamespace(db_path=Path('unused.db'), _lock=lock, _conn=conn)
kanban_replica_observer.fingerprint = lambda path: 'committed-revision'
print('REVISION_START', flush=True)
try:
    assert state_revision(db) is None
finally:
    release.set()
thread.join(2)
assert not thread.is_alive()
assert not errors, errors
assert state_revision(db) == 'committed-revision'
conn.close()
faulthandler.cancel_dump_traceback_later()
print('REVISION_FINISHED writer completed; idle revision observed', flush=True)
