"""In-memory stand-in for the hermes-hub Board service (worker/src/rows.ts + board.ts), for the sync client tests.

Implements the contract the client relies on: row ops (per-field LWW by HLC on tasks, ``created_on`` set once,
``import`` refusing another gateway's id, a live lease fencing status moves), ``client_op_id`` idempotency, a dense
``seq`` feed of full rows, a retention floor (``410``), paged snapshots and leases. Several gateways share one board.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Any, Optional

from hermes_cli import kanban_sync_client as ksc

FENCED = {"status", "current_run_uid", "result", "started_at", "completed_at"}


class FakeService:
    def __init__(self, *, lease_runs: bool = False) -> None:
        self.boards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.offline = False
        # Like transitions.ts grantLease/releaseTask: a grant writes the card running plus its run row, a release
        # ends that run and returns the card to ready. Off by default (the older tests count rows).
        self.lease_runs = lease_runs

    def board(self, bid: str) -> dict[str, Any]:
        return self.boards.setdefault(bid, {"seq": 0, "floor": 0, "changes": [], "rows": {}, "applied": {}, "leases": {}})

    def hub(self, gateway: str) -> FakeHub:
        return FakeHub(self, gateway)

    # ---- server-side helpers ----
    def _emit(self, b: dict, table: str, uid: str, op: str, row: Optional[dict], gw: str, op_id: Optional[str]) -> int:
        b["seq"] += 1
        b["changes"].append({"seq": b["seq"], "table": table, "uid": uid, "op": op,
                             "fields": copy.deepcopy(row), "hlc": (row or {}).get("hlc", ""),
                             "origin_gateway": gw, "client_op_id": op_id})
        return b["seq"]

    def _put(self, b: dict, table: str, uid: str, patch: dict, gw: str, op_id: Optional[str], hlc: str) -> dict:
        rows = b["rows"].setdefault(table, {})
        cur = rows.get(uid)
        if cur is None:
            cur = {"uid": uid, "version": 0}
            rows[uid] = cur
        cur.update(patch)
        cur.update(version=cur["version"] + 1, hlc=hlc, origin_gateway=gw, deleted_at=None)
        cur["seq"] = self._emit(b, table, uid, "upsert", None, gw, op_id)
        b["changes"][-1]["fields"] = copy.deepcopy(cur)
        return cur

    def row_op(self, b: dict, op: dict, gw: str) -> dict:
        table, uid, kind = op["table"], op["uid"], op["op"]
        rows = b["rows"].setdefault(table, {})
        if kind == "delete":
            if uid not in rows or rows[uid].get("deleted_at"):
                return {"status": "merged"}
            rows[uid]["deleted_at"] = 1
            self._emit(b, table, uid, "delete", None, gw, op["client_op_id"])
            return {"status": "applied"}
        f = dict(op["fields"])
        if table == "tasks":
            cur = rows.get(uid)
            if cur is None:
                clocks = {k: op["hlc"] for k in f}
                return {"status": "applied", "row": self._put(b, table, uid, {"id": uid, **f, "created_on": gw,
                                                                                "field_hlc": clocks}, gw, op["client_op_id"], op["hlc"])}
            if op.get("import") and cur.get("created_on") not in (None, gw):
                return {"status": "rejected", "reason": "id_taken", "row": cur}
            lease = b["leases"].get(uid)
            if lease and lease["holder"] != gw and FENCED & set(f):
                return {"status": "rejected", "reason": "live_claim", "row": cur}
            clocks = cur.setdefault("field_hlc", {})
            won = {k: v for k, v in f.items() if k not in clocks or op["hlc"] > clocks[k]}
            if not won:
                return {"status": "merged", "row": cur}
            for k in won:
                clocks[k] = op["hlc"]
            row = self._put(b, table, uid, won, gw, op["client_op_id"], op["hlc"])
            return {"status": "applied" if len(won) == len(f) else "merged", "row": row}
        return {"status": "applied", "row": self._put(b, table, uid, f, gw, op["client_op_id"], op["hlc"])}

    def compact(self, bid: str) -> None:
        """Advance the retention floor to the head (every older cursor now gets 410)."""
        b = self.board(bid)
        b["floor"] = b["seq"]


class FakeHub:
    """Duck-typed :class:`ksc.Hub` for one gateway."""

    def __init__(self, svc: FakeService, gateway: str) -> None:
        self.svc, self.gateway = svc, gateway
        self.ident = ksc.Identity(url="https://hub.test", key="k", gateway=gateway)

    def _check(self) -> None:
        if self.svc.offline:
            raise OSError("hub unreachable")

    def ops(self, board: str, ops: list[dict]) -> dict:
        self._check()
        self.svc.calls.append(("ops", board, ops))
        b = self.svc.board(board)
        results = []
        for op in ops:
            prior = b["applied"].get(op["client_op_id"])
            if prior is not None:
                results.append({**prior, "dup": True})
                continue
            res = {"client_op_id": op["client_op_id"], **self.svc.row_op(b, op, self.gateway)}
            row = res.get("row")
            res["version"] = row.get("version") if row else None
            b["applied"][op["client_op_id"]] = res
            results.append(res)
        return {"results": results, "head": b["seq"]}

    def changes(self, board: str, since: int, limit: int = 500) -> dict:
        self._check()
        b = self.svc.board(board)
        if since < b["floor"]:
            raise ksc.Resync("cursor below the retention floor")
        page = [c for c in b["changes"] if c["seq"] > since][:limit]
        more = len([c for c in b["changes"] if c["seq"] > since]) > len(page)
        return {"changes": copy.deepcopy(page), "next": page[-1]["seq"] if page else since, "head": b["seq"], "more": more}

    def snapshot_page(self, board: str, table: str, after: str = "", limit: int = 1000) -> dict:
        self._check()
        b = self.svc.board(board)
        live = sorted((u, r) for u, r in b["rows"].get(table, {}).items() if not r.get("deleted_at") and u > after)
        page = live[:limit]
        return {"head": b["seq"], "table": table, "rows": [copy.deepcopy(r) for _, r in page],
                "next": page[-1][0] if page else after, "more": len(live) > limit}

    def leases(self, board: str, body: dict) -> dict:
        self._check()
        b = self.svc.board(board)
        task, run = body["task"], body["run_uid"]
        cur = b["leases"].get(task)
        op_id, hlc, gw = body.get("client_op_id"), f"{9_999_999_999_998:013d}-0000-board", self.gateway
        version = None
        if body["action"] == "grant":
            if cur and cur["holder"] != self.gateway:
                return {"status": "rejected", "reason": "lease_held", "lease": cur}
            b["leases"][task] = {"task_id": task, "holder": self.gateway, "run_uid": run, "expires": 9_999_999_999_999}
            if self.svc.lease_runs and task in b["rows"].get("tasks", {}):
                self.svc._put(b, "task_runs", run, {"task_id": task, "status": "running", "started_at": 1,
                                                    "ended_at": None, "outcome": None}, gw, op_id, hlc)
                version = self.svc._put(b, "tasks", task, {"status": "running", "current_run_uid": run,
                                                           "lease_holder": gw, "lease_expires": 9_999_999_999_999},
                                        gw, op_id, hlc)["version"]
                # As the Board's grantLease (hermes-hub worker/src/transitions.ts): the claimed event, tagged with
                # the lane the claim came from, so a reviewer run retries to ``review``.
                payload = {"holder": gw, "run_uid": run, "expires": 9_999_999_999_999,
                           **({"source_status": "review"} if body.get("lane") == "review" else {})}
                self.svc._put(b, "task_events", f"ev-{run}", {"task_id": task, "run_uid": run, "kind": "claimed",
                                                              "payload": json.dumps(payload),
                                                              "created_at": int(time.time())}, gw, op_id, hlc)
        elif body["action"] == "renew":
            if not cur or cur["holder"] != self.gateway or cur["run_uid"] != run:
                return {"status": "rejected", "reason": "lease_lost", "lease": cur}
        else:
            b["leases"].pop(task, None)
            runs = b["rows"].get("task_runs", {})
            if (self.svc.lease_runs and cur and cur["run_uid"] == run and run in runs
                    and runs[run].get("ended_at") is None):
                self.svc._put(b, "task_runs", run, {"status": "released", "outcome": "released", "ended_at": 2},
                              gw, op_id, hlc)
                self.svc._put(b, "tasks", task, {"status": "ready", "current_run_uid": None, "lease_holder": None,
                                                 "lease_expires": None}, gw, op_id, hlc)
            return {"status": "applied", "lease": None}
        return {"status": "applied", "lease": b["leases"][task], "version": version}
