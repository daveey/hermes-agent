"""Thin Board route wrappers of :class:`hermes_cli.kanban_sync_client.Hub`: leases, placement, machine registry.

Split out of ``kanban_sync_client`` to keep that module under the file-size cap. ``Hub`` inherits these unchanged and
supplies ``_call``; anything with its own error or budget handling (``sessions``, ``report_metrics``) stays there.
"""

from __future__ import annotations

from typing import Optional


class HubRoutesMixin:
    """One ``_call`` per endpoint; no state of its own."""

    def status_origin_capability(self, board: str) -> dict:
        from urllib.parse import quote

        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/status-origin/capability")

    def actionability_capability(self, board: str) -> dict:
        from urllib.parse import quote

        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/actionability/capability")

    def decision_retain_capability(self, board: str) -> dict:
        from urllib.parse import quote

        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/decision-retain/capability")

    def owner_release_evidence(self, board: str, intent_uid: str) -> dict:
        """Private canonical certification, never inferred from ordinary event rows."""
        from urllib.parse import quote
        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/owner-release/" + quote(intent_uid, safe=''),
                          max_response_bytes=131072)

    def owner_release_comments(self, board: str, intent_uid: str) -> dict:
        """Complete current-valid private comment inventory, not display proof."""
        from urllib.parse import quote
        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/owner-release/" + quote(intent_uid, safe='') + "/comments",
                          max_response_bytes=2 * 1024 * 1024)

    def owner_release_consumption(self, board: str, intent_uid: str) -> dict:
        """Historical private winning consume certificate, not local release clearance."""
        from urllib.parse import quote
        from hermes_cli.kanban_release_certificate import MAX_BYTES
        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/owner-release/" + quote(intent_uid, safe='') + "/consumption",
                          max_response_bytes=MAX_BYTES)

    def owner_release_application(self, board: str, intent_uid: str) -> dict:
        """Historical final private certificate, never a current task postimage."""
        from urllib.parse import quote
        from hermes_cli.kanban_release_final import MAX_BYTES
        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/owner-release/" + quote(intent_uid, safe='') + "/application",
                          max_response_bytes=MAX_BYTES)

    def scope_delegates(self, board: str) -> dict:
        """Delegated scope-renewal grants (``kanban_scope_delegate``); ask only after ``scope_delegate: 1``."""
        from urllib.parse import quote

        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/scope-delegates")

    def containment_capability(self, board: str) -> dict:
        from urllib.parse import quote

        return self._call(
            "GET", f"/v2/boards/{quote(board, safe='')}/containment/capability"
        )

    def reviewer_allowance(self, board: str, task: str) -> dict:
        """SELECT-only diagnostic, with a small hard response bound; never a lease grant."""
        from urllib.parse import quote, urlencode
        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/reviewer-allowance?"
                          + urlencode({"task": task}), max_response_bytes=8192)

    def home_transfer_capability(self, board: str) -> dict:
        """Read-only protocol preflight; absence never authorizes local preparation."""
        from urllib.parse import quote

        return self._call("GET", f"/v2/boards/{quote(board, safe='')}/home-transfer/capability",
                          max_response_bytes=8192)

    def home_transfer(self, board: str, body: dict) -> dict:
        """Cooperative handoff; exact tuple/phase retry, never legacy rehome fallback."""
        from urllib.parse import quote

        return self._call("POST", f"/v2/boards/{quote(board, safe='')}/home-transfer", body, replay_safe=True)


    def leases(self, board: str, body: dict) -> dict:
        return self._call("POST", f"/v2/boards/{board}/leases", body)

    def repin(
        self, board: str, *, task_id: str, expected_revision: int, runs_on: list[str]
    ) -> dict:
        """Explicit rollback/manual pin; caller must pull the authoritative row afterward."""
        return self._call(
            "POST",
            f"/v2/boards/{board}/placement/repin",
            {
                "task_id": task_id,
                "expected_revision": expected_revision,
                "runs_on": runs_on,
            },
        )

    def recover(
        self,
        board: str,
        *,
        task_id: str,
        expected_revision: int,
        mode: str,
        candidates: list[str],
    ) -> dict:
        """Creator/CAS recovery; pull authoritative state after success or uncertain delivery."""
        return self._call(
            "POST",
            f"/v2/boards/{board}/placement/recover",
            {
                "task_id": task_id,
                "expected_revision": expected_revision,
                "mode": mode,
                "policy": "worker-first-v1",
                "candidates": candidates,
            },
        )

    def reserve(
        self, board: str, *, task_id: str, expected_revision: int, candidates: list[str]
    ) -> dict:
        """Atomic Board placement; errors leave the persisted local queue fence intact."""
        return self._call(
            "POST",
            f"/v2/boards/{board}/placement/reserve",
            {
                "task_id": task_id,
                "expected_revision": expected_revision,
                "policy": "worker-first-v1",
                "candidates": candidates,
            },
            replay_safe=True,
        )

    def machines(self) -> dict:
        """The machine registry (RFC "One machine registry"): ``{me, machines: [{name, labels, pinned_only, ...}]}``."""
        return self._call("GET", "/v2/machines")

    def report_pickup(self, pickup: Optional[str]) -> dict:
        """Publish this machine's ``kanban.sync.pickup`` (None → null) on its own registry row."""
        return self._call("POST", "/v2/machines/me", {"pickup": pickup})

    def attest_sources(self, body: dict) -> dict:
        """workspace_source v1 clone attestation: ``{version: 1, repos: [{id, full_name}]}`` (404 on an old Board)."""
        return self._call("POST", "/v2/machines/me/sources", body)
