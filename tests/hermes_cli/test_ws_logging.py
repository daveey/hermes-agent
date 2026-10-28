"""hermes_cli.ws_logging: the one handled websockets failure becomes an INFO line; everything else keeps its level."""

from __future__ import annotations

import logging

from hermes_cli.ws_logging import ws_logger


def _raise_and_log(log, msg):
    try:
        raise TimeoutError("timed out while closing connection")
    except TimeoutError:
        log.error(msg, exc_info=True)


def test_keepalive_failure_is_one_info_line_naming_the_cause(caplog):
    caplog.set_level(logging.DEBUG, logger="websockets.client")
    _raise_and_log(ws_logger(), "keepalive ping failed")
    [rec] = caplog.records
    assert rec.name == "websockets.client" and rec.levelno == logging.INFO and not rec.exc_info
    assert "keepalive ping failed (TimeoutError: timed out while closing connection)" in rec.getMessage()


def test_other_errors_keep_their_level_and_traceback(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="websockets")  # hermes_logging pins it at WARNING
    _raise_and_log(ws_logger("server"), "unexpected internal error")
    log = ws_logger()
    log.warning("keepalive ping failed")  # below ERROR: untouched
    log.debug("% sent keepalive ping")
    recs = [(r.name, r.levelno, bool(r.exc_info)) for r in caplog.records]
    assert recs == [("websockets.server", logging.ERROR, True), ("websockets.client", logging.WARNING, False),
                    ("websockets.client", logging.DEBUG, False)]


def test_level_config_of_the_library_logger_still_applies(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    try:
        log = ws_logger()
        assert not log.isEnabledFor(logging.INFO)  # websockets reads this to decide on debug output
        _raise_and_log(log, "keepalive ping failed")
        assert caplog.records == []  # demoted to INFO, below the library logger's WARNING floor
    finally:
        logging.getLogger("websockets.client").setLevel(logging.NOTSET)
