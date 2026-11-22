"""Refuse a second owner for work an open card already does (kanban_create). [daveey]

A chat counted owners by its OWN chat links, found none, and created "Stage B" on the default board while
Stage B was already running on board ``unconventional-collab`` from another chat. Before creating, this scans
open cards on every board of this gateway (live board DBs) and every other gateway (the hermes-hub replica,
``hub.db``), so the answer no longer depends on which chat asks or which board the card lands on.

An open card matches when:
- the new card names its board, is not being created on that board, and the titles share a word (for a
  plain-word slug, 2 words that are a quarter of the longer title: one shared word or a hostname with any of
  the hundreds of cards on ``bb`` or ``hermes`` is chance, not overlap), or
- both titles share at least half of the longer title's significant words (3 or more), or
- both titles name the same code file (``tests/run_ci.py`` / ``run_ci.py``) and share 6 or more significant words
  that are at least 30% of the longer title (:func:`code_files`).

The hermes-mods core mirror is a singleton job: a whole ``mirror`` token plus
``hermes-mods``, ``core``, or both ``fork`` and ``backlog`` matches regardless of score
or heuristic exemptions below. Exact-key retries still return the existing card;
a capability owner serves its quoted waiting card, not a second pass. An intentional
second owner requires a recorded override.

Naming a board means an explicit reference in the title or body: ``board <slug>``, ``--board <slug>``,
``<slug> board``, ``@task:<slug>@...`` / ``@task:<slug>/...`` or ``<gateway>/<slug>`` for a known gateway
(``hub/runners``). A plain-word slug (``hermes``, ``bb``, ``runners``) inside a URL, a filesystem path or a
slash list is NOT a reference: ``github.com/daveey/hermes``, ``~/code/hermes``, ``Slack/Discord/BB Inbox``.
A slug that is not one plain word (``unconventional-collab``) is specific enough that ``/<slug>`` anywhere
or the slug itself in the title still names it (``Metta-AI/unconventional-collab``). A bare plain word that
equals a board slug does not count.

Closed cards never match (a replica's stale legacy ``<board>/<id>`` row never outvotes its qualified
``<profile>/<board>/<id>`` twin). Heuristic exemptions (singletons ignore these, except the waiting card a
capability-owner create quotes and a card bound to it, :func:`_bound_to`): the creator's own card, the new
card's parents, cards the same chat created (fan-out), the waiting card a capability-owner
create quotes, a local card bound to that
waiting card (its deferred-placement origin, or a card parked on it: :func:`_bound_to`), and another capability-owner
card for that same waiting card, a card the new one parks/waits/depends on (:func:`waits_on`), a card whose failed
execution the new one recovers (:func:`recovers`: ``self-heal: kanban card t_X ... failed 2x``), and a card that
names a different target (:func:`distinct_target`: another repo, host or process, an inverted check at another
revision, or another work role, :func:`work_role`: implement vs measure vs deliver; a newer sha or another
ticket number alone is not a different target). A card the same worker created is NOT exempt: it meets
the same match, then the same-creator admits and the shared-fix-site match in
:mod:`hermes_cli.kanban_overlap_own`; a near-identical re-issue in the same run returns the first card
(``deduplicated``) instead of creating a second. Admitted look-alikes
are logged. Each card counts once per gateway. A missing or unreadable replica is skipped and logged, never a block.
``kanban.create_overlap_check: false`` turns the check off.

Concurrent creates (11-03, two owners of one failure inserted 186 ms apart): this full check reads outside the
INSERT's transaction, so :class:`AfterCreate` re-runs the match inside it (``create_task(insert_guard=...)``) against
open cards of the target board created since the check began. Other boards and gateways stay a documented race.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional

from hermes_cli import kanban_overlap_own as _own

_log = logging.getLogger(__name__)

EVENT = "create_overlap"
OPEN = ("todo", "ready", "running", "scheduled", "blocked", "review", "triage")
MAX_SHOWN = 8
MAX_WHY = 300
LOG_NAME = "create_overlap.jsonl"
LOG_MAX_BYTES = 1024 * 1024
MIN_SHARED = 3
PLAIN_BOARD_SCORE = 0.25
_STOP = frozenset("""
a an and are as at be by for from in into is it its of on or the then this that to via with without after before
when not no all new card task fix add run make use using update ensure follow up followup self heal
""".split())


def enabled() -> bool:
    from hermes_cli.config import cfg_get, load_config
    return bool(cfg_get(load_config(), "kanban", "create_overlap_check", default=True))


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 2 and w not in _STOP}


def _plain_word_slug(slug: str) -> bool:
    """True when the slug is one alphabetic word (``runners``, ``hermes``, ``bb``).

    A hyphen, underscore or digit (``unconventional-collab``) is specific enough that the
    slug itself in the title still names the board. A plain word is an ordinary word.
    """
    return bool(re.fullmatch(r"[a-z]+", (slug or "").lower()))


_SEP = r"[\s:=`'\\\"]+"
_END = r"(?![a-z0-9_-])"


def _explicit_board_ref(slug: str, text: str, gateways: Iterable[str] = ()) -> bool:
    """``board <slug>``, ``--board <slug>``, ``<slug> board``, ``@task:<slug>@gw/...``, ``<gateway>/<slug>``.

    A plain-word slug after a ``/`` in a URL, path or slash list (``github.com/daveey/hermes``,
    ``~/code/hermes``, ``Slack/Discord/BB Inbox``) is not a reference (hub t_01a189b2d186a986);
    a slug that is not one plain word (``Metta-AI/unconventional-collab``) still is.
    """
    s = re.escape((slug or "").lower())
    if not s:
        return False
    forms = [rf"\bboard{_SEP}{s}{_END}", rf"(?<![a-z0-9_-]){s}{_END}{_SEP}board\b", rf"@task:{s}(?=[@/])"]
    gws = "|".join(re.escape(g.lower()) for g in sorted(set(gateways)) if g)
    if gws:
        # ``hub/runners`` or ``hub/runners/t_...`` as a whole token, never a segment of a longer path or URL.
        forms.append(rf"(?<![\w./:~@-])(?:{gws})/{s}{_END}(?!/(?!t_[0-9a-f]))")
    if not _plain_word_slug(slug):
        forms.append(rf"/{s}{_END}")
    return bool(re.search("|".join(forms), (text or "").lower()))


def _names_board(slug: str, title: str, body: str, gateways: Iterable[str] = ()) -> bool:
    if not slug or slug == "default":
        return False
    if _explicit_board_ref(slug, title, gateways) or _explicit_board_ref(slug, body, gateways):
        return True
    # A bare slug token in the title names the board only when it is not a plain word.
    # "runners" in "bb-linux runners can't run jobs" is not a board reference.
    if _plain_word_slug(slug):
        return False
    s = re.escape(slug.lower())
    return bool(re.search(rf"(?<![a-z0-9_-]){s}(?![a-z0-9_-])", (title or "").lower()))


def singleton_job(title: str) -> Optional[str]:
    """Explicit single-writer jobs, not a change to general title similarity. [daveey]"""
    tokens = set(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", (title or "").lower()))
    # "core"/"fork backlog" name the mirror pass only outside hermes-sync bug cards: "a core pass of hermes-sync
    # crashed … hosts without a mirror checkout" is a hermes-sync fix, not a second mirror pass (t_01a19ddcef566978).
    mirror_words = {"hermes-mods"} if "hermes-sync" in tokens else {"hermes-mods", "core"}
    if "mirror" in tokens and (tokens & mirror_words or ("hermes-sync" not in tokens and {"fork", "backlog"} <= tokens)):
        return "hermes-mods core mirror"
    return None


# Failure identity (t_01a19ea9f1739df6). One red-CI failure, five owners in 23 min on 11-02: the cards named the same
# failing test (``test_busy_board_open_does_not_block_session_poll``) or the same Actions run (41516070553), but their
# titles shared too few words for the overlap above. A card NAMES a failing test when its title carries a pytest
# function name (``test_\w+``), its body's opening paragraph carries a node id (``tests/…py::test_…``) in a sentence
# that says it fails. A body whose only tests are pytest ``FAILED <node id>`` lines (ci_main_watch lists them) has
# them as ``listed`` tests: they match a card naming one of them, but they are the run's failures, not a test the card
# names, so that card still owns its run (t_01a1a05806da4ef0). A node id elsewhere in a body is context or a
# verification step ("the regression test tests/…::test_x checks …"), not the failure. A card names a run by
# ``actions/runs/<id>`` anywhere in its title or body.
_NODE = re.compile(r"(?<![\w-])(?:[\w.-]+/)*(test_\w+)\.py::(?:[A-Za-z_]\w*::)*(test_\w+)")
_BARE_TEST = re.compile(r"(?<![\w/.:-])(test_\w*[a-z0-9])(?![\w.:/-])")
_RUN = re.compile(r"\bactions/runs/(\d{6,})\b")
_FAILING = re.compile(
    r"FAILED|\bfail(?:s|ed|ing|ure|ures)?\b|[a-z]Error\b|\btime[sd]?[ -]?out\b|\btiming out\b"
    r"|flak(?:e|es|y|iness)\b|\bred\b|\bbroken\b|\bregress\w*|\bhangs?\b", re.IGNORECASE)
_FAILED_LINE = re.compile(r"^[\s|║>*`-]*FAILED\s+(.*)$", re.MULTILINE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_FAILURE_LEAD = 800  # the opening states the failure; later sections are context, history and verification steps
_MIN_TEST_NAME = 10  # "test_" + 5: "test_main" or "test_ok" names too many unrelated tests
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8,16}\b")


class FailureIdentity(NamedTuple):
    tests: frozenset  # failing test functions (parameters dropped)
    modules: frozenset  # test modules of the node ids named
    bare: frozenset  # test names in the title without a module (may be a module: "test_kanban_db spawn tests fail")
    runs: frozenset  # GitHub Actions run ids
    # True when ``tests`` came only from ``FAILED <node id>`` lines (ci_main_watch's ``failed tests:`` block): the whole
    # run's failures, not a test the card is about, so the card still owns the run (t_01a1a05806da4ef0).
    listed: bool = False


def _node_names(text: str) -> tuple[set[str], set[str]]:
    funcs, mods = set(), set()
    if "::" in text:
        for m in _NODE.finditer(text):
            mods.add(m.group(1))
            funcs.add(m.group(2))
    return funcs, mods


def failure_identity(title: str, body: str) -> FailureIdentity:
    """The failing tests and CI runs a card is about (see the comment above). Pure; never raises on text."""
    title, body = title or "", (body or "")[:_BODY_SCAN]
    funcs, mods = _node_names(title)
    bare = {t for t in _BARE_TEST.findall(_NODE.sub(" ", title) if "::" in title else title)
            if len(t) >= _MIN_TEST_NAME}
    funcs |= bare
    lead = body.strip().split("\n\n", 1)[0][:_FAILURE_LEAD]
    if "::" in lead:
        for sentence in _SENTENCE.split(lead):
            if "::" in sentence and _FAILING.search(_NODE.sub(" ", sentence)):
                f, m = _node_names(sentence)
                funcs, mods = funcs | f, mods | m
    from_list = "FAILED" in body and not funcs
    if from_list:
        # Only a card whose subject is the log list (ci_main_watch: "main is red", then FAILED lines). A card that
        # already named its test pastes a job tail whose FAILED lines include OTHER tests of that job.
        for line in _FAILED_LINE.findall(body):
            f, m = _node_names(line)
            funcs, mods = funcs | f, mods | m
    runs = set(_RUN.findall(title)) | (set(_RUN.findall(body)) if "actions/runs/" in body else set())
    tests = frozenset(f for f in funcs if len(f) >= _MIN_TEST_NAME)
    return FailureIdentity(tests, frozenset(mods), frozenset(bare), frozenset(runs), listed=from_list and bool(tests))


def same_failure(new: FailureIdentity, card: FailureIdentity) -> Optional[str]:
    """Why two cards own one failure, else None: the same failing test function (a title's bare name may be the
    other card's test module), or the same Actions run when neither card names its own failing test. One run fails
    many tests, so two cards naming different tests of one run are different failures. A run's FAILED list
    (``listed``) is not a test the card names: it matches a card naming one of those tests, and the card still owns
    the run."""
    tests = (new.tests & card.tests) | (new.bare & card.modules) | (card.bare & new.modules)
    if tests:
        return "same failing test " + ", ".join(sorted(tests)[:3])
    runs = new.runs & card.runs
    if runs and not _names_own_test(new) and not _names_own_test(card):
        return "same CI run " + ", ".join(f"actions/runs/{r}" for r in sorted(runs)[:3])
    return None


def _names_own_test(fi: FailureIdentity) -> bool:
    """The card names a failing test of its own (title, or a failing sentence of its opening), not only a FAILED list."""
    return bool(fi.tests) and not fi.listed


def _failure_reason(why: str) -> bool:
    return why.startswith(("same failing test ", "same CI run "))


def cites(card_id: str, title: str, body: str) -> bool:
    """The new card names ``card_id``: written knowing that card (a follow-up, an unstick, a split of its scope)."""
    return card_id in set(_CARD_ID.findall(f"{title}\n{(body or '')[:_BODY_SCAN]}"))


# Template titles (t_01a1db274c88e2e3): ``hermes-core-wt ship --request-review`` files "Independent exact-head review
# PR #N @<sha> (core-wt ship review required)", so two reviews of different PRs share ~80% of their words and only the
# PR number tells them apart. Titles that each name PRs, and no PR in common, are different work by title.
# owner/repo#N (2026-11-21): core-ship-backstop files "Retro exact-head review: softmax-universe/daveey-hermes#N merged
# without a hermes-core-wt/ship receipt". A bare ``#`` after a word character is no ref (``state#12``), so those titles
# named no PR and every retro for another PR collided with any open one: the backstop stalled from 03:41Z until state
# PR #3876 worked around it. A ref is now (repo, N): ``owner/repo#N`` and a github.com ``.../owner/repo/pull/N`` URL
# carry their repo (lowercased); ``#N``, ``PR N``, ``PRN``, ``PR #N`` and ``pull/N`` carry none and match any repo.
# ``##N``, ``word#N`` and ``a/b/c#N`` stay non-refs; N is 2-6 digits. A trailing ``.git`` on the repo is dropped.
# ``\bpr\s*(?:#\s*)?`` (not ``\s*#?\s*``): two adjacent ``\s*`` backtracked quadratically on "pr" + many spaces.
_PR_REF = re.compile(
    r"(?<![\w./#-])(?P<repo>[a-z0-9][a-z0-9-]{0,38}/[a-z0-9_.-]+)#(?P<rn>\d{2,6})(?!\d)"
    r"|github\.com/(?P<url>[a-z0-9][a-z0-9-]{0,38}/[a-z0-9_.-]+)/pull/(?P<un>\d{2,6})(?!\d)"
    r"|(?:(?<![\w#])#|\bpr\s*(?:#\s*)?|\bpull/)(?P<n>\d{2,6})(?!\d)",
    re.IGNORECASE)

PrRef = tuple[Optional[str], int]  # (lowercased "owner/repo" or None when the title does not say, PR number)


def pr_refs(title: str) -> frozenset[PrRef]:
    """PR refs a title names, as (repo or None, number): ``owner/repo#N`` and ``github.com/owner/repo/pull/N`` carry
    their repo; ``#N``, ``PR N``, ``PRN``, ``PR #N``, ``pull/N`` do not (N of 2-6 digits)."""
    refs = set()
    for m in _PR_REF.finditer(title or ""):
        repo = m.group("repo") or m.group("url")
        repo = repo.lower().removesuffix(".git") if repo else None  # o/r.git#N is o/r#N (same PR, never "different")
        refs.add((repo, int(m.group("rn") or m.group("un") or m.group("n"))))
    return frozenset(refs)


def pr_numbers(title: str) -> frozenset[int]:
    """PR numbers a title names, whatever the repo (see ``pr_refs``)."""
    return frozenset(n for _, n in pr_refs(title))


def _same_pr(x: PrRef, y: PrRef) -> bool:
    """Equal numbers in the same repo, or a side that names no repo (conservative: it may be either)."""
    return x[1] == y[1] and (x[0] is None or y[0] is None or x[0] == y[0])


def _different_prs(a: str, b: str) -> bool:
    """Both titles name a PR and no PR is common to them (``_same_pr``: ``a/b#5`` and ``c/d#5`` differ)."""
    mine, theirs = pr_refs(a), pr_refs(b)
    return bool(mine and theirs) and not any(_same_pr(x, y) for x in mine for y in theirs)


def _reasons(card: dict, new_words: set[str], title: str, body: str, target_board: str,
             gateways: Iterable[str] = (), failure: Optional[FailureIdentity] = None) -> tuple[list[str], float]:
    reasons, score = [], 0.0
    job = singleton_job(title)
    if job and job == singleton_job(card.get("title") or ""):
        reasons.append(f"same singleton job: {job}")
    if failure is not None and (failure.tests or failure.runs):
        why = same_failure(failure, failure_identity(card.get("title") or "", card.get("body") or ""))
        if why:
            reasons.append(why)
            score = 1.0
    theirs = words(card.get("title") or "")
    shared = new_words & theirs
    # A board named in passing (a path, a log line) is not enough: the titles must share a word too. A plain-word
    # board (``hermes``, ``bb``) holds hundreds of cards, so one common word ("skill", "create") or a hostname
    # ("David-MBP") matches some card there by chance: it needs 2 shared words that are a quarter of the title.
    named = shared - words(card["board"])
    if _plain_word_slug(card["board"]):
        named_enough = len(named) >= 2 and len(shared) / max(len(new_words), len(theirs)) >= PLAIN_BOARD_SCORE
    else:
        named_enough = bool(named)
    if (card["board"] != target_board and named_enough
            and _names_board(card["board"], title, body, gateways)):
        reasons.append(f"the new card names board {card['board']!r}")
    if new_words and theirs:
        ratio = len(shared) / max(len(new_words), len(theirs))
        score = max(score, ratio)
        # The title ratio alone: a failure match sets score to 1.0 without any title overlap.
        if _different_prs(title, card.get("title") or ""):
            pass  # Template words shared by titles of different PRs say nothing (_PR_REF).
        elif len(shared) >= MIN_SHARED and ratio >= 0.5:
            reasons.append(f"titles share {len(shared)} words ({', '.join(sorted(shared))})")
        elif len(shared) >= MIN_FILE_SHARED and ratio >= FILE_SCORE and (same := code_files(title)
                                                                          & code_files(card.get("title") or "")):
            reasons.append(f"titles name {', '.join(sorted(same))} and share {len(shared)} words "
                           f"({', '.join(sorted(shared))})")
    return reasons, score


# Shared code file (t_01a1d290dc768ba1): "State suite: ... runs tests/run_ci.py on a pool worker ..." and "Local full
# state suite on pool workers: run_ci.py fails ..." were one job across two gateways, but long descriptive titles
# shared 8 of 19 words, under half. A file both titles name is a strong signal, like a shared failing test; with 6
# shared words, replayed over 14 days of replica cards (8112 creates, 482 naming a file), it adds 2 refusals, both
# true duplicates, and no false one; 3 more fire but cite the open card by id and are admitted (see _find).
# A dotted basename is one name: ``sync-handoff.test.ts``, never its ``test.ts`` suffix (every vitest file has it).
_CODE_FILE = re.compile(r"(?<![\w.-])(?:[\w.~-]+/)*((?:[\w-]+\.)*[\w-]*[a-z][\w-]*\."
                        r"(?:py|sh|ts|tsx|js|mjs|cjs|json|jsonl|ya?ml|"
                        r"toml|sql|md|swift|rs|go|rb|plist|service|timer|db))\b", re.IGNORECASE)
MIN_FILE_SHARED = 6
FILE_SCORE = 0.3


def code_files(title: str) -> frozenset[str]:
    """File basenames a title names (``tests/run_ci.py`` and ``run_ci.py`` are both ``run_ci.py``)."""
    return frozenset(m.lower() for m in _CODE_FILE.findall(title or ""))


def _file_reason(why: str) -> bool:
    return why.startswith("titles name ")


# Target identity (overlap audit t_01a189c70884fa59). Shared title words say two cards are the same KIND of work;
# they do not say it is the same target. 108 refusals in 24 h matched on boilerplate while each title named a
# different machine, process or repo: ``David-MBP: gateway 78233 may run stale hermes-mods plugins`` vs
# ``daveey-mini: gateway 16839 may run stale hermes-mods plugins``, a mini probe leg vs its MacBook leg,
# ``BB-1155 ... mini`` vs ``BB-1197, BB-1202, BB-1205 ... metta3``. A class only decides when BOTH cards carry it.
# Ticket numbers are deliberately NOT a class: repeat filings of one failing check (``BB-434: Upkeep failing:
# assets-health`` and ``BB-463: Upkeep failing: assets-health``) are the duplicates this check exists for.
_HOST_ALIASES = {
    "macbook": "macbook", "mbp": "macbook", "david-mbp": "macbook", "test-key-gen": "macbook",
    "mini": "mini", "daveey-mini": "mini", "mac-mini": "mini", "davids-mac-mini-1": "mini",
    "daveey-mini-1": "mini",  # the same machine's Board gateway name since its in-place rename from ``mini``
    "hermes-hub": "hub",
}
# Bare "hub" is not a host here: it is also the BB hub (on the mini) and "for hub t_..." quotes a card. A host is a
# whole token: "mini-batch" or a device "sx-heart-room-mini" names no machine, and worker-N is a lane, not a host.
# gpuN is a device index on one machine, not a machine. ``metta-3`` is ``metta3``.
_HOST_PATTERN = re.compile(r"(metta|bb-linux)-?(\d+)")
_TASK_REF = re.compile(r"(?:\b[a-z][\w.-]{0,40}[/ ](?:[\w-]{1,40}/)?)?\bt_[0-9a-f]{8,16}\b")
# A process lifetime: ``gateway 78233``, ``pid 1722``. A port is not a process: ``gateway 8080 port`` nor
# ``hermes-gateway:8642`` (a number glued to the word by a colon is an address).
_PROCESS = re.compile(r"\b(?:gateway\s+(?:pid[\s=:#]*)?|pid[\s=:#]*)(\d{3,})\b(?![\s:]*port)")
_REVISION = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")
# A newer revision of the same check is the same owner's work, so a differing sha alone never makes cards
# distinct. It does when one title inverts the other's acceptance (da19695 "OWNER_ASK_KEYS removal"; the
# a2dadd6 probe expects them present): see :func:`_inverts`.
_REPO_OWNERS = ("daveey", "metta-ai", "nousresearch", "softmax-ai")
_REPO = re.compile(r"(?<![\w./~@-])(?:https?://)?(?:github\.com/)?((?:" + "|".join(_REPO_OWNERS)
                   + r")/[a-z0-9][\w.-]{0,99}?)(?=[.,;:)'`\"]{0,4}(?:\s|$)|/(?:actions|pull|issues|commit|tree|blob)\b)")
_INVERTS = frozenset(
    "remove removes removed removal removing revert reverts reverted drop drops dropped delete deletes deleted "
    "deletion disable disables disabled rollback".split())
_ARTICLES = frozenset({"the", "a", "an"})
_BODY_SCAN = 20000  # bodies can be huge; a repo named past this is not what the card is about
_WAIT = re.compile(r"\b(?:park(?:s|ed|ing)?|wait(?:s|ed|ing)?|blocked|depend(?:s|ing)?)\s+on\s+"
                   r"(?:[a-z][\w@-]{0,40}[ /]){0,2}(t_[0-9a-f]{8,16})\b", re.IGNORECASE)
_NEGATED = re.compile(r"(?:\bnot|n't|\bnever|\bno longer)\s+(?:(?:be|been|get|stay|keep)\s+)?$", re.IGNORECASE)


def target_identity(title: str, gateways: Iterable[str] = ()) -> dict[str, frozenset[str]]:
    """The targets a title names, by class: ``host``, ``process``, ``revision``."""
    low = _TASK_REF.sub(" ", (title or "").lower())
    hosts = set()
    dynamic = {str(g).lower() for g in (gateways or ()) if g and str(g).lower() not in {"hub", "local"}}
    for tok in re.findall(r"[a-z0-9][a-z0-9.-]*[a-z0-9]|[a-z0-9]", low):
        if tok in _HOST_ALIASES:
            hosts.add(_HOST_ALIASES[tok])
        elif tok in dynamic:
            hosts.add(tok)
        elif m := _HOST_PATTERN.fullmatch(tok):
            hosts.add(m.group(1) + m.group(2))
    return {"host": frozenset(hosts), "process": frozenset(_PROCESS.findall(low)),
            "revision": frozenset(_REVISION.findall(low))}


def repos(text: str) -> frozenset[str]:
    """GitHub ``owner/repo`` slugs, bare or in a URL (``daveey/hermes-mods`` is not ``daveey/hermes``)."""
    return frozenset(_REPO.findall((text or "")[:_BODY_SCAN].lower()))


def _repo_of(title: str, body: str) -> tuple[frozenset[str], bool]:
    """A card's repo and whether its title names it: the title's slugs, else the body's when it names exactly one,
    at least twice (the bb_bridge card "Main CI red since effa0d25f: repair the hub suite on main" names
    daveey/babybear in its run URL and its PR flow). One "see Metta-AI/metta" aside is not the card's repo."""
    named = repos(title)
    if named:
        return named, True
    found = _REPO.findall((body or "")[:_BODY_SCAN].lower())
    in_body = frozenset(found)
    return (in_body, False) if len(in_body) == 1 and len(found) >= 2 else (frozenset(), False)


def _inverts(title: str, other: str) -> bool:
    """``title`` inverts what ``other`` checks: an inversion word right next to a word the other title names
    (``OWNER_ASK_KEYS removal`` vs ``... OWNER_ASK_KEYS)``), and ``other`` inverts nothing. An inversion word
    elsewhere (``since 2bc71d8f: drop flaky test``) inverts nothing the other card checks."""
    theirs = words(other)
    if theirs & _INVERTS:
        return False
    toks = re.findall(r"[a-z0-9]+", (title or "").lower())

    def neighbour(i: int, step: int) -> Optional[str]:
        i += step
        while 0 <= i < len(toks) and toks[i] in _ARTICLES:
            i += step
        tok = toks[i] if 0 <= i < len(toks) else None
        return None if tok is None or tok in _STOP or _REVISION.fullmatch(tok) else tok

    return any(tok in _INVERTS and ({neighbour(i, -1), neighbour(i, 1)} & theirs)
               for i, tok in enumerate(toks))


def waits_on(card_id: str, title: str, body: str) -> bool:
    """The new card says it parks, waits, is blocked or depends on ``card_id``: a waiter is not a duplicate of
    the producer it waits on (hub 10-30 14:19:41Z, overridden 4 s later). A negated wait (``do not wait on``)
    is not one."""
    if not card_id:
        return False
    text = f"{title or ''}\n{(body or '')[:_BODY_SCAN]}"
    return any(m.group(1).lower() == card_id.lower() and not _NEGATED.search(text[max(0, m.start() - 20):m.start()])
               for m in _WAIT.finditer(text))


# Work role (audits t_01a189b2d186a986, t_01a19075b3f1531a). Shared board and vocabulary say two cards are about the
# same campaign; they do not say they do the same JOB. "Repair Stage B evidence gaps" (implements guards) was refused
# against "Follow-through: send the verified Stage C report", and "bb-tuner burn-down: 7-day re-measure ..." against
# four bb cluster fix/design cards. Roles are deliberately few and explicit; a title naming two roles ("Send
# failing: fix") has none, and an unknown role never makes cards distinct, so a vague title still meets the
# word-overlap rules. Verifying is NOT a role apart from implementing: "Verify green main CI: daveey/hermes" owns
# making main green, so "Fix red main CI: daveey/hermes" is its duplicate (hub 10-30 02:31:55Z, judged correct).
# Verbs only: "delivery", "measurement" and "implementation" are what a symptom is ABOUT ("Delivery receipts
# missing"), not a job.
_ROLES = {
    "implement": "fix fixes fixing repair repairs repairing implement implements implementing design redesign",
    "measure": "measure measures measuring re-measure remeasure",
    "deliver": "follow-through send sends sending deliver delivers delivering notify notifies relay relays",
}
_ROLE_OF = {w: role for role, ws in _ROLES.items() for w in ws.split()}
_ROLE_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # "failed-fix" and "bb-self-fix" are not the verb "fix"
_ROLE_NEGATORS = frozenset({"not", "no", "never", "without", "nor", "don", "dont", "doesn", "isn", "won"})
_ROLE_CLAUSE = re.compile(r"[.;:!?,\n]+")  # negation never reaches across a clause ("No. Fix the gate")
_ROLE_BODY_SCAN = 600  # the opening states the job; later sections ("DELIVERY: your own worktree") are steps
_ROLE_VERB_SLOTS = 1  # a title's job is the first word of a clause: in "Discord send retries drop" send is a noun
_ROLE_NOT_A_WORD = re.compile(r"\d")  # "7-day re-measure", "BB-1156: fix": numbers and ids precede the verb
# Only "implement" is read from a body: symptom-titled cards say "smallest fix" there, while "notify David when
# green" or "measure before and after" in a body is a closing step of nearly any card, not its job.
_BODY_ROLES = frozenset({"implement"})


def _role_tokens(text: str, *, verb_slots: Optional[int] = None) -> list[tuple[str, bool]]:
    """``(role, negated)`` per role word, in order, clause by clause. ``not a second verification campaign`` is
    negated. With ``verb_slots`` only the first N words of each clause count (titles); numbers and ids skipped."""
    out: list[tuple[str, bool]] = []
    for clause in _ROLE_CLAUSE.split(_TASK_REF.sub(" ", (text or "").lower())):
        toks = _ROLE_TOKEN.findall(clause)
        if verb_slots is not None:
            toks = [t for t in toks if not _ROLE_NOT_A_WORD.search(t)]
        out += [(_ROLE_OF[t], bool(_ROLE_NEGATORS & set(toks[max(0, i - 3):i]))) for i, t in enumerate(toks)
                if t in _ROLE_OF and (verb_slots is None or i < verb_slots)]
    return out


def _one_role(tokens: list[tuple[str, bool]]) -> tuple[Optional[str], bool]:
    """``(role, ambiguous)``: the role every unnegated role word agrees on; two roles are ambiguous."""
    named = {role for role, neg in tokens if not neg}
    return (next(iter(named)) if len(named) == 1 else None), len(named) > 1


def work_role(title: str, body: str = "") -> tuple[Optional[str], Optional[str]]:
    """``(role, source)``: the one role the title names in verb position (the first words of a clause; source
    ``title``), else the one role the body's opening names (source ``body``: bb_bridge cards title the failure and
    put "smallest fix" in the body), else ``(None, None)``. Text naming two roles is ambiguous and has none, and an
    ambiguous title never falls back to its body."""
    role, ambiguous = _one_role(_role_tokens(title, verb_slots=_ROLE_VERB_SLOTS))
    if role or ambiguous:
        return (role, "title") if role else (None, None)
    role, _ = _one_role(_role_tokens((body or "")[:_ROLE_BODY_SCAN]))
    return (role, "body") if role in _BODY_ROLES else (None, None)


# A recovery card is ABOUT a card's failed execution: the self-heal lane files "self-heal: kanban card t_X on mini/bb
# failed 2x (crashed,crashed): <t_X's title>" (coach signals.collect_kanban). It names t_X's board and repeats its
# title, so it matched t_X itself and was refused 8 times (hub 11-01 00:31-01:21Z). Diagnosing why t_X's worker
# died is not a second owner of t_X's work; another recovery card for t_X, or a card redoing t_X's work, still is.
_RECOVERS = re.compile(r"\bcards?\s+(t_[0-9a-f]{8,16})\b(?:\+\d+)?(?:\s+on\s+[\w./-]+)?\s+(?:failed|crashed|stalled|"
                       r"timed[ _]out|gave up|got stuck|is stuck|lost its worker)\b", re.IGNORECASE)
# ... unless it says it does t_X's work itself: "card t_X crashed, take over" is a second owner of that work.
_REDOES = re.compile(r"\b(?:take[ -]?over|takes over|taking over|redo|re-do|retry|re-run|rerun|resume|continue|"
                     r"finish|refile|re-file|replace|replaces|instead of)\b", re.IGNORECASE)


def recovers(card_id: str, title: str, card_title: str = "") -> bool:
    """The new title is a recovery of ``card_id``'s failed execution (``card t_X ... failed|crashed|...``) and does
    not say it redoes the work (``take over``, ``retry``, ``finish``...). Text after the next colon that quotes
    ``card_title`` (self-heal appends the source title, cut short) is the source's words, not read for that."""
    title = title or ""
    for m in _RECOVERS.finditer(title):
        if card_id and m.group(1).lower() == card_id.lower():
            head, _, quoted = title[m.end():].partition(":")
            quoted = quoted.strip().lower()
            own = title[:m.end()] + head if quoted and (card_title or "").strip().lower().startswith(quoted) \
                else title
            return not _REDOES.search(own)
    return False


def _identity(title: str, body: str, gateways: Iterable[str]) -> dict:
    repo, in_title = _repo_of(title, body)
    role, role_src = work_role(title, body)
    return {"title": title or "", **target_identity(title, gateways), "repo": repo, "repo_in_title": in_title,
            "role": role, "role_src": role_src}


def _distinct(a: dict, b: dict) -> Optional[str]:
    # A repo only in both BODIES is a passing mention ("see daveey/hermes-mods for prior art"): one title must
    # name it.
    if a["repo"] and b["repo"] and not a["repo"] & b["repo"] and (a["repo_in_title"] or b["repo_in_title"]):
        return f"repo {'/'.join(sorted(a['repo']))} != {'/'.join(sorted(b['repo']))}"
    for cls in ("host", "process"):
        if a[cls] and b[cls] and not a[cls] & b[cls]:
            return f"{cls} {'/'.join(sorted(a[cls]))} != {'/'.join(sorted(b[cls]))}"
    if (a["revision"] and b["revision"] and not a["revision"] & b["revision"]
            and (_inverts(a["title"], b["title"]) or _inverts(b["title"], a["title"]))):
        return f"revision {'/'.join(sorted(a['revision']))} inverts {'/'.join(sorted(b['revision']))}"
    # A body-only role is a weak read (template text): it decides only against a role a title states. A role whose
    # words the other title also uses is that card's subject, not a second job: "Send button disabled in BB
    # client" (deliver?) vs "Fix send button disabled in BB client" is one defect.
    if (a.get("role") and b.get("role") and a["role"] != b["role"]
            and "title" in (a.get("role_src"), b.get("role_src"))
            and not _role_words_in(a["role"], b["title"]) and not _role_words_in(b["role"], a["title"])):
        return f"role {a['role']} != {b['role']}"
    return None


def _role_words_in(role: str, title: str) -> bool:
    return any(_ROLE_OF.get(t) == role for t in _ROLE_TOKEN.findall((title or "").lower()))


def distinct_target(new_title: str, card_title: str, gateways: Iterable[str] = (), *, new_body: str = "",
                    card_body: str = "") -> Optional[str]:
    """Why two cards name different targets (a repo, a machine, a process lifetime, an inverted check at another
    revision), else None. Same-target pairs, and pairs where either side names no target in a class, are left to
    the word-overlap rules."""
    gws = tuple(gateways or ())
    return _distinct(_identity(new_title, new_body, gws), _identity(card_title, card_body, gws))


def _board_files() -> Iterable[tuple[str, Path]]:
    """``(board, db file)`` for each board, each file once, named after the board that owns it.

    Board paths resolve through ``kanban_db_path``, so a fenced worker stays on its ``HERMES_KANBAN_DB`` pin
    (workers do not read other boards). Under that pin every slug resolves to the one pinned file: read once
    per board, its cards came back once per board, each copy labelled with another board's slug (hub 10-30
    08:04:58Z and 12:05:05Z: one default card listed 7 times, as default/askeve/bb/runners/...)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_boards import list_boards

    def real(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path

    slugs = [meta.get("slug") or kb.DEFAULT_BOARD for meta in list_boards(include_archived=False)]
    owner = {real(kb.board_own_db_path(slug)): slug for slug in slugs}
    pinned = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or kb.DEFAULT_BOARD
    read: set[Path] = set()
    for slug in slugs:
        path = kb.kanban_db_path(board=slug)
        key = real(path)
        if key in read:
            continue
        read.add(key)
        yield owner.get(key) or (pinned if key != real(kb.board_own_db_path(slug)) else slug), path


def _board_cards(c: sqlite3.Connection, slug: str, self_gw: Optional[str], *,
                 since: Optional[tuple[int, int]] = None) -> list[dict]:
    """Open cards of the board ``c`` reads, as :func:`find` compares them. ``since`` (``(rowid, epoch seconds)``)
    keeps only cards inserted after that rowid or created at or after that time: the in-transaction re-check
    reads just what landed since the first check. The rowid is exact: ``created_at`` is stamped before
    ``create_task`` waits for the write lock, so a slow concurrent create can commit a row older than the check."""
    marks = ",".join("?" * len(OPEN))
    newer = " AND (t.rowid > ? OR t.created_at >= ?)" if since is not None else ""
    # ``placed_as``: the card on another gateway this one was forwarded to (deferred placement); ``awaits``: the
    # card its latest park waits on. Either binds this card's completion to that card (:func:`_bound_to`).
    rows = c.execute(
        "SELECT t.id, t.title, t.body, t.status, t.assignee, t.created_at, t.session_id, "
        "(SELECT json_extract(e.payload, "
        "'$.creator_task_id') FROM task_events e WHERE e.task_id = t.id AND e.kind = 'created' "
        "LIMIT 1), t.idempotency_key, (SELECT json_extract(e.payload, '$.remote_task_id') FROM task_events e "
        "WHERE e.kind = 'placement_forwarded' AND e.task_id = t.id AND json_extract(e.payload, "
        "'$.remote_task_id') IS NOT NULL ORDER BY e.id DESC LIMIT 1), (SELECT json_extract(e.payload, "
        "'$.awaiting.ref') FROM task_events e WHERE e.kind IN ('scheduled', 'continued') AND e.task_id = t.id "
        f"ORDER BY e.id DESC LIMIT 1) FROM tasks t WHERE t.status IN ({marks}){newer}",
        (*OPEN, *(since or ()))).fetchall()
    if not rows:
        return []
    try:
        links = c.execute("SELECT task_id, chat_session FROM task_chat_links WHERE released = 0").fetchall()
    except sqlite3.OperationalError:
        links = []
    chats: dict[str, set[str]] = {}
    for tid, sess in links:
        chats.setdefault(tid, set()).add(sess)
    return [{"id": tid, "board": slug, "gateway": self_gw or "local", "status": status, "title": title,
             "body": body or "", "assignee": assignee, "created_at": created_at,
             "session_id": sess, "creator_task_id": creator, "local": True, "key_family": key_family(key),
             "has_key": bool(key),
             "placed_as": placed if isinstance(placed, str) else None,
             "awaits": awaits if isinstance(awaits, str) and status == "scheduled" else None,
             "chats": sorted(chats.get(tid, set()) | ({sess} if sess else set()))}
            for tid, title, body, status, assignee, created_at, sess, creator, key, placed, awaits in rows]


def _local_cards(self_gw: Optional[str]) -> Iterable[dict]:
    from hermes_cli.kanban_session_activity import cached_ro
    for slug, path in _board_files():
        if not path.exists():
            continue
        try:
            with cached_ro(path) as c:
                cards = _board_cards(c, slug, self_gw)
        except sqlite3.Error as exc:
            _log.warning("kanban create overlap: board %s unreadable, skipped (%s)", slug, exc)
            continue
        yield from cards


def _replica_cards(self_gw: Optional[str]) -> Iterable[dict]:
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli.kanban_session_activity import cached_ro
    path = reg.hub_db_path()
    if not path.exists():
        _log.info("kanban create overlap: no hermes-hub replica at %s; other gateways not checked", path)
        return
    marks = ",".join("?" * len(OPEN))
    try:
        with cached_ro(path) as c:
            rows = c.execute(f"SELECT gw, key, data FROM replica WHERE coll = 'tasks' AND gw != ? "
                             f"AND json_extract(data, '$.status') IN ({marks})", (self_gw or "", *OPEN)).fetchall()
            keys = c.execute("SELECT gw, key FROM replica WHERE coll = 'tasks' AND gw != ?",
                             (self_gw or "",)).fetchall()
    except sqlite3.Error as exc:
        _log.warning("kanban create overlap: replica %s unreadable, other gateways skipped (%s)", path, exc)
        return
    # A legacy ``<board>/<id>`` row is a frozen pre-profile snapshot: t_d6973c6f was done under
    # ``default/hermes/t_d6973c6f`` and still "running" under ``hermes/t_d6973c6f``. When a qualified
    # ``<profile>/<board>/<id>`` twin exists it is the card's state, so the legacy row never counts.
    qualified = {(gw, str(key).split("/", 1)[1]) for gw, key in keys if str(key).count("/") == 2}
    seen = set()
    for gw, rkey, raw in rows:
        if str(rkey).count("/") == 1 and (gw, str(rkey)) in qualified:
            continue
        try:
            t = json.loads(raw)
        except ValueError:
            continue
        key = (gw, t.get("board"), t.get("id"))
        if not t.get("id") or key in seen:
            continue
        seen.add(key)
        sessions = [s for s in (t.get("sessions") or []) if isinstance(s, dict)]
        yield {"id": t["id"], "board": t.get("board") or "default", "gateway": t.get("gateway") or gw,
               "status": t.get("status"), "title": t.get("title") or "", "body": t.get("body") or "",
               "assignee": t.get("assignee") if isinstance(t.get("assignee"), str) else None,
               "local": False,
               "session_id": next((s.get("session") for s in sessions if s.get("rel") == "created"), None),
               # hermes-hub client >= 1.9.27 publishes the key; per-host automation keyed by one creator on two
               # gateways is then exempt like a local same-family card (cron-d7cb391a4c). Older publishers: None.
               "creator_task_id": None,
               "key_family": key_family(ikey if isinstance(ikey := t.get("idempotency_key"), str) else None),
               "chats": sorted({str(s["session"]) for s in sessions if s.get("session")})}


def key_family(key: Optional[str]) -> Optional[str]:
    """An idempotency key's family: the text before the first ':' (``coach-fix:<slug>`` -> ``coach-fix``), else
    the whole key. One automated creator (coach / self-heal cron) keys all its cards in one family."""
    key = (key or "").strip()
    return key.split(":", 1)[0] if key else None


_OWNER_TITLE_RE = re.compile(
    r"^Capability owner: \S+ for [A-Za-z0-9._-]+/(t_[0-9a-f]{8,16}) \(")
_OWNER_BODY_RE = re.compile(
    r"^Capability owner card for waiting card \S+/(t_[0-9a-f]{8,16})\b")


def capability_owner_waiting_id(title: str, body: str) -> Optional[str]:
    """The waiting card a capability-owner create quotes, when the title and body name the same id.

    The owner title embeds that card's title, so a word-overlap check matches the card the owner
    exists to serve (hub t_01a18669308f507f). Both shapes must agree, so a title that merely starts
    with the prefix does not skip anything.
    """
    title_m = _OWNER_TITLE_RE.match(title or "")
    body_m = _OWNER_BODY_RE.match(body or "")
    if title_m and body_m and title_m.group(1) == body_m.group(1):
        return title_m.group(1)
    return None


def _bound_to(card: dict, waiting_id: str) -> bool:
    """True when ``card`` completes only through ``waiting_id``: it was forwarded there by deferred placement
    (``placement_forwarded``, the origin placeholder the placed card's body names), or it is parked on it.

    Such a card waits on the waiting card by construction, so it can never do the capability owner's scoped
    action: counting it as an owner refused every owner create a placed card asked its origin gateway for
    (hub t_01a1d49003c65ca4, macbook placeholder t_01a1d48f676074bc).
    """
    # ``awaits`` is the park's bare awaiting.ref; awaiting.gateway is ignored. This relies on task ids being
    # globally unique: a card on another gateway with this id is the same card, not a namesake.
    return waiting_id in (card.get("placed_as"), card.get("awaits"))


def _sibling_owner(card_title: str, waiting_id: str) -> bool:
    found = _OWNER_TITLE_RE.match(card_title or "")
    return bool(found and found.group(1) == waiting_id)


def find(*, title: str, body: str, target_board: str, session_id: Optional[str], self_tid: Optional[str],
         parents: Iterable[str], family: Optional[str] = None) -> list[dict]:
    """Open cards elsewhere that already cover this work, best match first. Never raises. ``family`` (the new
    card's idempotency-key family) skips cards from the same keyed creator, except singleton jobs and the
    creator task's own keyed cards from an earlier run (:func:`kanban_overlap_own.earlier_run`)."""
    try:
        from hermes_cli import kanban_capability_registry as reg
        return _find(title=title, body=body, target_board=target_board, session_id=session_id, self_tid=self_tid,
                     parents=parents, family=family, self_gw=reg.self_gateway())[0]
    except Exception:  # health: allow BLE001 -- a broken check must never block creating a card
        _log.warning("kanban create overlap check failed; creating without it", exc_info=True)
        return []


def _find(*, title: str, body: str, target_board: str, session_id: Optional[str], self_tid: Optional[str],
          parents: Iterable[str], family: Optional[str] = None, self_gw: Optional[str] = None,
          cards: Optional[list[dict]] = None, gateways: Optional[set[str]] = None,
          assignee: Optional[str] = None, run_started: Optional[int] = None,
          shared_code: bool = True) -> tuple[list[dict], set[str]]:
    """:func:`find` over ``cards`` (default: every local board and the replica) -> ``(matches, gateways)``.
    ``gateways`` (default: those the cards name) feeds the board-reference and host rules; the in-transaction
    re-check passes the first check's set so both reads judge a card the same way. Raises on a broken read.
    A card ``self_tid`` created is compared too (:mod:`hermes_cli.kanban_overlap_own`): ``assignee`` feeds its
    admits, ``run_started`` (the creator's current run) its same-run re-issue, and ``shared_code`` its shared
    fix-site match (off for the in-transaction re-check, which reads too few cards to tell a rare identifier)."""
    skip = set(parents) | ({self_tid} if self_tid else set())
    waiting = capability_owner_waiting_id(title, body)
    new_words = words(title)
    job = singleton_job(title)
    failure = failure_identity(title, body)
    out, seen, mine = [], set(), None
    common: Optional[frozenset[str]] = None
    if cards is None:
        cards = [*_local_cards(self_gw), *_replica_cards(self_gw)]
    if gateways is None:
        gateways = {g for g in (self_gw, *(c["gateway"] for c in cards)) if g}
        if gateways & {"hub", "hermes-hub"}:
            gateways |= {"hub", "hermes-hub"}
    for card in cards:
        singleton = bool(job and job == singleton_job(card.get("title") or ""))
        if (card["gateway"], card["id"]) in seen:
            continue
        # The capability owner executes this exact waiting card, even for a singleton.
        # Keep other mirror owners visible; do not exempt a whole family or envelope.
        if waiting and (card["id"] == waiting or _bound_to(card, waiting)):
            continue
        own = bool(self_tid and card.get("creator_task_id") == self_tid)
        if not singleton and (card["id"] in skip
                or (waiting and _sibling_owner(card.get("title") or "", waiting))
                or (session_id and card["session_id"] == session_id and not own)
                or (family and card.get("key_family") == family
                    and not (own and _own.earlier_run(card, run_started)))):
            continue
        seen.add((card["gateway"], card["id"]))
        reasons, score = _reasons(card, new_words, title, body, target_board, gateways, failure)
        if not reasons and own and shared_code:
            score = _own.title_score(new_words, words(card.get("title") or ""))
            if common is None:
                common = _own.common_ids(_own.code_ids(title, body), cards)
            why = _own.shared_code_reason(title, body, card, common, score)
            reasons = [why] if why else []
        if not reasons:
            continue
        if mine is None:
            mine = _identity(title, body, gateways)
        admit = (f"waits on {card['id']}" if waits_on(card["id"], title, body)
                 else f"recovers {card['id']}" if recovers(card["id"], title, card.get("title") or "")
                 # A failure- or file-only match the new card cites by id was written knowing that owner.
                 else f"cites {card['id']}" if (all(_failure_reason(r) or _file_reason(r) or _own.is_reason(r)
                                                    for r in reasons)
                                                and cites(card["id"], title, body))
                 else _distinct(mine, _identity(card.get("title") or "", card.get("body") or "", gateways)))
        if own and not admit:
            admit = _own.own_distinct(title, assignee, card)
        if admit and not singleton:
            # Logged so an audit can tell an admitted look-alike from a missed one.
            _log.info("kanban create overlap: admitted %r despite %s/%s/%s (%s): %s", title, card["gateway"],
                      card["board"], card["id"], "; ".join(reasons), admit)
        else:
            out.append({**{k: card[k] for k in ("id", "board", "gateway", "status", "title", "chats")},
                        "why": "; ".join(reasons), "_score": score, "_local": card["local"],
                        "_reissue": own and not any(_own.is_reason(r) for r in reasons)
                        and _own.reissue(card, self_tid, run_started, score)})
    out.sort(key=lambda m: (-m["_score"], m["status"] not in ("running", "ready", "scheduled"), m["id"]))
    return out, gateways


def public(matches: list[dict]) -> list[dict]:
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in matches[:MAX_SHOWN]]


def _retry_ask(entry: str, uncovered: list[str]) -> str:
    if entry == "cli":
        flags = " ".join(f"--distinct-from {i}" for i in uncovered)
        return f"run `hermes kanban create` again with {flags} --distinct-reason '<why it is separate>'"
    if entry == "rest":
        return (f"POST again with \"distinct_from\": {json.dumps(uncovered)} and "
                "\"distinct_reason\": \"<why it is separate>\"")
    return f"call kanban_create again with distinct_from={uncovered} and distinct_reason='<why it is separate>'"


def refusal(matches: list[dict], distinct_from: list[str], reason: str, entry: str = "tool") -> dict:
    shown = public(matches)
    uncovered = [m["id"] for m in shown if m["id"] not in distinct_from]
    ask = ("pass distinct_reason (why this is separate work) with distinct_from" if distinct_from and not reason
           else _retry_ask(entry, uncovered))
    named = ", ".join(f"{m['id']} on {m['gateway']}/{m['board']} ({m['status']})" for m in shown[:3])
    note = (f"No card was created: {len(matches)} open card(s) already look like this work ({named}). "
            "Count owners by board/project, not by your chat's links: continue the existing card "
            f"(read it, comment on it, or message its chat), or if this really is separate work {ask}.")
    return {"ok": False, "created": False, "code": EVENT, "matches": shown, "total_matches": len(matches),
            "note": note}


def covered(matches: list[dict], distinct_from: list[str], reason: str) -> bool:
    return bool(reason.strip()) and all(m["id"] in distinct_from for m in matches[:MAX_SHOWN])


DEDUP_CODE = "create_deduplicated"


def reissued(matches: list[dict]) -> Optional[dict]:
    """The creator's own keyless card from this same run that the new create re-issues, if one matched."""
    return min((m for m in matches if m.get("_reissue")), key=lambda m: m["id"], default=None)


def deduplicated(card: dict) -> dict:
    """The answer to a same-run re-issue: the first card, nothing created (:func:`kanban_overlap_own.reissue`)."""
    return {"ok": True, "created": False, "deduplicated": True, "code": DEDUP_CODE, "task_id": card["id"],
            "note": f"No card was created: you already created {card['id']} ({card['status']}) for this in this "
                    "run. Use that card. If this really is separate work, call kanban_create again with "
                    f"distinct_from=['{card['id']}'] and distinct_reason='<why it is separate>'."}


def verdict(matches: list[dict], distinct_from: list[str], reason: str, entry: str = "tool") -> dict:
    """Refusal JSON for uncovered ``matches``, or the dedup answer when one is a same-run re-issue."""
    first = reissued(matches)
    return deduplicated(first) if first else refusal(matches, distinct_from, reason, entry)


def print_cli(refused: str, *, as_json: bool) -> int:
    """``hermes kanban create``'s answer to :func:`check`'s JSON: a refusal prints JSON (``--json``) or a summary on
    stderr, exit code 2; a same-run re-issue (``deduplicated``) prints the first card's id, exit code 0."""
    import sys
    ref = json.loads(refused)
    if ref.get("deduplicated"):
        print(refused if as_json else ref["task_id"])
        if not as_json:
            print(f"kanban: {ref['note']}", file=sys.stderr)
        return 0
    if as_json:
        print(refused)
        return 2
    print(f"kanban: {ref['code']}: {ref['note']}", file=sys.stderr)
    for m in ref["matches"]:
        print(f"  {m['id']}  {m['gateway']}/{m['board']}  ({m['status']})  {m['title']}  — {m['why']}",
              file=sys.stderr)
    return 2


def log_path():
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "logs" / LOG_NAME


def _append_log(payload: dict) -> None:
    """One JSON line for a refusal that has no board to hold an event (every match is on another gateway).
    Past ``LOG_MAX_BYTES`` the file is renamed to ``.1`` (one generation kept), so the record is bounded."""
    import time
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    line = json.dumps({"ts": time.time(), **payload}, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def record(conn: Optional[sqlite3.Connection], task_id: Optional[str], *, outcome: str, title: str,
           matches: list[dict], reason: str = "", session_id: Optional[str] = None) -> None:
    """``create_overlap`` event (the detection signal): on the new card for an override; for a refusal on the
    creator's own card, else on the best LOCAL match's board. Never raises."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    payload = {"outcome": outcome, "title": title, "reason": reason or None, "session_id": session_id,
               "matches": [{"id": m["id"], "board": m["board"], "gateway": m["gateway"], "status": m["status"],
                            "why": (m.get("why") or "")[:MAX_WHY]} for m in public(matches)]}
    _log.info("kanban create overlap: %s %r (%d matches: %s)", outcome, title, len(matches),
              ", ".join(f"{m['gateway']}/{m['board']}/{m['id']} ({m['why']})" for m in matches[:MAX_SHOWN]))
    try:
        if conn is not None and task_id:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, EVENT, payload)
            return
        local = next((m for m in matches if m["_local"]), None)
        if local is None:
            _append_log(payload)
            return
        with kbc.connect_closing(board=local["board"]) as other, kb.write_txn(other):
            kb._append_event(other, local["id"], EVENT, payload)
    except Exception:  # health: allow BLE001 -- the event is telemetry; the refusal itself already happened
        _log.warning("kanban create overlap: could not record the %s event", outcome, exc_info=True)


class OverlapRefused(Exception):
    """The in-transaction re-check found an open card that landed after the first check: the create rolls back.

    The entry catches it outside the rolled-back transaction and calls :meth:`settle`, which records the
    ``create_overlap`` event and returns the same refusal JSON the first check returns."""

    def __init__(self, settle: Callable[[], str]):
        super().__init__(EVENT)
        self.settle = settle


class AfterCreate:
    """``after_create(conn, new_task_id)`` records an accepted ``distinct_from`` override on the new card (a no-op
    when nothing overlapped). :attr:`guard` (None when the check did not run) goes to ``kb.create_task(...,
    insert_guard=...)``: it re-runs the match inside the INSERT's ``BEGIN IMMEDIATE`` transaction, against open
    cards of that board created since the first check began, and raises :class:`OverlapRefused` on a hit.

    Two creates of one failure that both passed the first check before either inserted (11-03: a worker's
    ``kanban_create`` and ci-main-watch's ``hermes kanban create``, 186 ms apart) serialize on the board's write
    lock, so the second one sees the first one's row here. Only this board is re-read under the lock: a card that
    lands on another board or gateway in the same window is still a documented race."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None, *, since: Optional[tuple[int, int]] = None,
                 board: str = "", title: str = "", body: str = "", target_board: str = "",
                 session_id: Optional[str] = None,
                 self_tid: Optional[str] = None, parents: Iterable[str] = (), family: Optional[str] = None,
                 self_gw: Optional[str] = None, gateways: Optional[set[str]] = None, matches: Iterable[dict] = (),
                 distinct: Iterable[str] = (), reason: str = "", own: bool = False, entry: str = "tool",
                 assignee: Optional[str] = None, run_started: Optional[int] = None):
        self._conn, self._since, self._board, self._title, self._body = conn, since, board, title, body
        self._find_kw = {"title": title, "body": body, "target_board": target_board, "session_id": session_id,
                         "self_tid": self_tid, "parents": list(parents), "family": family, "self_gw": self_gw,
                         "assignee": assignee, "run_started": run_started}
        self._gateways, self._matches = gateways, list(matches)
        self._distinct, self._reason, self._own, self._entry = list(distinct), reason, own, entry
        self._session_id, self._self_tid = session_id, self_tid

    def __call__(self, conn: sqlite3.Connection, task_id: str) -> None:
        if self._matches:
            record(conn, task_id, outcome="override", title=self._title, matches=self._matches,
                   reason=self._reason, session_id=self._session_id)

    @property
    def guard(self) -> Optional[Callable[[sqlite3.Connection], None]]:
        return self._recheck if self._since is not None else None

    def _recheck(self, conn: sqlite3.Connection) -> None:
        try:
            cards = _board_cards(conn, self._board, self._find_kw["self_gw"], since=self._since)
            matches = (_find(**self._find_kw, cards=cards, gateways=self._gateways, shared_code=False)[0]
                       if cards else [])
        except Exception:  # health: allow BLE001 -- a broken re-check must never block creating a card
            _log.warning("kanban create overlap: in-transaction re-check failed; creating without it", exc_info=True)
            return
        known = {m["id"] for m in self._matches}
        fresh = [m for m in matches if m["id"] not in known]
        if not fresh:
            return
        if covered(fresh, self._distinct, self._reason):
            self._matches += fresh
            return
        _log.info("kanban create overlap: %r refused under the write lock: %s landed after the first check",
                  self._title, ", ".join(m["id"] for m in fresh))

        def settle() -> str:
            out = verdict(fresh, self._distinct, self._reason, self._entry)
            record(self._conn if self._own else None, self._self_tid,
                   outcome="deduplicated" if out.get("deduplicated") else "refused", title=self._title,
                   matches=fresh, session_id=self._session_id)
            return json.dumps(out)

        raise OverlapRefused(settle)


_no_override = AfterCreate()


def check(conn: sqlite3.Connection, args: dict, *, courier: bool, title: str, session_id: Optional[str],
          self_tid: Optional[str], parents: list[str], entry: str = "tool",
          ) -> tuple[Optional[str], AfterCreate]:
    """``(refusal_json, _)`` stops the create; otherwise ``(None, after_create)`` (:class:`AfterCreate`): pass
    ``after_create.guard`` as ``kb.create_task(insert_guard=...)`` and call ``after_create(conn, new_task_id)``
    once the card exists. A courier-applied card (``courier``) was already checked on the gateway that asked for
    it. ``entry`` (tool | cli | rest) only words the refusal's retry hint for the caller's entry path."""
    import time
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_capability_registry as reg
    if courier or not enabled():
        return None, _no_override
    key = args.get("idempotency_key")
    if key and conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
                            (key,)).fetchone():
        return None, _no_override  # a retry returns the existing card; nothing new is created
    # A card another process inserts from here on is invisible to this read; the guard re-reads them under the
    # write lock (rowid watermark; the time, one second earlier as ``created_at`` is whole seconds, also catches a
    # row whose rowid was reused after a delete).
    since = (conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM tasks").fetchone()[0], int(time.time()) - 1)
    distinct = [str(x) for x in (args.get("distinct_from") or [])]
    reason = str(args.get("distinct_reason") or "")
    body = str(args.get("body") or "")
    target_board = args.get("board") or kb.DEFAULT_BOARD
    own = bool(self_tid) and not args.get("board")  # the creator's card is on the board this call opened
    assignee = args.get("assignee") if isinstance(args.get("assignee"), str) else None
    find_kw = {"title": title, "body": body, "target_board": target_board, "session_id": session_id,
               "self_tid": self_tid, "parents": parents, "family": key_family(key),
               "assignee": assignee, "run_started": _run_started(conn, self_tid)}
    try:
        self_gw = reg.self_gateway()
        matches, gateways = _find(**find_kw, self_gw=self_gw)
    except Exception:  # health: allow BLE001 -- a broken check must never block creating a card
        _log.warning("kanban create overlap check failed; creating without it", exc_info=True)
        return None, _no_override
    # The board ``conn`` is (the tool opens the current board when none is named), labelled as the first read
    # labels it, so both reads judge a card and record its event the same way.
    board = args.get("board") or kb.get_current_board()
    pending = AfterCreate(conn, since=since, board=board, **find_kw, self_gw=self_gw, gateways=gateways, distinct=distinct,
                          reason=reason, own=own, entry=entry)
    if not matches:
        return None, pending
    if covered(matches, distinct, reason):
        pending._matches = list(matches)
        return None, pending
    out = verdict(matches, distinct, reason, entry)
    record(conn if own else None, self_tid, outcome="deduplicated" if out.get("deduplicated") else "refused",
           title=title, matches=matches, session_id=session_id)
    return json.dumps(out), _no_override


def _run_started(conn: sqlite3.Connection, self_tid: Optional[str]) -> Optional[int]:
    """When the creator's current run began (None outside a worker, or when the read fails)."""
    if not self_tid:
        return None
    try:
        row = conn.execute("SELECT r.started_at FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                           "WHERE t.id = ?", (self_tid,)).fetchone()
    except sqlite3.Error:
        return None
    return _own._epoch(row[0]) if row else None
