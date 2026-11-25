"""Read-only status evidence for processes supervised by a different backend.

The tool registry and completion receipts are not a complete live inventory after
restart. Check the same profile checkpoint used by process_manage's foreign route,
without adopting a process, starting a watcher or changing its completion custodian.
"""
from __future__ import annotations

import json


def checkpoint_items(registry, owner, known):
    from tools.environments.sandbox_scope import sandbox_visible
    from tools.process_registry import _checkpoint_path
    from tools.process_registry_results import _owns_result

    try:
        entries = json.loads(_checkpoint_path().read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return []
    if not isinstance(entries, list):
        raise ValueError('malformed process checkpoint')
    items = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('parent_session_id'), str):
            raise ValueError('process checkpoint owner unavailable')
        if not _owns_result(owner, entry['parent_session_id']):
            continue
        pid = entry.get('session_id')
        if not isinstance(pid, str) or not pid.startswith('proc_'):
            raise ValueError('process checkpoint identity unavailable')
        if pid in known:
            continue  # current registry/structured completion is authoritative
        if (entry.get('pid_scope', 'host') != 'host'
                or not sandbox_visible(entry.get('sandbox_key', ''))):
            raise ValueError('process checkpoint scope unavailable')
        host_pid, birth = entry.get('pid'), entry.get('host_start_time')
        if type(host_pid) is not int or host_pid <= 0 or type(birth) is not int or birth <= 0:
            raise ValueError('process checkpoint birth unavailable')
        if registry._detached_host_fate(host_pid, birth) != 'running':
            raise ValueError('process checkpoint missing completion evidence')
        items.append({'kind': 'process', 'id': f'process:{pid}', 'state': 'running',
                      'label': str(entry.get('command') or pid)[:120], 'at': 0.0})
    return items
