"""Core's wire schema must be accepted by the deployed Board's declared row contract."""
import json
from pathlib import Path

from hermes_cli import kanban_db_sync as sync


def test_wire_columns_are_writable_on_board():
    snapshot = json.loads(
        (Path(sync.__file__).with_name("kanban_board_row_fields.json")).read_text(encoding="utf-8")
    )
    missing = sorted(
        f"{table}.{wire}"
        for table, columns in sync.SYNCED_COLUMNS.items()
        for column in columns
        if (wire := sync.REF_COLUMNS.get((table, column), column))
        not in snapshot["row_fields"].get(table, [])
    )
    assert not missing, (
        f"Board {snapshot['commit']} does not accept {', '.join(missing)}: "
        "add it to hermes-hub worker/src/rows.ts ROW_FIELDS and board_schema.ts "
        "(ADDED_*_COLUMNS), deploy the Board, then update this snapshot"
    )
