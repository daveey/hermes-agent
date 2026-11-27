"""Count session slices from a narrow, count-only admission index. [daveey]

The common count previously scanned wide records and repeatedly decoded the
lineage JSON markers. Normal deferred schema reconciliation now indexes the
same fields and exact JSON expressions. An arithmetic tautology opts only this
reader into the partial index, avoiding changes to other readers' LIMIT/ties.
Read-only/old stores without the index still execute the identical predicate.

For negative source filters, enumerate current non-NULL sources with successor
probes, then seek admitted sources instead of walking excluded history. MIN
ignores NULL exactly as NOT IN rejects a NULL source; the original NOT IN also
retains NULL-bound semantics. All work shares one statement/snapshot. No source
names, counts or rows survive the call. No DDL is performed by a read.
"""


def count_query(clauses: list[str], *, seek_sources: bool) -> str:
    from hermes_state_sessions import _where_sql

    prefix = ""
    if seek_sources:
        clauses = list(clauses)
        for i, clause in enumerate(clauses):
            if clause.startswith("s.source NOT IN ("):
                prefix = """WITH RECURSIVE source_keys(source) AS (
                    SELECT MIN(source) FROM sessions
                    UNION ALL
                    SELECT (SELECT MIN(source) FROM sessions WHERE source > source_keys.source)
                    FROM source_keys WHERE source IS NOT NULL
                ) """
                clauses[i] = f"s.source IN (SELECT source FROM source_keys s WHERE {clause})"
                break
    clauses = [*clauses, '(s.id+0 IS NULL OR s.id+0 IS NOT NULL)']
    return prefix + f"SELECT COUNT(*) FROM sessions s{_where_sql(clauses, ' ')}"


def count_index_sql(json_extract) -> str:
    """Normal deferred schema reconciliation; never installed by a read. [daveey]"""
    expressions = ','.join(json_extract('model_config', path) for path in
                           ('$._delegate_from', '$._branched_from', '$._reset_from'))
    # [daveey] The tautology is an explicit opt-in: unrelated LIMIT/order readers cannot use this index.
    # Keep only admission fields/markers, not the wide model config or transcript payload.
    return f"""CREATE INDEX IF NOT EXISTS idx_sessions_count_admission ON sessions(
        source, archived, message_count, parent_session_id, started_at, session_key, created_source, id,
        {expressions}) WHERE (id+0 IS NULL OR id+0 IS NOT NULL);"""
