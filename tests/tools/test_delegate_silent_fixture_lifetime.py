"""Silent-resume scenarios must finish their extra, non-silent background children before returning."""
import threading
from unittest.mock import patch

import pytest

from tests.tools import test_delegate_auto_resume_silent as scenarios
from tests.tools.test_delegate_continue import _ScriptedModel
from tools import async_delegation


@pytest.mark.parametrize("scenario", [
    scenarios.test_a_spawn_with_a_waiting_childs_exact_goal_is_refused_until_it_is_stopped,
    scenarios.test_the_duplicate_guard_is_off_without_auto_resume,
    scenarios.test_turning_auto_resume_off_still_reports_a_promised_child_once_and_the_guard_follows_its_deadline,
])
def test_silent_scenario_owns_its_extra_child_until_completion(tmp_path, scenario):
    """Hold the extra child at its model boundary until its parent explicitly awaits completion.

    Without that wait the scenario returns with a live writer, allowing the next test's
    HERMES_HOME to become the destination of its late async-ledger writes.
    """
    release = threading.Event()
    entered = threading.Event()
    finished = threading.Event()
    awaited = []
    real_wait = scenarios._until_completion

    class HeldModel(_ScriptedModel):
        def __call__(self, **kwargs):
            child = super().__call__(**kwargs)
            run = child.run_conversation.side_effect

            def held(*args, **kw):
                entered.set()
                assert release.wait(10), "scenario never released its extra child"
                result = run(*args, **kw)
                finished.set()
                return result

            child.run_conversation.side_effect = held
            return child

    def await_extra(unit):
        assert entered.wait(10), "extra child never reached its model"
        awaited.append(unit)
        release.set()
        return real_wait(unit)

    try:
        with patch("tests.tools.test_delegate_continue._ScriptedModel", HeldModel), \
                patch.object(scenarios, "_ScriptedModel", HeldModel), \
                patch.object(scenarios, "_until_completion", await_extra):
            scenario(tmp_path)
            assert awaited and finished.is_set(), "scenario returned with an unawaited background child"
    finally:
        release.set()
        # Both RED and GREEN reap owned work while the fixture's home still exists.
        if async_delegation._executor is not None:
            async_delegation._executor.shutdown(wait=True)
            async_delegation._executor = None
