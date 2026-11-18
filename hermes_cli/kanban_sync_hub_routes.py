"""Thin Board route wrappers of :class:`hermes_cli.kanban_sync_client.Hub`: leases, placement, machine registry.

Split out of ``kanban_sync_client`` to keep that module under the file-size cap. ``Hub`` inherits these unchanged and
supplies ``_call``; anything with its own error or budget handling (``sessions``, ``report_metrics``) stays there.
"""

from __future__ import annotations

from typing import Optional


class HubRoutesMixin:
    """One ``_call`` per endpoint; no state of its own."""

    def containment_capability(self, board: str) -> dict:
        from urllib.parse import quote

        return self._call(
            "GET", f"/v2/boards/{quote(board, safe='')}/containment/capability"
        )

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
