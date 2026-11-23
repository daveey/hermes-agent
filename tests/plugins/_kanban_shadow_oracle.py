"""``/board``: a shadow board's replica cards, merged read-only under the live ones. [daveey]

``board_read.project`` renders the live file's rows. A board synced in ``shadow`` mode keeps the other gateways'
cards (the mini's Baby Bear queue) only in the replica, so this adds them to the projected payload: opened
read-only (``hermes_cli.kanban_shadow_view``), a live card wins on an id collision, each card is stamped
``remote`` / ``read_only`` / ``home_gateway``. Writes on those cards are already refused as ``foreign_card`` (409).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from typing import Callable

from hermes_cli import kanban_shadow_view as shadow_view
from plugins.kanban.dashboard import board_read

_LOCK = threading.Lock()
_MEMO: dict[tuple, tuple[tuple, dict]] = {}
_MEMO_MAX = 16


def _signature(path: str) -> tuple:
    """Data files change on commits; shm read marks also change on reads, so exclude them."""
    sig = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(path + suffix)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _remote_rows(key: tuple, replica_path: str, build: Callable[[], dict]) -> dict:
    sig = _signature(replica_path)
    with _LOCK:
        hit = _MEMO.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    rows = build()
    with _LOCK:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.pop(next(iter(_MEMO)))
        _MEMO[key] = (sig, rows)
    return rows


def clear() -> None:
    with _LOCK:
        _MEMO.clear()


def for_board(payload: dict, connect: Callable, query: tuple, diagnostic_inputs: Callable,
              diagnostics_from_inputs: Callable) -> dict:
    """``merge`` on a fresh live connection; ``query`` = (tenant, include_archived, workflow_template_id,
    current_step_key, done_limit). A live-file failure leaves the payload as it was."""
    try:
        with closing(connect()) as live:
            return merge(payload, live, *query, diagnostic_inputs=diagnostic_inputs,
                         diagnostics_from_inputs=diagnostics_from_inputs)
    except sqlite3.Error:
        return payload


def merge(payload: dict, live, tenant, include_archived, workflow_template_id, current_step_key, done_limit, *,
          diagnostic_inputs: Callable, diagnostics_from_inputs: Callable) -> dict:
    """``payload`` (``board_read.project`` of the live rows) plus the replica's cards the live file lacks."""
    with shadow_view.open_shadow_replica(live) as replica:
        if replica is None:
            return payload
        live_file = shadow_view.db_file(live)
        replica_file = shadow_view.db_file(replica)
        key = (live_file, tenant, bool(include_archived), workflow_template_id, current_step_key, int(done_limit or 0))
        try:
            def build():
                # Filter collisions before the final cap: otherwise a live archived/done row
                # can consume a replica slot and hide the next remote card in the window.
                result = board_read.materialize(
                    replica, tenant, include_archived, workflow_template_id, current_step_key, 0,
                    diagnostic_inputs=diagnostic_inputs)
                result["homes"] = shadow_view.homes_of(replica, [t.id for t, _f, _x in result["cards"]])
                return result

            rows = _remote_rows(key, replica_file, build)
        except Exception:  # health: allow BLE001 -- an unreadable replica must not take the live board down with it
            shadow_view._log.warning("kanban: shadow replica board read failed", exc_info=True)
            return payload
        homes = rows["homes"]
    live_ids = {t["id"] for col in payload["columns"] for t in col["tasks"]}
    live_ids |= shadow_view._live_ids(live, [i for i in homes if i not in live_ids])
    remote_ids = set(homes) - live_ids
    cards = tuple(c for c in rows["cards"] if c[0].id in remote_ids)
    done_remote = sum(1 for t, _f, _x in cards if t.status == "done")
    remote = board_read.project({**rows, "cards": cards, "done_total": done_remote}, include_archived,
                                diagnostics_from_inputs)
    by_name = {c["name"]: c for c in remote["columns"]}
    for col in payload["columns"]:
        extra = by_name.get(col["name"])
        if extra is None:
            continue
        col["tasks"] = col["tasks"] + [shadow_view.mark(t, homes.get(t["id"])) for t in extra["tasks"]]
        if col["name"] == "done":
            col["tasks"].sort(key=lambda d: d["id"], reverse=True)
            col["tasks"].sort(key=lambda d: (d["completed_at"] is None, -(d["completed_at"] or 0)))
            total = col.get("total", 0) + done_remote
            if int(done_limit or 0) > 0:
                col["tasks"] = col["tasks"][:int(done_limit)]
            col["total"] = total
            col["omitted"] = total - len(col["tasks"])
    payload["tenants"] = sorted(set(payload["tenants"]) | set(remote["tenants"]))
    payload["assignees"] = sorted(set(payload["assignees"]) | set(remote["assignees"]))
    return payload
