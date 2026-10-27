"""Explicit owner-attested legacy aliases; never infer a profile from a task ID."""
from __future__ import annotations

import hashlib
import json
import re


def record_digest(task: dict) -> str:
    raw = json.dumps(task, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_alias(alias: dict, binding: dict) -> None:
    owner = binding['owner']
    if (not isinstance(alias, dict) or set(alias) not in ({'key', 'version', 'digest', 'evidence'},
                                                         {'key', 'version', 'digest', 'evidence', 'path'})
            or alias.get('key') != f"{owner['board']}/{owner['task_id']}"
            or type(alias.get('version')) is not int or alias['version'] < 1
            or not isinstance(alias.get('digest'), str)
            or not re.fullmatch('[0-9a-f]{64}', alias['digest'])
            or not isinstance(alias.get('evidence'), str) or not alias['evidence'].strip()):
        raise ValueError('origin_legacy_invalid')
    if 'path' in alias:
        _validate_path(alias, owner['board'])


def _validate_path(alias: dict, board: str) -> None:
    path = alias['path']
    if not isinstance(path, list) or not 2 <= len(path) <= 32:
        raise ValueError('origin_legacy_invalid')
    seen = set()
    for node in path:
        if (not isinstance(node, dict) or set(node) != {'key', 'version', 'digest'}
                or not isinstance(node.get('key'), str)
                or not node['key'].startswith(board + '/') or node['key'].count('/') != 1
                or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', node['key'].split('/')[1])
                or node['key'] in seen
                or type(node.get('version')) is not int or node['version'] < 1
                or not isinstance(node.get('digest'), str)
                or not re.fullmatch('[0-9a-f]{64}', node['digest'])):
            raise ValueError('origin_legacy_invalid')
        seen.add(node['key'])
    if path[-1] != {k: alias[k] for k in ('key', 'version', 'digest')}:
        raise ValueError('origin_legacy_invalid')


def _publication(conn, node: dict, owner: dict):
    row = conn.execute("SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                       (owner['gateway'], node['key'])).fetchone()
    if not row or row[0] < node['version']:
        return None
    task = json.loads(row[1])
    if (row[0] != node['version'] or not isinstance(task, dict)
            or record_digest(task) != node['digest']
            or task.get('id') != node['key'].split('/')[1] or task.get('board') != owner['board']
            or task.get('gateway') not in (None, owner['gateway'])
            or task.get('profile') is not None):
        raise ValueError('origin_legacy_publication_mismatch')
    return task


def verify_publication(conn, alias: dict, binding: dict) -> bool:
    """Exact task-graph evidence proves association, never qualified ownership."""
    owner, origin = binding['owner'], binding['origin']
    nodes = alias.get('path', [alias])
    tasks = [_publication(conn, node, owner) for node in nodes]
    for parent, child in zip(tasks, nodes[1:]):
        if parent is None:
            continue
        children = parent.get('children')
        if not isinstance(children, list) or child['key'].split('/')[1] not in children:
            raise ValueError('origin_legacy_association_mismatch')
    root = tasks[0]
    if root is not None:
        links = root.get('sessions')
        if not isinstance(links, list) or not any(
                isinstance(link, dict) and link.get('rel') == 'shepherd'
                and link.get('gw') == origin['gateway'] and link.get('profile') == origin['profile']
                and link.get('session') == origin['session'] for link in links):
            raise ValueError('origin_legacy_association_mismatch')
    return all(task is not None for task in tasks)


def check_conflicts(conn, alias: dict, binding: dict) -> None:
    """The same legacy association cannot be attested to two different owners."""
    for raw, in conn.execute('SELECT receipt_json FROM origin_receipts'):
        ack = json.loads(raw)
        prior = ack.get('legacy_alias')
        if (prior and ack['owner']['gateway'] == binding['owner']['gateway']
                and ack['origin'] == binding['origin']
                and prior['key'] == alias['key']
                and prior['version'] == alias['version'] and prior['digest'] == alias['digest']
                and ack['owner'] != binding['owner']):
            raise ValueError('origin_legacy_conflict')


def aliases_for_projection(receipts: list[dict], binding: dict) -> list[dict]:
    """Only receipts joined to this exact obligation may suppress an alias."""
    from hermes_cli.kanban_origin_ledger import binding_hash
    digest = binding_hash(binding)
    result = []
    for ack in receipts:
        if ack.get('binding_hash') != digest:
            continue
        alias = ack.get('legacy_alias')
        if alias is not None:
            validate_alias(alias, binding)
            result.append({k: alias[k] for k in ('key', 'version', 'digest')})
    return result


def supersedes(card: dict, row: dict) -> bool:
    """Require exact source bytes/version and origin, not merely a matching ID."""
    if (card.get('profile') is not None or card.get('gateway') != row['gateway']
            or card.get('origin_gateway') != row['origin_gateway']
            or card.get('origin_profile') != row['origin_profile']
            or card.get('session') != row['origin_session']):
        return False
    return any(card.get('legacy_record') == alias for alias in row.get('legacy_aliases', []))
