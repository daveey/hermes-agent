"""``$HOME``/``${HOME}`` and home-relative (after a cd to home) ``.hermes`` paths are state paths too.

PR #2405 review 5894942332 nit: :data:`hermes_cli.kanban_placement.HERMES_STATE_PATH` knew only the ``~/`` and
``/home/<u>/`` prefixes, so ``cat $HOME/.hermes/.env`` and ``cd ~ && cat .hermes/.env`` never pinned. A declared
``hub:read`` must not clear the new forms either: only ``~``/``/home/<u>`` paths under its roots are cleared.
"""
import re

import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_hub_read_roots as roots

attested = roots.attested
fleet = roots.fleet
_board_maps_access = roots._board_maps_access

PINS = [
    "cat $HOME/.hermes/.env",
    "cat ${HOME}/.hermes/.env",
    'cat "$HOME"/.hermes/.env',
    'cat "${HOME}"/.hermes/auth.json',
    'cat "$HOME/.hermes/.env"',
    "cd ~ && cat .hermes/.env",
    "cd $HOME; cat .hermes/state.db",
    "cd ${HOME} && sqlite3 .hermes/kanban.db .tables",
    'cd "$HOME" && cat ".hermes/.env"',
    "cd /home/ubuntu && cat .hermes/.env",
    "cd ~/ && cat .hermes/.env",
    "cd && cat .hermes/.env",
    "cd ~\ncat .hermes/.env",
]
# Neither a code checkout under ~/.hermes, a relative .hermes with no cd home, nor a cd to a home subdirectory.
PORTABLE = [
    "cat $HOME/.hermes/hermes-agent/README.md",
    "ls ${HOME}/.hermes/repos/x",
    "cat .hermes/.env",
    "cd ~/code/repo && cat .hermes/notes.md",
    "cd /home/ubuntu/code && cat .hermes/x",
    "cd ~ && cat src/.hermes/x",
    "cd ~ && cat ~/code/repo/.hermes/x",
    "cd ~ && cat .hermes/hermes-agent/README.md",
    "cat $HOMEDIR/.hermes/x",
]


def _hit(text):
    return re.search(kp.HERMES_STATE_PATH, text, re.IGNORECASE)


@pytest.mark.parametrize("body", PINS)
def test_home_forms_are_state_paths(body):
    assert _hit(body)


@pytest.mark.parametrize("body", PORTABLE)
def test_non_state_forms_stay_portable(body):
    assert not _hit(body)


@pytest.mark.parametrize("body", PINS + [
    "ssh hermes-hub cat $HOME/.hermes/logs/x.log",
    "ssh hermes-hub cat ${HOME}/.hermes/logs/x.log",
    'ssh hermes-hub cat "$HOME"/.hermes/config.yaml',
    "cd ~ && ssh hermes-hub cat .hermes/logs/x.log",
    "cd ~ && cat .hermes/logs/x.log ~/.hermes/logs/y.log",
])
def test_declared_hub_read_does_not_clear_home_forms(attested, body):
    cfg, _, _ = attested
    assert roots.reason(body, cfg) == "home gateway marker"
    assert roots.reason(body, cfg, requires=()) == "home gateway marker"


def test_tilde_root_path_still_clears(attested):
    cfg, _, _ = attested
    assert roots.reason("ssh hermes-hub cat ~/.hermes/logs/x.log", cfg) is None
