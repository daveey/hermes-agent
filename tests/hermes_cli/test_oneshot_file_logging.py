"""``hermes -z`` keeps writing agent.log while keeping the console clean.

``run_oneshot`` used to call ``logging.disable(logging.CRITICAL)``. That short-circuits
``Logger.isEnabledFor`` for every logger, so the file handlers from ``setup_logging()`` never saw
a record either: one-shot turns left no ``conversation turn`` / ``API call #N`` / ``Turn ended``
line in agent.log, and provider audits that read it could not see them. Only console handlers
may be silenced; stdout stays exactly the reply and stderr carries no log text.
"""

import logging
from unittest import mock

import pytest

import hermes_logging
from hermes_cli import oneshot

_MARK = "oneshot-file-logging-probe API call #1: model=fake provider=fake"


@pytest.fixture
def file_logged_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    saved_disable = logging.root.manager.disable
    saved_last_resort = logging.lastResort
    hermes_logging._reset_queued_handlers()
    hermes_logging._logging_initialized = False
    logging.disable(logging.NOTSET)
    hermes_logging.setup_logging(hermes_home=home, force=True)
    try:
        yield home
    finally:
        hermes_logging._reset_queued_handlers()
        hermes_logging._logging_initialized = False
        for h in list(root.handlers):
            if h not in saved_handlers:
                root.removeHandler(h)
        root.setLevel(saved_level)
        logging.disable(saved_disable)
        logging.lastResort = saved_last_resort


def test_oneshot_logs_reach_agent_log_but_not_console(file_logged_home, capfd):
    # A console handler like the one ``--verbose`` / a library basicConfig would attach, bound to
    # the real stderr before run_oneshot's redirect takes effect.
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(console)
    module_logger = logging.getLogger("run_agent.oneshot_probe")

    def fake_agent(*_a, **_kw):
        module_logger.info(_MARK)
        return "the reply", {"final_response": "the reply", "completed": True, "failed": False}

    try:
        with mock.patch.object(oneshot, "_run_agent", side_effect=fake_agent):
            code = oneshot.run_oneshot("q")
        hermes_logging.flush_log_queue()
    finally:
        logging.getLogger().removeHandler(console)

    out, err = capfd.readouterr()
    assert code == 0
    assert out == "the reply\n"
    assert _MARK not in err and err == ""
    agent_log = (file_logged_home / "logs" / "agent.log").read_text(encoding="utf-8")
    assert _MARK in agent_log
