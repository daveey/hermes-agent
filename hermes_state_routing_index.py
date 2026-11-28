"""Opt-in sparse routing index; malformed inputs remain readable errors, not write errors."""

ACTIVE_ROUTING_INDEX = "idx_gateway_routing_active_snapshot"
# Every literal match is included. Invalid/unsupported JSON is included too, so
# the SELECT's original json_extract still raises on malformed rows. Indexing
# the unguarded extraction would instead reject formerly valid routing writes.
ACTIVE_ROUTING_PREDICATE = (
    "CASE WHEN json_valid(entry_json) "
    "THEN json_extract(entry_json, '$.active_turn_token') IS NOT NULL ELSE 1 END"
)
# Constant-key entries retain rowid order in either scan direction. Other
# readers cannot use this partial index without the explicit safe predicate.
ACTIVE_ROUTING_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS {ACTIVE_ROUTING_INDEX} "
    f"ON gateway_routing((1)) WHERE {ACTIVE_ROUTING_PREDICATE};"
)
