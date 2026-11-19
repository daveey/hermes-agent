"""Inherited descriptor alternatives are not nested-shell provenance proofs."""

import pytest

from cron.lifecycle_guard_nested_shells import _fed_shell_inputs


@pytest.mark.parametrize("command, expected", [
    ("exec 3<input.sh; { su -l bob; } 0<&3", []),
    ("{ su -l bob; } <input.sh", ["input.sh"]),
    ("cat input.sh | su -l bob", ["input.sh"]),
])
def test_nested_feed_keeps_inherited_descriptors_for_provenance(tmp_path, command, expected):
    # This collector handles proven group/file pipes. Original fd copies must
    # reach the separate source_contexts proof, which rejects non-flat scopes.
    (tmp_path / "input.sh").write_text("echo clean\n")
    assert list(_fed_shell_inputs(command, cwd=str(tmp_path))) == [
        (None, tmp_path / name) for name in expected
    ]
