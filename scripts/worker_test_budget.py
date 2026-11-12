"""Linux worker test-slot inheritance: evidence, not a caller-supplied flag.

Receipts are process metadata, not configuration. Only a live ancestor holding
an exclusive flock on a canonical slot can waive reacquisition. Never unlink a
held slot: age/deleted cwd cannot prove the holder's descendants stopped.
"""
from __future__ import annotations

import json
import os
from collections.abc import MutableMapping
from pathlib import Path

RECEIPT_ENV = 'HERMES_TEST_SLOT_RECEIPT'
THREAD_VARS = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
               'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS')


def _stat(pid: int) -> tuple[int, int]:
    parts = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
    return int(parts[1]), int(parts[19])


def inherited_slot() -> bool:
    from scripts.run_tests_host_slots import host_slot_dir, host_slot_count
    try:
        rec = json.loads(os.environ.get(RECEIPT_ENV, 'null'))
        if not isinstance(rec, dict):
            return False
        pid, fd, birth, slot = (rec[k] for k in ('pid', 'fd', 'birth', 'slot'))
        if any(type(v) is not int for v in (pid, fd, birth, slot)) or pid < 1 or fd < 0:
            return False
        if not 0 <= slot < host_slot_count():
            return False
        current = os.getpid()
        for _ in range(128):
            parent, started = _stat(current)
            if current == pid:
                if started != birth:
                    return False
                break
            if parent <= 0 or parent == current:
                return False
            current = parent
        else:
            return False
        expected = Path(host_slot_dir()) / f'{slot}.lock'
        st = expected.stat()
        held = os.stat(f'/proc/{pid}/fd/{fd}')
        if (st.st_dev, st.st_ino) != (held.st_dev, held.st_ino):
            return False
        record = json.loads(expected.read_text(encoding='utf-8'))
        if (not isinstance(record, dict) or not isinstance(rec.get('nonce'), str) or not rec['nonce'] or
                record.get('nonce') != rec['nonce'] or record.get('pid') != pid or
                record.get('start') != birth):
            return False
        identity = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
        for line in Path(f'/proc/{pid}/fdinfo/{fd}').read_text(encoding='utf-8').splitlines():
            fields = line.split()
            if (len(fields) == 9 and fields[0] == 'lock:' and
                    fields[2:6] == ['FLOCK', 'ADVISORY', 'WRITE', str(pid)] and
                    fields[6:] == [identity, '0', 'EOF']):
                return _stat(pid)[1] == birth
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False
    return False


def child_environment(env: MutableMapping[str, str], fd: int | None) -> None:
    from hermes_cli.worker_compute_capacity import worker_enabled
    from scripts.run_tests_host_slots import HELD_ENV, host_slot_dir, host_slot_count, _read_holder
    env[HELD_ENV] = '1'
    if not worker_enabled():
        return
    env.update(dict.fromkeys(THREAD_VARS, '1'))
    if fd is None:
        if not inherited_slot():
            raise RuntimeError('worker test child has no verified host slot')
        env[RECEIPT_ENV] = os.environ[RECEIPT_ENV]
        return
    st = os.fstat(fd)
    for slot in range(host_slot_count()):
        path = Path(host_slot_dir()) / f'{slot}.lock'
        if not path.exists():
            continue
        candidate = path.stat()
        if (candidate.st_dev, candidate.st_ino) == (st.st_dev, st.st_ino):
            record = _read_holder(fd)
            if not record or not record.get('nonce'):
                raise RuntimeError('worker test lock has no acquisition identity')
            env[RECEIPT_ENV] = json.dumps({'pid': os.getpid(), 'birth': _stat(os.getpid())[1],
                                         'fd': fd, 'slot': slot, 'nonce': record['nonce']})
            return
    raise RuntimeError('worker test lock is outside canonical slot domain')
