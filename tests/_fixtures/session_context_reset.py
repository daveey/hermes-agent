"""Autouse reset of ``gateway.session_context`` state before every test.

Imported into ``tests/conftest.py`` so pytest registers the fixture there (see live_system_guard.py
for why ``pytest_plugins`` is not used).

The canonical runner gives each test file its own process, but a multi-file invocation
(``pytest a.py b.py``, a sweep of related files) shares one interpreter. ``clear_session_vars``
leaves every session ContextVar at ``""``, which masks ``os.environ`` in ``get_session_env``, and
``set_session_vars`` latches the process-wide ``_session_context_engaged`` flag. A test in one file
that bound and cleared a session therefore made later files ignore their monkeypatched
HERMES_SESSION_ID / HERMES_CRON_SESSION (test_kanban_provenance before test_cronjob_tools,
test_kanban_tools and test_completed_process_results). Resetting to "never bound here" makes any
file combination behave like a fresh process. A near no-op while the module is not imported.
"""
import sys
from typing import Any

import pytest

_SESSION_CONTEXT_MODULE = "gateway.session_context"


@pytest.fixture(autouse=True)
def _reset_session_context_vars():
    mod: Any = sys.modules.get(_SESSION_CONTEXT_MODULE)
    if mod is not None:
        mod.reset_session_vars()
        mod._session_context_engaged = False
    yield
