"""The ``Event`` row dataclass; ``kanban_db`` re-exports it as ``kb.Event``."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None
    uid: Optional[str] = None  # stable cross-replica ID; None on pre-UID schemas

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        from hermes_cli.kanban_db import _json_or, _lossy_text, _opt_int, _row_get, _to_epoch

        return cls(
            id=row["id"], task_id=row["task_id"], kind=_lossy_text(row["kind"]),
            payload=_json_or(_lossy_text(row["payload"])), created_at=_to_epoch(row["created_at"]) or 0,
            run_id=_opt_int(_row_get(row, "run_id")), uid=_row_get(row, "uid"),
        )
