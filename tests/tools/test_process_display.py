"""display_command: what a background process is shown as. [daveey]"""

import pytest

from tools.process_display import display_command

NICE = ('# kanban-worker-nice terminal floor\n'
        'if [ "$(nice)" -lt 10 ]; then renice -n 10 -p "${BASHPID:-$$}" >/dev/null || exit $?; fi\n')


@pytest.mark.parametrize("raw, shown", [
    (NICE + "bb-land --wait", "bb-land --wait"),
    (NICE + NICE + "make test", "make test"),  # stacked prefixes
    (NICE.replace("\n", "\r\n", 1) + "ls", "ls"),
    (NICE, ""),
    (NICE.rstrip("\n"), ""),
    ("# kanban-worker-nice terminal floor", "# kanban-worker-nice terminal floor"),  # no injected line: not a prefix
    ("ls\n" + NICE + "pwd", "ls\n" + NICE + "pwd"),  # only a leading block
    ("# build it\nmake", "# build it\nmake"),
    ("", ""),
    (None, ""),
])
def test_display_command(raw, shown):
    assert display_command(raw) == shown
