"""``$HOME``/``${HOME}`` and home-relative (after a cd to home) ``.hermes`` paths are state paths too.

PR #2405 review 5894942332 nit: :data:`hermes_cli.kanban_placement.HERMES_STATE_PATH` knew only the ``~/`` and
``/home/<u>/`` prefixes, so ``cat $HOME/.hermes/.env`` and ``cd ~ && cat .hermes/.env`` never pinned. A declared
``hub:read`` must not clear the new forms either: only ``~``/``/home/<u>`` paths under its roots are cleared.
"""
import re
import time

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


# PR #2460 review 5895876398 nits 1-3: a pushd home and a cd home with a trailing shell comment are cd-home forms
# too; a prose "CD", a bare pushd (it swaps the stack, it does not go home) and a ``cd ~#x`` word are not.
@pytest.mark.parametrize("body", [
    "cd ~ # comment\ncat .hermes/.env",
    "cd # go home\ncat .hermes/.env",
    "pushd ~ && cat .hermes/.env",
    "pushd $HOME >/dev/null; cat .hermes/.env",
    "pushd /home/ubuntu\ncat .hermes/state.db",
    # PR #2533 review: a redirect that starts with an fd number (or ``&>``) after the home word.
    "pushd ~ 2>/dev/null && cat .hermes/.env",
    "cd ~ 2>/dev/null && cat .hermes/.env",
    "cd $HOME 2>&1 >/dev/null; cat .hermes/.env",
    "cd ~ &>/dev/null && cat .hermes/.env",
])
def test_pushd_and_commented_cd_home_are_state_paths(body):
    assert _hit(body)


@pytest.mark.parametrize("body", [
    "Burn a CD\ncat .hermes/.env",
    "CD ~ && cat .hermes/.env",
    "pushd && cat .hermes/.env",
    "cd ~#x && cat .hermes/.env",
    "popd ~ && cat .hermes/.env",
    # No space: ``~2`` / ``~/2`` is a literal directory word, not home plus a ``2>`` redirect.
    "cd ~2>x && cat .hermes/.env",
    "cd ~/2>x && cat .hermes/.env",
])
def test_prose_cd_and_non_home_pushd_stay_portable(body):
    assert not _hit(body)


def test_many_cd_home_lines_without_a_state_path_scan_in_linear_time():
    # A pasted shell transcript: before the fix each ``cd ~`` scanned to the end of the text (20000 lines: ~37 s).
    body = "cd ~ && make\n" * 20000
    start = time.perf_counter()
    assert not _hit(body)
    assert not _hit(body + "cat src/.hermes/x\n")
    assert _hit(body + "cat .hermes/.env\n")
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize("body, cls, path", [
    ("cd ~ && make all && cat .hermes/logs/x.log", "b", "~/.hermes/logs/x.log"),
    # PR #2533 review: ``"\u0130".lower()`` is two code points, so an offset taken on the lowered span shifted.
    ("cd ~ && make all \u0130 && cat .hermes/kanban/workspaces/t_x/f", "artifact-not-here",
     "~/.hermes/kanban/workspaces/t_x/f"),
])
def test_relative_state_path_hit_is_the_home_path_not_the_cd_span(body, cls, path, tmp_path, monkeypatch):
    from hermes_cli import kanban_home_marker_placement as hm

    monkeypatch.setenv("HOME", str(tmp_path))  # the own-artifact probe must not find a real workspace file
    match = _hit(body)
    [(decision, got_cls, hit, labels, note)] = hm._state_path(kp.HERMES_STATE_PATH, match, "worker-2", hm._Probe())
    assert (decision, got_cls, hit, labels) == ("queue", cls, path, ("capability:hub-write",))
    assert "make all" not in note
