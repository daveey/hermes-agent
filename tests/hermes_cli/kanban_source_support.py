"""Shared doubles for workspace_source core PR-2 tests: a Board that answers ``/leases`` source actions the way
hermes-hub ``worker/src/workspace_source.ts`` does (custody per card, revision CAS, receipt application)."""

from __future__ import annotations

import time
from typing import Any, Optional

from hermes_cli import kanban_db_sync as sync


class FakeSourceHub:
    """``hub.leases(board, payload)``: grant/renew/release/source_attach/publish_source/source_release."""

    def __init__(self, board: FakeSourceBoard) -> None:
        self.board = board

    def leases(self, board_id: str, payload: dict) -> dict:
        return self.board.answer(dict(payload))


class FakeSourceBoard:
    """A lease client (``sync.set_lease_client``) plus the source actions. ``refuse[action] = reason`` makes that
    action answer ``{"status": "rejected", "reason": reason}``; ``offline`` makes it raise like a dead Board;
    ``old`` makes every source action answer like an old Board (``400 action must be one of``)."""

    def __init__(self) -> None:
        self.hub = FakeSourceHub(self)
        self.calls: list[tuple[str, dict]] = []
        self.refuse: dict[str, str] = {}
        self.offline: set[str] = set()
        self.old = False
        self.manifest: dict[str, dict] = {}
        self.custody: dict[str, dict] = {}
        self.renew_mode = "grant"
        self.lease_seconds = 900

    # --- lease client ---------------------------------------------------------------------------------------------
    def _lease(self, kind: str, kw: dict) -> sync.Lease:
        self.calls.append((kind, kw))
        if kind == "renew" and self.renew_mode == "deny":
            return sync.Lease(granted=False, run_uid=kw["run_uid"], reason="lease_lost")
        return sync.Lease(granted=True, run_uid=kw["run_uid"], holder=kw["gateway_id"],
                          expires_ms=int((time.time() + self.lease_seconds) * 1000))

    def acquire(self, **kw) -> sync.Lease:
        return self._lease("acquire", kw)

    def renew(self, **kw) -> sync.Lease:
        return self._lease("renew", kw)

    def release(self, **kw) -> None:
        self.calls.append(("release_lease", kw))

    # --- /leases source actions --------------------------------------------------------------------------------------
    def answer(self, body: dict) -> dict:
        from hermes_cli.kanban_sync_client import HubError
        action = body["action"]
        self.calls.append((action, body))
        if action in self.offline:
            raise OSError("connection refused")
        if self.old and action in ("source_attach", "publish_source", "source_release"):
            raise HubError(400, "action must be one of grant, renew, release")
        if action in self.refuse:
            return {"status": "rejected", "reason": self.refuse[action], "detail": "refused by the fake Board"}
        task = body["task"]
        if action == "source_attach":
            cur = self.custody.get(task) or {}
            self.custody[task] = {**cur, "state": "attached", "attached_run_uid": body["run_uid"],
                                  "source_holder": cur.get("source_holder") or "here",
                                  "manifest_revision": body.get("manifest_revision")}
            return {"status": "applied"}
        if action == "publish_source":
            cur = (self.manifest.get(task) or {}).get("manifest_revision", 0)
            if body["expected_revision"] != cur:
                return {"status": "rejected", "reason": "source_revision_conflict", "detail": f"current {cur}"}
            self.manifest[task] = body["manifest"]
            self.custody[task] = {**self.custody.get(task, {}), "manifest_revision": body["manifest"]["manifest_revision"]}
            return {"status": "applied", "manifest": body["manifest"]}
        if action == "release":
            receipt = body.get("source_receipt")
            if receipt and task in self.custody:
                self.custody[task] = {**self.custody[task], "state": receipt["state"], "attached_run_uid": None}
            return {"status": "applied"}
        if action == "source_release":
            self.custody[task] = {**self.custody.get(task, {}), "state": body["to_state"]}
            return {"status": "applied"}
        return {"status": "applied"}

    def actions(self, *names: str) -> list[str]:
        return [k for k, _ in self.calls if not names or k in names]

    def body(self, action: str) -> Optional[dict]:
        for k, b in reversed(self.calls):
            if k == action:
                return b
        return None


def install(board: FakeSourceBoard) -> Any:
    from hermes_cli import kanban_source_board as kbs
    kbs.reset_board_lacks_source()
    return sync.set_lease_client(board)
