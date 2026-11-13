"""Every spelling of a home-rooted ``~/.hermes`` state path pins a hub-origin card (review 5894942332 nit).

Before this only ``~/`` and ``/home/<u>/`` pinned, so ``cat $HOME/.hermes/.env`` and ``cd ~ && cat .hermes/.env``
named hub state yet routed heavy work to the worker. [daveey]"""
import re
import time

import pytest

from tests.hermes_cli import test_kanban_placement as helpers


@pytest.mark.parametrize("body", [
    "Run pytest, then cat $HOME/.hermes/.env",
    "Run pytest, then cat ${HOME}/.hermes/.env",
    'Run pytest, then cat "$HOME"/.hermes/.env',
    'Run pytest, then cat "${HOME}/.hermes/.env"',
    "Run pytest, then cd ~ && cat .hermes/.env",
    "Run pytest, then cd $HOME; cat ./.hermes/.env",
    'Run pytest, then cd "$HOME"\ncat .hermes/.env',
    "Run pytest, then cd && cat .hermes/.env",
    "Run pytest, then cd /home/ubuntu/ && cat .hermes/.env",
    "Run pytest, then pushd ~ && cat .hermes/.env",
])
def test_home_spellings_of_state_paths_stay_local(body):
    d = helpers._hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


@pytest.mark.parametrize("body", [
    "Run pytest, then cat $HOME/.hermes/hermes-agent/README.md",   # the code checkout is not state
    "Run pytest, then cd ~/code/x && cat .hermes/notes.md",         # a repo's own .hermes dir, not home
    "Run pytest, then cd ~ && cat code/x/.hermes/notes.md",
    "Run pytest, then cd ~ && cat .hermes/repos/x",
    "Run pytest in CI/CD; see .hermes/notes.md in the repo",
])
def test_home_spellings_of_code_paths_stay_portable(body):
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


# PR #2463 review 5895947424 nits. (1) A later ``cd`` elsewhere ends the home-relative span: a repo's own
# ``.hermes`` after ``cd repo`` is not home state. (3) A ``cd``/``pushd`` into the state directory itself (any home
# spelling) names it.
@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~/.hermes && cat .env",
    "Run pytest, then cd $HOME/.hermes; cat .env",
    'Run pytest, then cd "${HOME}/.hermes" && cat auth.json',
    'Run pytest, then cd "$HOME"/.hermes/\ncat auth.json',
    "Run pytest, then cd /home/ubuntu/.hermes/ && cat .env",
    "Run pytest, then pushd ~/.hermes && cat .env",
    "Run pytest, then cd ~ && cd .hermes && cat .env",
    "Run pytest, then cd ~ && cd ./.hermes/ && cat .env",
    "Run pytest, then cd ~ && cd repo && cd ~ && cat .hermes/.env",
])
def test_the_state_directory_itself_stays_local(body):
    d = helpers._hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && git clone x repo && cd repo && write .hermes/plans/p.md",
    "Run pytest, then cd ~\ngit clone x repo\ncd repo\ncat .hermes/notes.md",
    "Run pytest, then pushd ~ && cd code/x && cat .hermes/notes.md",
    "Run pytest, then cd ~ && cd /tmp/x && cat .hermes/notes.md",
    "Run pytest, then cd ~/.hermes/hermes-agent && cat README.md",
    "Run pytest, then cd ~/.hermesrc && ls",
    # A bare ~/.hermes outside a cd is ordinary repo work (9328cbc2d9b: "the hermes-agent tree under ~/.hermes").
    "Run pytest, then read the hermes-agent tree under ~/.hermes",
    "Run pytest, then ls ~/.hermes/hermes-agent/",
])
def test_a_later_cd_elsewhere_ends_the_home_relative_span(body):
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


# PR #2585 review 5897242912 nit 1: only a cd that changes this shell's directory ends the span. A subshell
# ``(cd repo ...)`` does not; ``cd -``/``popd``/a bare ``pushd`` go somewhere unknown, so the span stays open (fails
# toward the pin); a prose "cd" outside a command position does not end it either.
@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && (cd repo && make) && cat .hermes/.env",
    "Run pytest, then cd ~\n(cd repo && make)\ncat .hermes/.env",
    "Run pytest, then cd ~ && x=$(cd repo && pwd) && cat .hermes/.env",
    "Run pytest, then cd ~ && cd - && cat .hermes/.env",
    "Run pytest, then pushd ~ && popd && cat .hermes/.env",
    "Run pytest, then cd ~ && pushd && cat .hermes/.env",
    "Run pytest, then cd ~ && echo you can cd later && cat .hermes/.env",
    "Run pytest, then cd ~  # no need to cd anywhere else\ncat .hermes/.env",
    "Run pytest, then cd ~ && ls | cd repo && cat .hermes/.env",
    "Run pytest, then cd ~ && (cd repo && cat .hermes/.env)",
    "Run pytest, then cd ~/.hermes && cd hermes-agent-old && cat ../.env",
    "Run pytest, then cd ~/.hermes && echo cd hermes-agent && cat .env",
])
def test_only_a_cd_of_this_shell_ends_the_span(body):
    d = helpers._hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && cd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ || cd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~; cd repo; cat .hermes/notes.md",
    "Run pytest, then cd ~\n  cd repo\ncat .hermes/notes.md",
    "Run pytest, then cd ~ and run `cd repo && cat .hermes/notes.md`",
    "Run pytest, then cd ~ && { cd repo; cat .hermes/notes.md; }",
    "Run pytest, then cd ~ && pushd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ && cd .. && cat .hermes/notes.md",
    # PR #2585 review 5897242912 nit 2: ~/.hermes then its code tree is the code tree, not state.
    "Run pytest, then cd ~/.hermes && cd hermes-agent && git pull",
    "Run pytest, then cd \"$HOME/.hermes\"  # state root\ncd 'hermes-agent' && git pull",
    "Run pytest, then pushd ~/.hermes; pushd repos/x && git pull",
    "Run pytest, then cd ~ && cd .hermes && cd hermes-agent/scripts && ls",
])
def test_a_cd_of_this_shell_still_ends_the_span(body):
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


# Post-merge mutants on PR #2585 (card t_01a1d6cc4a9eaa0a): cd/pushd match lowercase only, as the shell does. An
# uppercase "CD" in prose (a disc, CI/CD) neither ends a home-relative span nor starts or names one.
@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && read the CI/CD notes, then cat .hermes/.env",
    "Run pytest, then cd ~ && read the notes\nCD burns are out of scope; cat .hermes/.env",
    "Run pytest, then cd ~; CD repo; cat .hermes/.env",
    "Run pytest, then cd ~ && CD ~/x && cat .hermes/.env",
])
def test_an_uppercase_cd_does_not_end_the_span(body):
    d = helpers._hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


# PR #2649 review 5898066605 nit: a cd nested in a subshell, ``$( )`` or a backtick substitution changes no cwd of
# this shell, however many cds the nested command chains; ``pushd -n`` only edits the stack.
@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && (cd repo && cd sub && make) && cat .hermes/.env",
    "Run pytest, then cd ~ && x=$(cd a && cd b && pwd) && cat .hermes/.env",
    "Run pytest, then cd ~\n(\n  cd repo\n  cd sub; make\n)\ncat .hermes/.env",
    "Run pytest, then cd ~ && x=$(cd a && y=$(cd b; cd c) && cd d) && cat .hermes/.env",
    "Run pytest, then cd ~ && x=`cd repo && pwd` && cat .hermes/.env",
    'Run pytest, then cd ~ && echo "`cd repo; cd sub; pwd`" && cat .hermes/.env',
    "Run pytest, then cd ~ && pushd -n repo && cat .hermes/.env",
    # The state path inside the nesting still names home state, as before.
    "Run pytest, then cd ~ && (x=$(cat .hermes/.env))",
    "Run pytest, then cd ~ && x=$(cd a && y=$(cat .hermes/.env))",
])
def test_a_nested_cd_never_ends_the_span(body):
    from hermes_cli import kanban_placement as kp
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE)  # raw text too: here backtick spans are not stripped
    d = helpers._hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


@pytest.mark.parametrize("body", [
    "Run pytest, then label the backup CD; then cat .hermes/notes.md in the repo",
    "Run pytest, then label the backup CD ~/.hermes; it holds the hermes-agent tree",
])
def test_an_uppercase_cd_is_no_home_marker(body):
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


@pytest.mark.parametrize("body", [
    "Run pytest, then cd ~ && (cd repo && make) && cd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ && x=$(cd a && cd b) && cd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ && x=`cd a && pwd` && cd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ && pushd -n x && pushd repo && cat .hermes/notes.md",
    "Run pytest, then cd ~ && echo :( && cd repo && cat .hermes/notes.md",  # an unclosed paren is no subshell
    "Run pytest, then cd ~ && x=`cd a && cd repo && cat .hermes/notes.md",  # nor an unclosed backtick
    "Run pytest, then cd ~ and run `make` then `cd repo && cat .hermes/notes.md`",  # markdown code spans
])
def test_a_top_level_cd_after_the_nesting_still_ends_the_span(body):
    from hermes_cli import kanban_placement as kp
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE) is None
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


@pytest.mark.parametrize("body", [
    "cd ~ && x=$(cd a && y=`cat .hermes/.env`)",   # marker_work_text strips backtick spans; the raw regex reads them
    "cd ~ && x=`cd a; cd b` && cat .hermes/.env",
])
def test_the_raw_pattern_reads_backtick_substitution(body):
    from hermes_cli import kanban_placement as kp
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE)


@pytest.mark.parametrize("unit", [
    "(cd ~) x=$(cd a && (cd b) && `cd c`) step {i} padded out to a realistic length ",
    "cd ~ && (( ( ( x=` step {i} padded out to a realistic length\n",
    "cd ~ && x=`cd a` y=$(cd b) (cd c (cd d (cd e))) && cd f step {i}\n",
])
def test_a_large_no_match_nested_body_is_linear(unit):
    from hermes_cli import kanban_placement as kp
    body = "Run pytest, then " + "".join(unit.format(i=i) for i in range(5000))
    start = time.monotonic()
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE) is None
    assert time.monotonic() - start < 1.0


def test_a_large_no_match_prose_cd_body_is_linear():
    """Spans no longer end at a prose cd, so a later cd home must still end one: no O(n^2) on one long line."""
    from hermes_cli import kanban_placement as kp
    body = "Run pytest, then " + "".join(
        f"(cd ~) then say cd somewhere in prose, step {i} padded to a realistic length " for i in range(5000))
    assert len(body) > 180_000
    start = time.monotonic()
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE) is None
    assert time.monotonic() - start < 1.0


def test_a_large_no_match_body_is_linear():
    """(2) Before, each qualifying cd ran a lazy span to the end of the text: 16.9 s at 180 KB / 5000 cd lines."""
    from hermes_cli import kanban_placement as kp
    body = "Run pytest, then\n" + "".join(
        f"cd ~ && echo step {i} padded out to a realistic shell line length\n" for i in range(5000))
    assert len(body) > 180_000
    start = time.monotonic()
    assert re.search(kp.HERMES_STATE_PATH, body, re.IGNORECASE) is None
    assert time.monotonic() - start < 1.0
    start = time.monotonic()
    assert helpers._hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]
    assert time.monotonic() - start < 5.0
