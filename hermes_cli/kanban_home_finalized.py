"""Structural SQL release authority shared by dormant home-custody consumers.

A scoped receipt is not enough: the unique related manifest must include the
single canonical task/home/version row. This predicate also protects stores
containing malformed receipts written by earlier dormant code. [daveey]
"""
from __future__ import annotations


def predicate(prepared: str = "p", receipt: str = "f") -> str:
    """Trusted internal SQL aliases only; no connection-local UDF is required."""
    from hermes_cli.kanban_db_sync import SYNCED_TABLES

    p, f = prepared, receipt
    raw = f"{f}.receipt"
    tables = ",".join("'" + t.replace("'", "''") + "'" for t in SYNCED_TABLES)
    # CASE protects json_each/extract from malformed persisted JSON and from
    # array scalars (json_each.value for a string is not itself JSON text).
    manifest = f"""CASE WHEN json_valid({raw}) THEN
        json_type({raw}) IS 'object'
        AND json_extract({raw},'$.task') IS {p}.task_id
        AND json_extract({raw},'$.source') IS {p}.source
        AND json_extract({raw},'$.destination') IS {p}.destination
        AND json_extract({raw},'$.nonce') IS {p}.nonce
        AND {f}.nonce IS {p}.nonce
        AND json_type({raw},'$.expected_version') IS 'integer'
        AND json_extract({raw},'$.expected_version') IS {p}.expected_version
        AND json_extract({raw},'$.phase') IS 'materialized'
        AND json_type({raw},'$.rows') IS 'array'
        AND json_array_length({raw},'$.rows') > 0
        AND json_extract({raw},'$.rows[0].table') IS 'tasks'
        AND json_extract({raw},'$.rows[0].uid') IS {p}.task_id
        AND json_extract({raw},'$.rows[0].fields.id') IS {p}.task_id
        AND json_extract({raw},'$.rows[0].fields.created_on') IS {p}.destination
        AND json_type({raw},'$.rows[0].fields.version') IS 'integer'
        AND json_extract({raw},'$.rows[0].fields.version') IS {p}.expected_version + 1
        AND json_extract({raw},'$.rows[0].fields.deleted_at') IS NULL
        AND NOT EXISTS(SELECT 1 FROM json_each({raw},'$.rows') r WHERE NOT
            CASE WHEN r.type IS 'object' THEN
                json_extract(r.value,'$.table') IN ({tables})
                AND json_type(r.value,'$.uid') IS 'text'
                AND json_type(r.value,'$.fields') IS 'object'
                AND CASE json_extract(r.value,'$.table')
                    WHEN 'tasks' THEN r.key=0 AND json_extract(r.value,'$.uid') IS {p}.task_id
                        AND json_extract(r.value,'$.fields.id') IS {p}.task_id
                    WHEN 'task_links' THEN {p}.task_id IN
                        (json_extract(r.value,'$.fields.parent_id'),json_extract(r.value,'$.fields.child_id'))
                    ELSE json_extract(r.value,'$.fields.task_id') IS {p}.task_id END
            ELSE 0 END IS 1)
        AND (SELECT count(*) FROM json_each({raw},'$.rows')) =
            (SELECT count(*) FROM (SELECT json_extract(r.value,'$.table'),json_extract(r.value,'$.uid')
                FROM json_each({raw},'$.rows') r WHERE r.type IS 'object' GROUP BY 1,2))
        ELSE 0 END"""
    return f"({f}.task_id={p}.task_id AND ({manifest}) IS 1)"
