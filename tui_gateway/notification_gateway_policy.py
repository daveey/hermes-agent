"""One native control lease for live f2 rollback; never a consumer-side config read."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class Policy:
    def __init__(self, event):
        self.event = event
        self.lock = threading.Lock()
        self.dirty = True
        self.enabled = True  # pending admission waits for step() to validate the policy
        self.watch = None
        self.retry_at = float('inf')

    def changed(self):
        with self.lock:
            self.dirty = True
        self.event.set()

    def step(self, now, *, reconcile):
        with self.lock:
            dirty, self.dirty = self.dirty, False
        if not (dirty or reconcile or now >= self.retry_at):
            return False
        previous = self.enabled
        try:
            from hermes_cli.config import FailedConfigRead, get_config_path, load_config_readonly
            from hermes_cli.fact_source_watch import NativeSourceWatch
            if self.watch is None:
                try:
                    self.watch = NativeSourceWatch((get_config_path(),), self.changed)
                except NotImplementedError:
                    pass  # unsupported platforms sample policy on the existing census
            if self.watch is not None:
                self.watch.check()
            config = load_config_readonly()
            if isinstance(config, FailedConfigRead):
                raise ValueError('configuration read failed') from config.read_error
            self.enabled = config.get('display', {}).get('gateway_notification_f', False) is True
            self.retry_at = float('inf')
        except (OSError, ValueError, RuntimeError) as exc:
            # Unknown policy cannot authorize native work. Legacy custody remains.
            self.enabled = False
            self.retry_at = now + 1.0
            log.warning('gateway notification policy unavailable; legacy recovery: %s', exc)
        return previous != self.enabled

    def close(self):
        if self.watch is not None:
            self.watch.close()
            self.watch = None
        self.dirty = True
