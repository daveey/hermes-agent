"""A declared, attested ``hub:read`` clears hub-read's own reads: ``ssh hermes-hub <sub>`` and paths under its roots."""
import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_coordination as coord
from tests.hermes_cli import test_kanban_coordination_placement as helpers

fleet = helpers.fleet

ROOTS = ["logs", "cron", "kanban", "cache/core-update", "core-update", "cache/hermes-sync", "cache/delegation",
         "config.yaml", "coach", "user-steps", "inbox"]
STEP = "Step 1: ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log and report the matches."
# ``$HOME`` spellings and a home-relative path after a cd home are state paths too (review 5894942332 nit). The
# roots clear only ``~``/``/home/<u>`` spellings, so a declared hub:read keeps these pinned even under a root.
HOME_SPELLINGS = [
    "ssh hermes-hub cat $HOME/.hermes/logs/x.log",
    "ssh hermes-hub cat ${HOME}/.hermes/logs/x.log",
    'ssh hermes-hub cat "$HOME"/.hermes/logs/x.log',
    'ssh hermes-hub cat "${HOME}/.hermes/logs/x.log"',
    "cat $HOME/.hermes/.env",
    "cd ~ && cat .hermes/.env",
    "cd ~ && cat .hermes/logs/x.log",
    "cd $HOME && cat ./.hermes/logs/x.log",
    'cd "${HOME}"; cat .hermes/logs/x.log',
    "cd\ncat .hermes/.env",
    "ssh hermes-hub cat ~/.hermes/logs/x.log; cd ~ && cat .hermes/.env",
    "cd ~ && ssh hermes-hub cat ~/.hermes/logs/x.log && cat '.hermes/.env'",
    # The state directory itself (PR #2463 review 5895947424 nit): a cd into it. No root clears it:
    # _strip_read_paths keeps any text with such a cd.
    "cd ~/.hermes && cat .env",
    "cd $HOME/.hermes; cat .env",
    'cd "${HOME}/.hermes" && cat logs/x.log',
    "cd ~/.hermes/ && cat logs/x.log",
    "pushd /home/ubuntu/.hermes && cat logs/x.log",
    "cd ~ && cd .hermes && cat logs/x.log",
]
# A later cd elsewhere ends the home-relative span (review 5895947424 nit): a repo's own .hermes is no marker.
REPO_RELATIVE = [
    "cd ~ && git clone x repo && cd repo && cat .hermes/plans/p.md",
    "cd $HOME\ncd code/x\ncat .hermes/notes.md",
]


@pytest.fixture(autouse=True)
def _board_maps_access(monkeypatch):
    monkeypatch.setattr(coord, "BOARD_PLACEABLE_ACCESS", frozenset(coord.ACCESS_LABELS))


@pytest.fixture
def attested(fleet):
    cfg, entry, machines = fleet
    entry["labels"].append("capability:hub-read")
    cfg["hub_read_roots"] = list(ROOTS)
    return cfg, entry, machines


def reason(body, cfg, requires=("hub:read",)):
    return kp.portable_reason({"title": "Check the sync log", "body": body, "requires": list(requires)}, cfg)


def test_declared_read_step_is_portable(attested):
    cfg, _, _ = attested
    assert reason(STEP, cfg) is None


@pytest.mark.parametrize("body", [
    "ssh hermes-hub cat /home/ubuntu/.hermes/config.yaml",
    "ssh hermes-hub tail ~/.hermes/cron/output/x.log",
    "ssh hermes-hub ls ~/.hermes/cache/core-update/",
    "ssh hermes-hub sqlite kanban 'select 1'",
    "read `~/.hermes/kanban/boards/x/kanban.db`.",
    "ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log.",
    "ssh hermes-hub cat ~/.hermes/config.yaml: check kanban.placement",
    'ssh hermes-hub cat "~/.hermes/logs/x.log"',
    "ssh hermes-hub tail '~/.hermes/logs/x.log'.",
    "(evidence: ssh hermes-hub cat ~/.hermes/logs/x.log)",
    # Guards for review 5893721470 leave plain reads alone: an unquoted option value, a cd outside ~/.hermes.
    "ssh hermes-hub grep --file=~/.hermes/logs/pat.txt ~/.hermes/logs/x.log",
    "ssh hermes-hub cat ~/.hermes/logs/x.log, then cd /tmp && ls",
    # A word merely ending in ``cd`` is no directory change.
    "ssh hermes-hub cat ~/.hermes/logs/x.log; abcd",
])
def test_other_hub_read_shapes_are_portable(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg) is None


def test_undeclared_card_still_pins(attested):
    cfg, _, _ = attested
    assert reason(STEP, cfg, requires=()) == "home gateway marker"


def test_pool_without_label_still_pins(attested):
    cfg, entry, _ = attested
    entry["labels"].remove("capability:hub-read")
    assert reason(STEP, cfg) is not None


def test_unset_roots_fail_closed(attested):
    cfg, _, _ = attested
    del cfg["hub_read_roots"]
    assert reason(STEP, cfg) == "home gateway marker"
    assert reason("ssh hermes-hub grep X", cfg) is None  # the reader invocation alone needs no roots


@pytest.mark.parametrize("roots", ["logs", ["/"], [".."], ["logs/../state"], [""], [None]])
def test_malformed_roots_clear_nothing(attested, roots):
    cfg, _, _ = attested
    cfg["hub_read_roots"] = roots
    assert reason(STEP, cfg) == "home gateway marker"


@pytest.mark.parametrize("body", [
    "ssh hermes-hub cat ~/.hermes/state/x",
    "ssh hermes-hub cat ~/.hermes/logs/../state.db",
    "ssh hermes-hub cat ~/.hermes/logsx/y",
    "ssh mini grep X ~/.hermes/logs/hermes-sync.log",
    "ssh hermes-hub sudo reboot",
    "ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log, then restart the gateway",
    "ssh hermes-hub grep X ~/.hermes/logs/hermes-sync.log, then systemctl restart hermes-sync",
    "ssh hermes-hub cat ~/.hermes/config.yaml and hermes config set kanban.x 1",
    # A write glued to a root path is not part of the path (review 5877095261).
    "ssh hermes-hub cat ~/.hermes/logs/x&&systemctl restart hermes-gateway",
    "ssh hermes-hub cat ~/.hermes/logs/x&&hermes-sync push",
    "ssh hermes-hub cat ~/.hermes/logs/x:systemctl restart hermes-gateway",
    "ssh hermes-hub cat ~/.hermes/logs/x;systemctl restart hermes-gateway",
    "ssh hermes-hub cat ~/.hermes/logs/x|systemctl restart hermes-gateway",
    # Dot-only segments never count as under a root.
    "rm -rf ~/.hermes/logs/..",
    "rm -rf ~/.hermes/logs/x/..",
    "rm -rf ~/.hermes/logs/.",
    "rm -rf ~/.hermes/logs/...",
    # The path must end its shell word, or a quoted/brace/$/backslash/glob remainder hides `..` (review 5877418508).
    "chmod -R 777 ~/.hermes/logs/'..'",
    'chmod -R 777 ~/.hermes/logs/"../state.db"',
    "chmod -R 777 ~/.hermes/logs/x'/../../state.db'",
    "chmod -R 777 ~/.hermes/logs/{..,x}",
    "chmod -R 777 ~/.hermes/logs/{x,..}/state.db",
    "chmod -R 777 ~/.hermes/logs/$(echo ..)",
    "chmod -R 777 ~/.hermes/logs/${D}",
    "chmod -R 777 ~/.hermes/logs/\\..",
    "chmod -R 777 ~/.hermes/logs/[.][.]",
    # Closing punctuation may not be followed by more than one dot, and a trailing quote must close the quote that
    # opened right before the prefix (review 5877782890): each of these resolves outside the root.
    "chmod -R 777 ~/.hermes/logs/''..",
    'chmod -R 777 ~/.hermes/logs/""..',
    "chmod -R 777 $(echo ~/.hermes/logs/)..",
    'chmod -R 777 "$(echo ~/.hermes/logs/)"..',
    'chmod -R 777 $(echo "~/.hermes/logs/")..',
    "chmod -R 777 ~/.hermes/logs/' '/../../.env",
    "chmod -R 777 ~/.hermes/logs/x' '/../../state.db",
    "chmod -R 777 ''/home/ubuntu/.hermes/logs/' x/../../state.db'",
    "chmod -R 777 $(echo ~/.hermes/logs/x)/../../state.db",
    "chmod -R 777 ~/.hermes/logs/x(..)",
    "chmod -R 777 /home/../.hermes/logs/x",
    # Root paths are judged before marker_work_text drops backtick spans (review 5878536845): a backtick span
    # glued to the path hides its ``..`` (both resolve to ~/.hermes/.env).
    "chmod -R 777 ~/.hermes/logs/`x`/../.env",
    "chmod -R 777 ~/.hermes/logs/`echo ..`/.env",
    # An opening quote must close right after the path: whitespace inside the quoted word is part of it
    # (review 5892779312).
    'cat "~/.hermes/logs/a b/../../.env"',
    "cat '~/.hermes/logs/a b/../../.env'",
    'cat "/home/ubuntu/.hermes/logs/a b/../../.env"',
    # A bare parameter or a backtick substitution computes a ``..`` or a cd as well; bash reads <home>/.hermes/.env
    # for each of the first five. A cd spelled with quotes or a backslash is still a cd into a root.
    "X=~/.hermes/logs/ && ssh hermes-hub cat $X../.env",
    "X=.; Y=~/.hermes/logs/; ssh hermes-hub cat $Y$X$X/.env",
    "set -- ~/.hermes/logs/ . .; IFS=; cat \"$*\"/.env",
    "Y=~/.hermes/logs/ && ssh hermes-hub cat $Y`printf '\\56\\56'`/.env",
    "`printf 'c\\x64'` ~/.hermes/logs && cat `printf '\\56\\56'`/.env",
    "c''d ~/.hermes/logs && cat auth.json",
    "c\\d ~/.hermes/logs && cat auth.json",
    # Bash drops a whole backslash-newline line continuation before it splits words, so it joins the dots of a
    # ``..`` and the letters of a cd (review 5896219134 nit): the second reads <home>/.hermes/.env. A backslash
    # before a carriage return only quotes that CR, so the newline after it still starts a new command.
    "cat ~/.hermes/logs/x.log .\\\n./.env",
    "c\\\nd ~/.hermes/logs && cat .\\\n./.env",
    "c\\\nd ~/.hermes/logs && cat auth.json",
    "cat x\\\r\ncd ~/.hermes/logs && cat .env",
    # One row per exact anchor of the root-clearing guards: a mutant of each passed every other row.
    'ssh hermes-hub cat --file="~/.hermes/logs/a b/c"',
    "ssh hermes-hub cat ~/.hermes/logs/x.log .\\./.env",
    'ssh hermes-hub cat ~/.hermes/logs/x.log .""./.env',
    "ssh hermes-hub cat ~/.hermes/logs/x.log .``./.env",
    "ssh hermes-hub cat ~/.hermes/logs/x.log $'\\56\\56'/.env",
    "pushd ~/.hermes/logs && cat .env",
    "popd && cat ~/.hermes/logs/x.log",
    "cd ~/.hermes/logs && cat auth.json",
    "cd /tmp/x(y) && cat ~/.hermes/logs/x.log",
    # A relative ``..`` after a cd into a root walks out of it (review 5892779312 nit).
    "cd ~/.hermes/logs && cat ../.env",
    "cd ~/.hermes/logs; chmod -R 777 ../state",
    # A traversal written without a literal ``..`` (review 5893721470 nit): a quote glued to a non-space before the
    # prefix may be the opening one, quote/backslash-split dots are still ``..``, and a cd into a root lets any
    # relative or computed path walk out of it.
    'cat --file="/home/ubuntu/.hermes/logs/a b/$(printf "\\56\\56")/auth.json"',
    'cat --file="~/.hermes/logs/a b/$(printf "\\56\\56")/auth.json"',
    "cat x''/home/ubuntu/.hermes/logs/' b/'.''.'/'.''.'/state.db'",
    "cd ~/.hermes/logs && cd .\\. && cat auth.json",
    "cd ~/.hermes/logs && cd .''. && cat auth.json",
    'cd ~/.hermes/logs && cd "$(dirname "$PWD")" && cat .env',
    "cd ~/.hermes/logs && cd - && cat .env",
    "pushd ~/.hermes/logs && popd && cat .env",
    "echo 'a '/home/ubuntu/.hermes/logs/' b/'.''.'/'.''.'/state.db'",
    "cat ~/.hermes/logs/x; D=~/.hermes/logs/; cat ${D%logs/}.env",
    "D=$(dirname ~/.hermes/logs/x); cat $(dirname $D)/.env",
] + HOME_SPELLINGS)
def test_controls_still_pin(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg) == "home gateway marker"


@pytest.mark.parametrize("body", HOME_SPELLINGS)
def test_home_spellings_pin_an_undeclared_card(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg, requires=()) == "home gateway marker"


@pytest.mark.parametrize("body", REPO_RELATIVE)
def test_a_repo_relative_hermes_after_a_later_cd_does_not_pin(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg, requires=()) is None
    assert reason(body, cfg) is None


@pytest.mark.parametrize("body", [
    # A whole backtick span is a name to marker_work_text (main fdd26189801), so main already ignores these with or
    # without hub:read; the declaration must not change the answer either way.
    "chmod -R 777 `echo ~/.hermes/logs/`..",
    "chmod -R 777 `~/.hermes/state/x`",
    "read `~/.hermes/logs/x.log` then chmod -R 777 ~/.hermes/state/x",
    # Removing a whole path must not merge its delimiters into a new backtick run.
    "Read `~/.hermes/logs/x.log` then chmod -R 777 ~/.hermes/state/x with ``foo``",
    "Read `~/.hermes/logs/x.log` then restart the gateway; see ``foo``",
    "``a`` then `~/.hermes/logs/x.log` then systemctl restart hermes ``b``",
])
def test_backtick_spans_follow_main_with_or_without_hub_read(attested, body):
    cfg, _, _ = attested
    assert reason(body, cfg) == reason(body, cfg, requires=())


def test_claim_refusal_unchanged(attested):
    _, entry, _ = attested
    card = {"placement_policy": "worker-first-v1", "title": "t", "body": STEP, "requires": ["hub:read"]}
    assert coord.claim_refusal(card, "worker") is None
    entry["labels"].remove("capability:hub-read")
    assert coord.claim_refusal(card, "worker") == coord.REASON
