"""The test environment must tolerate logger registration during capture setup.

pytest 9.1's live registry scan can corrupt teardown for unrelated threaded
cron tests; see https://github.com/pytest-dev/pytest/issues/15237.
"""

import logging
import threading

from _pytest.logging import LogCaptureHandler, catching_logs


def test_capture_setup_tolerates_concurrent_logger_registration(monkeypatch):
    # Use a private registry: the trigger must not affect pytest's outer capture.
    manager = logging.Manager(logging.root)
    monkeypatch.setattr(logging.Logger, "manager", manager)
    register = threading.Event()
    registered = threading.Event()

    def register_logger():
        if register.wait(timeout=30):
            logging.getLogger("capture.concurrent-registration")
            registered.set()

    worker = threading.Thread(target=register_logger)

    class TriggerLogger(logging.Logger):
        @property
        def propagate(self):
            # If capture scans loggers, force registration between iterator steps.
            # Root-only capture need not inspect this logger at all.
            register.set()
            assert registered.wait(timeout=30)
            return True

        @propagate.setter
        def propagate(self, value):
            pass

    manager.loggerDict["capture.trigger"] = TriggerLogger("capture.trigger")
    handler = LogCaptureHandler()
    capture = catching_logs(handler)
    worker.start()
    try:
        with capture:
            register.set()
            assert registered.wait(timeout=30)
            logging.getLogger("capture.concurrent-registration").warning("captured")
        assert [record.getMessage() for record in handler.records] == ["captured"]
        assert handler not in logging.root.handlers
    finally:
        register.set()
        worker.join(timeout=30)
        assert not worker.is_alive()
        # Failed __enter__ in the regressed dependency leaves a root handler behind.
        logging.root.removeHandler(handler)
