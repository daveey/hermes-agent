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
    def __init__(self, *, lease_runs: bool = False, machines: Optional[dict] = None) -> None:
        self.boards: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.offline = False
        # Like transitions.ts grantLease/releaseTask: a grant writes the card running plus its run row, a release
        # ends that run and returns the card to ready. Off by default (the older tests count rows).
        self.lease_runs = lease_runs
        # Opt-in placement, like transitions.ts checkPlacement's runs_on rule: a grant needs every runs_on label
        # on the claiming machine ({name: {"labels": [...]}}; machine:<name> is implicit). None = no check.
        self.machines = machines
        # Session owners (worker/src/board.ts ``sessions``): {board: {session_id: owner}} plus the move audit.
        self.session_owners: dict[str, dict[str, dict]] = {}
        self.session_audit: list[dict] = []

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
            # As rows.ts: the holder moving its card out of `running` ends the lease (opt-in with lease_runs).
            if self.lease_runs and lease and lease["holder"] == gw and "status" in won and won["status"] != "running":
                b["leases"].pop(uid, None)  # clearLease: the lease row and the task's lease mirror
                # endLeaseRun: a run the holder has not ended yet ends with a placeholder (the holder's own end
                # "normally follows in the same batch and overwrites this generic one").
                run = b["rows"].get("task_runs", {}).get(lease["run_uid"])
                if run is not None and run.get("ended_at") is None:
                    self._put(b, "task_runs", lease["run_uid"],
                              {"status": "released", "outcome": "released",
                               "error": f"card left running ({won['status']})", "ended_at": int(time.time())},
                              gw, op["client_op_id"], op["hlc"])
                row = self._put(b, table, uid, {"lease_holder": None, "lease_expires": None}, gw, op["client_op_id"],
                                op["hlc"])
            return {"status": "applied" if len(won) == len(f) else "merged", "row": row}
        if table == "task_links":
            # rows.ts upsertLink: a link needs both of its cards on the Board.
            tasks = b["rows"].get("tasks", {})
            missing = [x for x in (f.get("parent_id"), f.get("child_id"))
                       if x not in tasks or tasks[x].get("deleted_at")]
            if missing:
                return {"status": "rejected", "reason": "not_found", "detail": f"unknown task(s): {', '.join(map(str, missing))}"}
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

    def row_evidence(self, board: str, table: str, uid: str) -> dict:
        self._check()
        b = self.svc.board(board)
        row = copy.deepcopy(b["rows"].get(table, {}).get(uid))
        state = ("deleted" if row.get("deleted_at") is not None else "live") if row else (
            "unknown" if b["floor"] else "never_existed")
        return {"table": table, "uid": uid, "state": state, "row": row,
                "tombstone": None, "floor": b["floor"], "head": b["seq"]}

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
            card = b["rows"].get("tasks", {}).get(task) or {}
            if self.svc.machines is not None and card.get("runs_on"):
                want = json.loads(card["runs_on"]) if isinstance(card["runs_on"], str) else card["runs_on"]
                mine = set((self.svc.machines.get(self.gateway) or {}).get("labels") or []) | {f"machine:{self.gateway}"}
                if set(want or []) - mine:
                    return {"status": "rejected", "reason": "labels_mismatch", "lease": cur}
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
                unused = bool(body.get("unused_grant"))
                # As the Board's releaseTask with ``unused_grant: true`` (live t_01a1a1dea9c64d6a run 164239): the
                # run's metadata and the ``released`` event both say the grant never started anything.
                self.svc._put(b, "task_runs", run, {"status": "released", "outcome": "released", "ended_at": 2,
                                                    **({"metadata": json.dumps({"unused_grant": True},
                                                                               separators=(",", ":"))}
                                                       if unused else {})},
                              gw, op_id, hlc)
                if unused:
                    self.svc._put(b, "task_events", f"ev-rel-{run}",
                                  {"task_id": task, "run_uid": run, "kind": "released",
                                   "payload": json.dumps({"error": None, "retry_status": "ready",
                                                          "unused_grant": True}, separators=(",", ":")),
                                   "created_at": int(time.time())}, gw, op_id, hlc)
                self.svc._put(b, "tasks", task, {"status": "ready", "current_run_uid": None, "lease_holder": None,
                                                 "lease_expires": None}, gw, op_id, hlc)
            return {"status": "applied", "lease": None}
        return {"status": "applied", "lease": b["leases"][task], "version": version}

    def sessions(self, board: str, body: dict) -> dict:
        """As board.ts ``sessions``: ``get`` -> {owners}; ``move`` is a CAS signed by the current owner. The target
        must be an active, unheld registry machine (with ``machines`` None, any name) other than the caller.
        A refusal is the 409 body ``{reason, owner}`` (what :meth:`ksc.Hub.sessions` returns for a 409)."""
        self._check()
        self.svc.calls.append(("sessions", board, body))
        owners = self.svc.session_owners.setdefault(board, {})
        if body["action"] == "get":
            ids = body["session_ids"]
            assert isinstance(ids, list) and len(ids) <= 200
            return {"owners": {sid: copy.deepcopy(owners[sid]) for sid in ids if sid in owners}}
        sid, to, expect = body["session_id"], body["to"], body["expect_epoch"]
        cur = copy.deepcopy(owners.get(sid))

        def refuse(reason: str) -> dict:
            return {"reason": reason, "owner": cur}

        if cur and cur["gateway"] != self.gateway:
            return refuse("not_owner")
        if (cur["epoch"] if cur else 0) != expect:
            return refuse("epoch_mismatch")
        if to == self.gateway:
            return refuse("same_gateway")
        if self.svc.machines is not None:
            m = self.svc.machines.get(to)
            if not m or m.get("state", "active") != "active" or m.get("hold"):
                return refuse("unknown_target")
        elif not to:
            return refuse("unknown_target")
        owners[sid] = {"gateway": to, "epoch": expect + 1, "moved_at": int(time.time() * 1000),
                       "moved_by": self.gateway}
        self.svc.session_audit.append({"session_id": sid, "actor": self.gateway,
                                       "from_gateway": cur["gateway"] if cur else None, "to_gateway": to,
                                       "epoch": expect + 1, "reason": body.get("reason")})
        return {"status": "moved", "owner": copy.deepcopy(owners[sid])}
