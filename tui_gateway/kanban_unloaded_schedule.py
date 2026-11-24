"""Fact admission for unloaded work, separate from the install-wide lease."""
from gateway.kanban_notification_schedule import BoundActions, PassReport
from tui_gateway.kanban_unloaded_gates import scheduling
import time


class UnloadedSchedule:
    def __init__(self, wake, interval: float, *, native=False):
        self.gate = BoundActions(wake, native=native)
        self.interval = interval
        self.role = None
        self.next_legacy = 0.0

    def run(self, role, work, *args, **kwargs):
        now = time.monotonic()
        changed = role != self.role
        self.role = role
        # Read before due(): an empty membership resets retry_at.
        report_due = now >= self.gate.retry_at
        due = self.gate.due(now, legacy_due=now >= self.next_legacy)
        if not due and report_due and not self.gate.was_bound and not self.gate.actions:
            # Never-bound work is the legacy executor; its reported deadline is
            # due work. A detached binding still waits for a fresh membership.
            due = True
        # Ownership changes require catch-up, but a detached facts registry is
        # never permission to resume scans using the launch profile instead.
        native = self.gate.native_action
        ready = native is not None and native.owner.ready
        if not due and not (changed and (self.gate.actions or ready or self.gate.legacy)):
            return
        report = PassReport()
        try:
            with scheduling(report):
                work(*args, **kwargs)
        except Exception:  # health: allow BLE001 -- preserve the timer before the existing loop logs the failure
            report.retry()
            raise
        finally:
            now = time.monotonic()
            self.gate.settled(report, now, self.interval)
            self.next_legacy = now + self.interval

    def close(self):
        self.gate.close()
