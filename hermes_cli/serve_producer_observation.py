"""Read-only live shared-family observation under the collector's LaunchGate.

broker.json is a locator, never custody evidence. Promotion requires the exact
current enrolled broker, exact exec argv/source generation, its PID-owned family
FLOCK, and the child's independent PID-owned execution FLOCK plus inherited family
inode. Every PID is reobserved. No PONG/readiness boolean can replace these checks.
"""
from __future__ import annotations

import os
from pathlib import Path

from hermes_cli.serve_producer_admission import launcher_generation
from hermes_cli.serve_producer_broker import OwnerUnavailable, read_owner
from hermes_cli.serve_producer_census import CensusUnavailable, LinuxProcessCensus, _read_bytes
from hermes_cli.serve_producer_custody import CustodyUnsafe, _private

_MAX_FDS = 1024


def _descriptor(record, path, *, owner):
    """Find a current description; foreign/inherited lock owners never certify execution."""
    info = path.stat(follow_symlinks=False)
    _private(info, directory=False)
    entries = []
    with os.scandir(f'/proc/{record.identity.pid}/fd') as scan:
        for entry in scan:
            if len(entries) >= _MAX_FDS:
                raise CensusUnavailable('producer descriptor observation budget')
            entries.append(entry.name)
    for name in entries:
        try:
            actual = Path(f'/proc/{record.identity.pid}/fd/{name}').stat()
            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                continue
            lines = _read_bytes(Path(f'/proc/{record.identity.pid}/fdinfo/{name}'), 8192).decode('ascii').splitlines()
            if any(line.startswith('lock:') and ' FLOCK ' in line and ' WRITE ' in line
                   and f' {owner} ' in line for line in lines):
                current = path.stat(follow_symlinks=False)
                return (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
        except FileNotFoundError:
            continue  # unrelated descriptors can close during observation
    return False


def _exec(record, installation, role):
    argv = record.argv
    return (len(argv) == (8 if role == 'broker' else 7)
            and argv[1:4] == ('-I', str(installation / 'hermes_cli/serve_producer_process.py'), role)
            and argv[4].isdigit() and argv[5] == str(installation)
            and (argv[6] == '-1' if role == 'broker' else argv[6].isdigit())
            and (role != 'broker' or argv[7].isdigit()))


def observe_shared_family(runtime, installation, participants, generation, *, gate):
    """Return exact live broker/child observations or empty; uncertainty fails admission.

    This does not authorize attachment: the relay independently verifies compatibility
    and the complete provider manifest. The caller holds the same stable gate until
    attachment, preventing another supported owner from replacing this family.
    """
    runtime, installation = Path(runtime), Path(installation)
    gate.require_runtime(runtime)
    try:
        _private(runtime.stat(follow_symlinks=False), directory=True)
        owner = read_owner(runtime)
        if owner is None:
            return ()
        pid, child_pid = owner['pid'], owner.get('child_pid')  # census identity() refuses non-int pids
        adapter = LinuxProcessCensus()
        boot = adapter.boot_id()
        broker = adapter.read_process(pid, boot)
        child = adapter.read_process(child_pid, boot)
        tickets = [row for row in participants if row.key == broker.key
                   and row.role == 'producer_broker' and row.installation == str(installation)
                   and row.generation == generation]  # key pins the observation digest
        if (len(tickets) != 1 or not _exec(broker, installation, 'broker')
                or not _exec(child, installation, 'producer')
                or not _descriptor(broker, runtime / 'panel.lock', owner=pid)
                or not _descriptor(child, runtime / 'panel.lock', owner=pid)
                or not _descriptor(child, runtime / 'panel-execution.lock', owner=child_pid)
                or adapter.read_process(pid, boot) != broker
                or adapter.read_process(child_pid, boot) != child
                or launcher_generation(installation) != generation
                or read_owner(runtime) != owner):
            return ()
        return broker, child
    except (OSError, ValueError, KeyError, CensusUnavailable, CustodyUnsafe, OwnerUnavailable):
        return ()
