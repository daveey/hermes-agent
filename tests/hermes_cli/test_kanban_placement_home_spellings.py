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
