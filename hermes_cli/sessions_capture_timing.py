"""Opt-in, payload-free observations of the existing sessions capture window."""
import itertools
import json
import logging
import os

logger = logging.getLogger(__name__)
_instances = itertools.count(1)


class CaptureTimingLog:
    """Best-effort logs; never configure handlers or decide capture policy.

    A missing header, sequence gap or filtered INFO output is unmeasured, not
    a duty PASS. The interval excludes watch checks, no-op reconciliation,
    publication and commit. It is not whole-thread CPU or wall duty.
    """

    def __init__(self):
        self.pid = os.getpid()
        self.instance = next(_instances)
        self.emit('sessions.capture.enabled')

    @classmethod
    def from_environment(cls):
        # health: allow HX002 -- process-owned diagnostic switch, not a product behavior or policy setting
        return cls() if os.environ.get('HERMES_SESSIONS_CAPTURE_TIMINGS') == '1' else None

    def emit(self, event, **fields):
        try:
            logger.info(json.dumps(dict(event=event, pid=self.pid, instance=self.instance, **fields),
                                   allow_nan=False, separators=(',', ':')))
        except Exception:  # health: allow BLE001 S110 -- logging failure cannot affect capture or recursively log itself
            pass
