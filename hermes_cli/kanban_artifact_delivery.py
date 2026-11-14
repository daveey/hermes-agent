"""Resolve custody through receiver-local attachment rows, never foreign paths."""
from __future__ import annotations

import hashlib
from pathlib import Path


def receipt_delivery_paths(conn, task_id: str, records: list) -> list[str]:
    from hermes_cli.kanban_artifact_receipt import executing_machine

    rows = [dict(row) for row in conn.execute(
        "SELECT * FROM task_attachments WHERE task_id=?", (task_id,),
    )]
    machine = executing_machine(conn)
    paths = []
    for record in records:
        if not isinstance(record, dict) or record.get("warning") or not record.get("sha256"):
            continue
        uid = record.get("attachment_uid")
        for row in rows:
            matches = (uid and row.get("uid") == uid) or (
                not uid and record.get("machine") == machine
                and record.get("attachment_id") == row["id"]
            )
            if not matches:
                continue
            path = Path(row["stored_path"])
            try:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except OSError:
                continue  # Blob sync may not have fetched bytes yet.
            if digest == record["sha256"] and str(path) not in paths:
                paths.append(str(path))
    return paths


def delivery_paths(task_id: str, records: list, board=None) -> list[str]:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect

    with kb.pin_first_board_resolution():
        conn = kanban_db_connect.connect(board=board)
        try:
            return receipt_delivery_paths(conn, task_id, records)
        finally:
            conn.close()
