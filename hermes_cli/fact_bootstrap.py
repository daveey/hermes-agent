"""Source-owned task bootstrap pages; never expose a partial generation. [daveey]"""
from dataclasses import replace

from hermes_cli.fact_capture import (
    CaptureBudgetExceeded, CapturePending, CaptureRefused, capture_tasks, payload_bytes,
)


class PagedBootstrap:
    """Stage keyset pages then indexed closed dirty windows before publication.

    Failed reads retain safe staged pages. Lost coverage resets only staging.
    Completed captures remain cached for a failed publication. The candidate
    cap latches an explicit error until source revision or capacity changes,
    avoiding repeated full rescans for unchanged oversized input.
    Scalar byte bounds are not a Python RSS guarantee.
    """
    def __init__(self, path, hydrate, *, capture=capture_tasks, max_candidate_bytes=64 * 1024 * 1024):
        self.path, self.hydrate = path, hydrate
        self._capture = capture
        self.max_candidate_bytes = max_candidate_bytes
        self.reset()

    def reset(self):
        self.anchor = None
        self.before = None
        self.last_id = ""
        self.rows = {}
        self.sizes = {}
        self.size = 0
        self.replay = False
        self.ready = None
        self.fence = None
        self.position = None
        self.page_keys = None
        self.fast_pages = 0
        self.error = None
        self.capped_at = None
        self.capped_limit = None

    def _apply(self, capture):
        updates = {row["id"]: row for row in capture.records}
        if len(updates) != len(capture.records) or not updates.keys() <= set(capture.ids):
            raise CaptureRefused("hydrator returned duplicate or unrequested tasks")
        sizes = {key: payload_bytes(key) + payload_bytes(row) for key, row in updates.items()}
        size = self.size - sum(self.sizes.get(key, 0) for key in capture.ids) + sum(sizes.values())
        if size > self.max_candidate_bytes:
            self.reset()
            self.error = "bootstrap candidate byte budget exceeded"
            self.capped_at = capture.after
            self.capped_limit = self.max_candidate_bytes
            raise CaptureRefused(self.error)
        for key in capture.ids:
            self.rows.pop(key, None)
            self.sizes.pop(key, None)
        self.rows.update(updates)
        self.sizes.update(sizes)
        self.size = size

    def _step(self, budget):
        if not self.replay:
            result = self._capture(self.path, self.anchor, self.hydrate,
                                   budget=budget, page_after=self.last_id)
            self._observe_page(result, budget)
            if self.anchor is None:
                self.anchor = result.after
            self._apply(result)
            if result.ids:
                self.last_id = result.ids[-1]
            self.replay = len(result.ids) < budget.max_keys
            return None
        result = self._capture(self.path, self.anchor, self.hydrate, budget=budget,
                               replay=True, replay_fence=self.fence, replay_position=self.position)
        self._observe_page(result, budget)
        self._apply(result)
        self.fence, self.position = result.replay_fence, result.replay_position
        if not result.replay_done:
            return None
        if result.after != self.fence:
            # Writes after the fence may have moved marks out of this window.
            # Replay those before claiming the staged generation is coherent.
            self.anchor, self.fence, self.position = self.fence, None, None
            return None
        keys = tuple(sorted(self.rows))
        self.ready = replace(result, before=self.before, full=True, ids=keys,
                             records=tuple(self.rows[key] for key in keys), payload_bytes=self.size)
        return self.ready

    def _observe_page(self, result, budget):
        if (result.transaction_seconds < budget.max_seconds / 4
                and result.payload_bytes < budget.max_bytes / 4):
            self.fast_pages += 1
            if self.fast_pages >= 4:
                self.page_keys = budget.max_keys * 2
                self.fast_pages = 0
        else:
            self.fast_pages = 0

    def capture(self, cursor, budget):
        if self.error:
            # Failure retries probe one bounded revision window, never hydrate
            # or rescan the candidate. Real source changes can recover quietly.
            try:
                probe = self._capture(self.path, self.capped_at, lambda conn, ids: (),
                                      budget=replace(budget, max_keys=1), replay=True)
            except CaptureBudgetExceeded:
                raise
            except CaptureRefused:
                self.reset()
                raise
            if probe.after == self.capped_at and self.capped_limit == self.max_candidate_bytes:
                raise CaptureRefused(self.error)
            self.reset()
        if self.ready is not None:
            if cursor == self.before:
                return self.ready
            if cursor != self.ready.after:
                raise CaptureRefused("unexpected bootstrap caller cursor")
            self.reset()
        if self.anchor is not None and cursor != self.before:
            raise CaptureRefused("unexpected bootstrap caller cursor")
        if self.anchor is None:
            self.before = cursor
        page_budget = replace(budget, max_keys=min(budget.max_keys, self.page_keys or budget.max_keys))
        try:
            result = self._step(page_budget)
        except CaptureBudgetExceeded:
            if page_budget.max_keys == 1:
                raise
            self.fast_pages = 0
            self.page_keys = max(1, page_budget.max_keys // 2)
            raise CapturePending("retry smaller task bootstrap page") from None
        except CaptureRefused:
            if not self.error:
                self.reset()
            raise
        if result is not None:
            return result
        raise CapturePending("task bootstrap page captured")
