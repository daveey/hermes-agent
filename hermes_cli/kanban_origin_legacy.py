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
    if (not isinstance(alias, dict) or set(alias) != {'key', 'version', 'digest', 'evidence'}
            or alias.get('key') != f"{owner['board']}/{owner['task_id']}"
            or type(alias.get('version')) is not int or alias['version'] < 1
            or not isinstance(alias.get('digest'), str)
            or not re.fullmatch('[0-9a-f]{64}', alias['digest'])
            or not isinstance(alias.get('evidence'), str) or not alias['evidence'].strip()):
        raise ValueError('origin_legacy_invalid')


def verify_publication(conn, alias: dict, binding: dict) -> bool:
    """Return pending only for missing/behind publication; reject changed evidence."""
    owner, origin = binding['owner'], binding['origin']
    row = conn.execute("SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                       (owner['gateway'], alias['key'])).fetchone()
    if not row or row[0] < alias['version']:
        return False
    task = json.loads(row[1])
    if (row[0] != alias['version'] or not isinstance(task, dict)
            or record_digest(task) != alias['digest']
            or task.get('id') != owner['task_id'] or task.get('board') != owner['board']
            or task.get('gateway') not in (None, owner['gateway'])
            or task.get('profile') is not None):
        raise ValueError('origin_legacy_publication_mismatch')
    links = task.get('sessions')
    if not isinstance(links, list) or not any(
            isinstance(link, dict) and link.get('rel') == 'shepherd'
            and link.get('gw') == origin['gateway'] and link.get('profile') == origin['profile']
            and link.get('session') == origin['session'] for link in links):
        raise ValueError('origin_legacy_association_mismatch')
    return True


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
