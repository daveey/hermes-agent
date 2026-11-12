"""Bounded, sanitized /proc evidence for sustained worker-load alerts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time

from hermes_cli import worker_compute_capacity as capacity


def _text(value: str, length=64) -> str:
    return re.sub(r'[^A-Za-z0-9_.:+-]', '_', value)[:length]


def _processes(proc: Path) -> dict:
    result = {}
    for path in proc.iterdir():
        if not path.name.isdecimal():
            continue
        try:
            raw = (path / 'stat').read_text(encoding='utf-8')
            opening, closing = raw.index('('), raw.rindex(')')
            fields = raw[closing + 2:].split()
            # Fields after comm start at stat field 3; starttime is field 22.
            ticks, birth = int(fields[11]) + int(fields[12]), int(fields[19])
            cgroup = (path / 'cgroup').read_text(encoding='utf-8')
            unified = next(line[3:] for line in cgroup.splitlines() if line.startswith('0::'))
            result[int(path.name)] = dict(birth=birth, ticks=ticks, comm=_text(raw[opening + 1:closing]),
                                          group=unified, uid=path.stat().st_uid)
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited during snapshot, not a living omitted CPU owner
        except (PermissionError, ValueError, IndexError, StopIteration):
            result[int(path.name)] = None
    return result


def snapshot(*, proc: Path = Path('/proc'), interval: float = 0.2) -> dict:
    """Interval CPU (100% = one core); no command lines or environment reads."""
    if not 0 < interval <= 1:
        raise ValueError('invalid CPU evidence sampling interval')
    owners = {}
    owner_errors = 0
    directory = capacity.policy_root() / 'worker-resources' / 'owners'
    if directory.exists():
        for path in directory.glob('*.json'):
            try:
                owner = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(owner, list) or len(owner) != 2
                        or not all(isinstance(value, str) and value for value in owner)
                        or capacity.owner_slice(*owner) != path.stem):
                    raise ValueError('invalid owner evidence')
                owners[path.stem] = _text(owner[1])
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                owner_errors += 1
    first = _processes(proc)
    started = time.monotonic()
    time.sleep(interval)
    second = _processes(proc)
    elapsed = time.monotonic() - started
    if elapsed <= 0:
        raise ValueError('invalid CPU evidence clock')
    hz = os.sysconf('SC_CLK_TCK')
    rows = []
    unknown = 0
    for pid, current in second.items():
        previous = first.get(pid)
        if current is None or previous is None or current['birth'] != previous['birth']:
            unknown += 1
            continue
        delta = current['ticks'] - previous['ticks']
        if delta < 0:
            unknown += 1
            continue
        parts = current['group'].split('/')
        owned = next((owners[part] for part in parts if part in owners), None)
        contained = 'hermes-kanban.slice' in parts
        rows.append(dict(pid=pid, comm=current['comm'], cpu_percent=round(100 * delta / hz / elapsed, 2),
                         card=owned or 'unknown', contained=contained,
                         uncontained_same_uid=current['uid'] == os.getuid() and not contained))
    rows.sort(key=lambda row: (-row['cpu_percent'], row['pid']))
    return dict(cpu_basis='interval; 100% = one core', sample_seconds=round(elapsed, 3),
                process_inventory='partial' if unknown else 'observed', unknown_processes=unknown,
                owner_map='partial' if owner_errors else 'observed', owner_errors=owner_errors,
                processes=rows[:20],
                uncontained_active=[row for row in rows if row['uncontained_same_uid'] and row['cpu_percent'] > 0][:20])
