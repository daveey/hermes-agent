"""[daveey] Errors logged on the API worker thread carry the session tag (self-heal agent-1572c64e24).

The session id that prefixes log lines is thread-local; the streaming/API worker runs on its own
thread, so "Streaming failed before delivery" reached errors.log without ``[<sid>]`` and the errors
signal (which reads the tag) filed the defect with an empty session id.
"""

import logging
import threading


def test_worker_thread_logs_carry_the_callers_session_tag():
    from agent.chat_completion_helpers import _context_thread_target
    from hermes_logging import _install_session_record_factory, clear_session_context, set_session_context

    _install_session_record_factory()
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("agent.chat_completion_helpers.test_session_tag")
    handler = _Capture()
    log.addHandler(handler)
    log.setLevel(logging.ERROR)
    try:
        set_session_context("20261027_220000_5326d3")
        worker = threading.Thread(
            target=_context_thread_target(lambda: log.error("Streaming failed before delivery: boom")))
        worker.start()
        worker.join(5)
    finally:
        clear_session_context()
        log.removeHandler(handler)
    assert [r.session_tag for r in records] == [" [20261027_220000_5326d3]"]


def test_worker_thread_without_a_session_stays_untagged():
    from agent.chat_completion_helpers import _context_thread_target
    from hermes_logging import _install_session_record_factory, clear_session_context

    _install_session_record_factory()
    clear_session_context()
    tags = []
    worker = threading.Thread(target=_context_thread_target(
        lambda: tags.append(logging.getLogRecordFactory()("x", logging.ERROR, "", 0, "m", (), None).session_tag)))
    worker.start()
    worker.join(5)
    assert tags == [""]
