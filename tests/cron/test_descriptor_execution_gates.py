"""Descriptor fixtures retain scanner and portable execution on old Bash."""
import pytest

from tests.cron import bash_execution
from tests.cron import test_lifecycle_guard_descriptor_async as async_cases
from tests.cron import test_lifecycle_guard_descriptor_keywords as keyword_cases


@pytest.mark.parametrize("major", [3, 4])
@pytest.mark.parametrize("operator", ["|", "|&"])
@pytest.mark.parametrize("case", ["async-clean", "async-dangerous", "child"])
def test_descriptor_execution_gate(monkeypatch, tmp_path, major, operator, case):
    if major >= 4 and operator == "|&":
        bash_execution.require_modern_system_bash()
    module = keyword_cases if case == "child" else async_cases
    scan = module.scan_gateway_lifecycle
    run = module.subprocess.run
    scanned = []
    executed = []

    def record_scan(command):
        result = scan(command)
        scanned.append(result)
        return result

    def record_run(argv, **kwargs):
        assert scanned, "scanner assertion must precede the execution gate"
        assert argv[0] == "/bin/bash"
        executed.append(argv)
        return run(argv, **kwargs)

    monkeypatch.setattr(bash_execution, "system_bash_major", lambda: major)
    monkeypatch.setattr(module, "scan_gateway_lifecycle", record_scan)
    monkeypatch.setattr(module.subprocess, "run", record_run)

    def original():
        if case == "child":
            keyword_cases.test_child_close_keeps_parent_descriptor(tmp_path, f"exec 3<&- {operator} cat;")
        else:
            async_cases.test_async_list_preserves_parent(
                tmp_path, f"&& true {operator} cat & wait;", case == "async-clean",
            )

    if major < 4 and operator == "|&":
        with pytest.raises(pytest.skip.Exception, match="requires /bin/bash >= 4"):
            original()
        assert not executed
    else:
        # The supported premise runs the real host shell, not a fabricated result.
        original()
        assert len(executed) == 1
    assert scanned == [(case != "async-clean", None)]
