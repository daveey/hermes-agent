"""Fact admission for unloaded work, separate from the install-wide lease."""
from gateway.kanban_notification_schedule import BoundActions, PassReport
from tui_gateway.kanban_unloaded_gates import scheduling
import time


class UnloadedSchedule:
    def __init__(self, wake, interval: float):
        self.gate = BoundActions(wake)
        self.interval = interval
        self.role = None
        self.next_legacy = 0.0

    def run(self, role, work, *args, **kwargs):
        now = time.monotonic()
        changed = role != self.role
        self.role = role
        due = self.gate.due(now, legacy_due=now >= self.next_legacy)
        # Ownership changes require catch-up, but a detached facts registry is
        # never permission to resume scans using the launch profile instead.
        if not due and not (changed and (self.gate.actions or not self.gate.was_bound)):
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
