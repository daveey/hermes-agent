"""Schema-owned canonical create transport; defaults never erase caller intent.

The courier is a typed envelope, not a second independently maintained create
API. Runtime custody is carried by its authenticated envelope, never model args.
"""
from __future__ import annotations

# Exclude only gateway-local controls. Each decision must be covered by the
# schema census, so adding a model-facing property cannot silently lose it.
EXCLUDED = {
    'board': 'The authenticated destination envelope selects the default board.',
    'placement': 'Consumed into execution runs_on before storage-home selection.',
    'workspace_kind': 'Only scratch is portable; host source kinds refuse in planning.',
    'workspace_path': 'A host-local source path cannot be transferred by the courier.',
    'project': 'A project names a host-local checkout and refuses in planning.',
    'project_id': 'Internal resolved project identity is host-local, not transport authority.',
    'origin_session': 'Qualified origin identity is resolved into the authenticated envelope.',
    'session_id': 'Receiver-local session identity comes only from the courier envelope.',
    'force_hub': 'Operator admission overrides cannot cross gateway execution boundaries.',
}


def properties():
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    return KANBAN_CREATE_SCHEMA['parameters']['properties']


def payload(args):
    """Forward all supplied schema fields except documented local controls.

    None retains omission semantics. False, zero and empty containers/strings
    remain values; validation and defaults are still owned by common create.
    Unknown internal fields must be classified, not silently discarded.
    """
    unknown = set(args) - set(properties()) - set(EXCLUDED)
    if unknown:
        raise ValueError('canonical_home_unknown_fields: ' + ','.join(sorted(unknown)))
    return {key: value for key, value in args.items() if key not in EXCLUDED and value is not None}


def validate_forward(create):
    """Shape-check against the same schema before origin authorization.

    Common create still performs semantic admission. Never accept raw internal
    kwargs just because this module forwards newly declared public properties.
    """
    from jsonschema import Draft7Validator
    if not isinstance(create, dict):
        raise ValueError('origin_forward_create_invalid')
    fields = properties()
    if set(create) - (set(fields) - set(EXCLUDED)) - {'session_id'}:
        raise ValueError('origin_forward_create_invalid')
    for key, value in create.items():
        if key == 'session_id':
            continue  # validated as a qualified envelope component by the caller
        # This boundary owns JSON shape, not semantic admission (for example a
        # partial human_request must reach common create's typed refusal).
        shape = {name: fields[key][name] for name in ('type', 'items') if name in fields[key]}
        if list(Draft7Validator(shape).iter_errors(value)):
            raise ValueError('origin_forward_create_invalid')


def main():
    """Read-only courier preflight; old cores lacking this entry refuse safely."""
    import json
    import sys
    try:
        raw = sys.stdin.read(262145)
        if len(raw.encode('utf-8')) > 262144:
            raise ValueError('canonical_home_payload_too_large')
        args = json.loads(raw)
        if not isinstance(args, dict) or payload(args) != args:
            raise ValueError('canonical_home_local_control')
        validate_forward(args)
        if not all(isinstance(args.get(key), str) and args[key].strip() for key in ('title', 'assignee')):
            raise ValueError('canonical_home_identity_invalid')
    except (TypeError, ValueError, RecursionError) as exc:
        print(json.dumps({'canonical_home': 2, 'valid': False, 'error': str(exc)}))
        return 2
    print(json.dumps({'canonical_home': 2, 'valid': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
