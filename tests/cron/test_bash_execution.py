"""The shared execution gate rejects old Bash without changing scanner policy."""
from types import SimpleNamespace

import pytest

from tests.cron import bash_execution as execution


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("script", ["bash ./r.sh", "bash 2>$(echo /dev/null) < ./r.sh"])
@pytest.mark.parametrize("wrapper", ['echo "$(BODY)"', 'echo "$(echo "$(BODY)")"'])
def test_original_case_substitution_premises_use_shared_native_gate(tmp_path, script, wrapper):
    from tests.cron import test_lifecycle_guard_substitution_comments as original

    args = (tmp_path, 'case x in x) echo " # ";; esac; SCRIPT', wrapper, script, True)
    # Native proof: no mocked version or substitute Bash. Old Bash must report
    # the shared reason; Linux Bash 5 must still execute the original assertions.
    if execution.system_bash_major() < 4:
        with pytest.raises(pytest.skip.Exception, match="execution premise requires /bin/bash >= 4"):
            original.test_nested_quote_comment_boundary(*args)
    else:
        original.test_nested_quote_comment_boundary(*args)


@pytest.mark.parametrize("major", [3, 4, 5])
def test_version_gate(monkeypatch, major):
    monkeypatch.setattr(execution, "system_bash_major", lambda: major)
    if major < 4:
        with pytest.raises(pytest.skip.Exception, match="requires /bin/bash >= 4"):
            execution.require_modern_system_bash()
    else:
        assert execution.require_modern_system_bash() == "/bin/bash"


def test_version_probe_is_cached_and_uses_system_bash(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="5")

    execution.system_bash_major.cache_clear()
    monkeypatch.setattr(execution.subprocess, "run", run)
    try:
        assert execution.system_bash_major() == 5
        assert execution.system_bash_major() == 5
        assert len(calls) == 1
        argv, kwargs = calls[0]
        assert argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]
        assert "BASH_VERSINFO[0]" in argv[4]
        assert kwargs["check"] is True
        assert kwargs["timeout"] == 5
    finally:
        execution.system_bash_major.cache_clear()


@pytest.mark.parametrize("module", ["test_lifecycle_guard_fd_redirections", "test_lifecycle_guard_comment_words"])
def test_shared_marker_helpers_gate_before_execution(monkeypatch, tmp_path, module):
    import importlib

    helper = importlib.import_module(f"tests.cron.{module}")
    monkeypatch.setattr(execution, "system_bash_major", lambda: 3)
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **kw: pytest.fail("old Bash executed"))
    with pytest.raises(pytest.skip.Exception, match="requires /bin/bash >= 4"):
        helper._bash_runs(tmp_path, "true")
