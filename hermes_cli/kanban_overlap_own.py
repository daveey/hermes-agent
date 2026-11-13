"""Same-creator duplicates for the create-overlap check (:mod:`hermes_cli.kanban_overlap`). [daveey]

Until 11-13, ``_find`` skipped every open card the creating worker had made itself ("fan-out"). So an owner
could file the same fix twice, and three times on 11-13 a second worker built a change its sibling already had:
- t_01a1d44c7b488318 / t_01a1d44d9e68cca4, 75 s apart, and t_01a1d4914914925a / t_01a1d4917584d038, 12 s apart.
  The creator archive-and-recreated a card to drop a placement marker, the archive was refused because the
  first card was already claimed, and the recreate went ahead anyway.
- t_01a1d462ff5c8698 / t_01a1d4e145f389a5, 2 h 18 m apart: a round-3 review nit filed without checking the
  open round-1 child that already owned ``_answer_interrupted_calls``.

A card from the same creator task now goes through the normal match. It is admitted when one of these says
the cards are different work (:func:`own_distinct`):
- another assignee;
- one title is a review and the other is not;
- different leading component tags (``[core]`` vs ``[mods]``, ``Board:`` vs ``Core:``);
- the titles are an enumeration (:func:`enumerated`): in the same place of both titles each carries an index
  marker (a digit token or a lone capital letter) and the markers differ (``w1-01``/``w1-02``,
  ``worker-2``/``worker-3``, ``1/3``/``3/3``, fixture A/B, two shas). A marker only one title has (an added
  ``round 2`` or sha) is a re-issue, not a leg.

Two cards from one owner that name the same rare code identifiers share a fix site even when their titles are
worded apart, as the PR #2062 pair did (title ratio 0.41). That is :func:`shared_code_reason`: at least
``MIN_IDS`` snake_case identifiers in both cards, each named by at most ``RARE_MAX`` open cards (``reviewed_sha``,
``kanban_complete`` and other template words are in hundreds), and titles sharing ``IDS_SCORE`` of their words.

The replay of 7 days of hub cards had 1654 same-creator pairs where the first card was open when the second was
created. Through this code the rule refuses 11 of them:
- the 3 duplicates above;
- 7 real or partial duplicates (one exact-head review filed twice, two readbacks each filed twice, two
  review-nit rounds that overlapped, ...);
- 1 false refusal (two "Fixture: ... pickup" cards), which ``distinct_from`` overrides.

Removing the exemption outright would have refused 517 pairs.

A card from the same chat but another creator (the session match) keeps the old exemption. Every card in a chat's
tree inherits that session, and the 10-11 race of two creators is another cause.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable, Optional

MIN_IDS = 3
RARE_MAX = 2  # open cards (besides the new one) that may name an identifier for it to still mark a fix site
IDS_SCORE = 0.3
REISSUE_SCORE = 0.8  # a same-run re-issue this close returns the first card instead of creating a second

_CODE = re.compile(r"(?<![\w/.-])((?:[a-z_][a-z0-9_]*\.)*_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w-])")
_CARD_ID = re.compile(r"\bt_[0-9a-f]{8,16}\b")
_SCAN = 20000
_REVIEW = re.compile(r"\b(?:re-?review|review)\b(?!\s+(?:nits?|follow-?ups?|findings?|comments?)\b)", re.IGNORECASE)
_COMPONENT = re.compile(r"^\s*(?:\[([a-z0-9_-]+)\]|([a-z0-9_-]+):\s)", re.IGNORECASE)
_DIGIT_TOKEN = re.compile(r"[a-z0-9]*\d[a-z0-9]*")
_LETTER_MARK = re.compile(r"(?<![\w'\u2019])([A-Z])(?![\w'\u2019])")
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-/.'\u2019][A-Za-z0-9]+)*")


@lru_cache(maxsize=8192)
def code_ids(title: str, body: str = "") -> frozenset[str]:
    """snake_case code identifiers a card names (``_answer_interrupted_calls``, ``update_pause_cancel``), each
    part of a dotted name on its own, at least 8 characters; card ids are not identifiers."""
    text = _CARD_ID.sub(" ", f"{title or ''}\n{(body or '')[:_SCAN]}".lower())
    return frozenset(part for name in _CODE.findall(text) for part in name.split(".")
                     if "_" in part.strip("_") and len(part) >= 8)


def common_ids(names: Iterable[str], cards: Iterable[dict]) -> frozenset[str]:
    """Those of ``names`` (the new card's identifiers) that more than ``RARE_MAX`` of ``cards`` mention: template
    words, not a fix site. A substring test, so it counts high, never low (``kanban_complete`` in
    ``kanban_complete_task`` counts)."""
    names = set(names)
    if len(names) < MIN_IDS:
        return frozenset(names)
    seen = dict.fromkeys(names, 0)
    for card in cards:
        text = f"{card.get('title') or ''}\n{(card.get('body') or '')[:_SCAN]}".lower()
        for name in names:
            if name in text:
                seen[name] += 1
    return frozenset(name for name, n in seen.items() if n > RARE_MAX)


_REASON_PREFIX = "same creator; both name "


def is_reason(why: str) -> bool:
    return why.startswith(_REASON_PREFIX)


def markers(title: str) -> frozenset[str]:
    """Index markers in a title: digit-bearing tokens (``w1-01``, ``2``, a sha) and lone capitals (fixture
    ``A``), never a possessive's ``s``. Card ids are not markers."""
    plain = _CARD_ID.sub(" ", title or "")
    return frozenset(_DIGIT_TOKEN.findall(plain.lower())) | frozenset(
        f"letter:{m}" for m in _LETTER_MARK.findall(plain))


def _title_tokens(title: str) -> list[str]:
    plain = _CARD_ID.sub(" ", title or "")
    return [m.group(0) for m in _TOKEN.finditer(plain)]


# A counter after one of these words numbers a re-issue of the same work, not a leg (PR #2429 review).
# "pass"/"take"/"try" are left out: "Pass 1 of 3: parser" / "Pass 2 of 3: parser" are staged legs (PR #2481 review).
_REISSUE_WORD = re.compile(r"(?:attempts?|rounds?|retry|retries|v|rev|revision|iterations?)",
                           re.IGNORECASE)
_REISSUE_TOKEN = re.compile(r"(?:v|rev)\d+(?:\.\d+)*", re.IGNORECASE)  # "v2", "rev3"
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)", re.IGNORECASE)  # "2nd attempt"


def _reissue_counter(tokens: list[str], i1: int, i2: int) -> bool:
    """``tokens[i1:i2]`` holds a re-issue counter: a number right after a re-issue word (``attempt 2``,
    ``round 1``), ``v3``, or an ordinal before one (``2nd attempt``)."""
    for k in range(i1, i2):
        tok = tokens[k]
        if not _DIGIT_TOKEN.fullmatch(tok.lower()):
            continue
        if (_REISSUE_TOKEN.fullmatch(tok) or (k > 0 and _REISSUE_WORD.fullmatch(tokens[k - 1]))
                or (_ORDINAL.fullmatch(tok) and k + 1 < len(tokens) and _REISSUE_WORD.fullmatch(tokens[k + 1]))):
            return True
    return False


def _shape(marker: str) -> str:
    """``w1-01`` -> ``a9-9``: two markers of one shape are the same index field."""
    return re.sub(r"[a-z]+", "a", re.sub(r"\d+", "9", marker.lower()))


def enumerated(title: str, theirs: str) -> Optional[tuple[frozenset[str], frozenset[str]]]:
    """``(mine, theirs)`` index markers that differ in the same place of both titles, else None.

    Only a replaced span counts, with a marker on both sides (``w1-01``/``w1-02``, ``1/3``/``3/3``, fixture
    ``A``/``B``, head ``5d2e512cbb``/``9baf8d8abc``). A marker only one title has (an added ``round 2``, an
    appended sha, ``attempt 3``) is a re-issue of the same work, not a leg (PR #2329 review), and so is a bumped
    counter (``attempt 2``/``attempt 3``, ``round 1``/``round 2``, ``v2``/``v3``). A marker that moved (``w1-01
    sleep probe``/``sleep probe w1-02``) is a deleted and an inserted span: it counts when both carry a marker of
    the same shape (PR #2429 review)."""
    a, b = _title_tokens(title), _title_tokens(theirs)
    ops = SequenceMatcher(None, [t.lower() for t in a], [t.lower() for t in b], autojunk=False).get_opcodes()
    deleted: set[str] = set()
    inserted: set[str] = set()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" or _reissue_counter(a, i1, i2) or _reissue_counter(b, j1, j2):
            continue
        mine_m, their_m = markers(" ".join(a[i1:i2])), markers(" ".join(b[j1:j2]))
        if tag == "replace" and mine_m and their_m and mine_m != their_m:
            return mine_m - their_m, their_m - mine_m
        deleted |= mine_m if tag == "delete" else set()
        inserted |= their_m if tag == "insert" else set()
    moved = {_shape(m) for m in deleted} & {_shape(m) for m in inserted}
    mine_m = frozenset(m for m in deleted - inserted if _shape(m) in moved)
    their_m = frozenset(m for m in inserted - deleted if _shape(m) in moved)
    if mine_m and their_m:
        return mine_m, their_m
    return None


def _component(title: str) -> Optional[str]:
    m = _COMPONENT.match(title or "")
    return (m.group(1) or m.group(2)).lower() if m else None


def own_distinct(title: str, assignee: Optional[str], card: dict) -> Optional[str]:
    """Why a card from the same creator is a different job (module docstring), else None."""
    theirs = card.get("title") or ""
    if assignee and card.get("assignee") and assignee != card["assignee"]:
        return f"assignee {assignee} != {card['assignee']}"
    if bool(_REVIEW.search(title or "")) != bool(_REVIEW.search(theirs)):
        return "a review and a non-review card"
    mine_c, their_c = _component(title), _component(theirs)
    if mine_c and their_c and mine_c != their_c:
        return f"component {mine_c} != {their_c}"
    diff = enumerated(title, theirs)
    if diff:
        mine_m, their_m = diff
        return f"enumerated ({', '.join(sorted(mine_m)[:3]) or '-'} vs {', '.join(sorted(their_m)[:3]) or '-'})"
    return None


def title_score(new_words: set[str], card_words: set[str]) -> float:
    return len(new_words & card_words) / max(len(new_words), len(card_words), 1)


def shared_code_reason(title: str, body: str, card: dict, common: frozenset[str], score: float) -> Optional[str]:
    """``same creator; both name <ids>`` when the cards share a rare fix site (module docstring), else None."""
    if score < IDS_SCORE:
        return None
    shared = (code_ids(title or "", body or "")
              & code_ids(card.get("title") or "", card.get("body") or "")) - common
    if len(shared) < MIN_IDS:
        return None
    return f"{_REASON_PREFIX}{', '.join(sorted(shared)[:6])}"


def reissue(card: dict, self_tid: Optional[str], run_started: Optional[int], score: float) -> bool:
    """The creator made ``card`` earlier in this same run, with no idempotency key, and the new title is
    nearly the same: a re-issue (the 11-13 archive-and-recreate after a refused archive). The create returns
    ``card`` instead of a second owner."""
    started = _epoch(run_started)
    return bool(self_tid and card.get("creator_task_id") == self_tid and card.get("local")
                and not card.get("has_key") and started is not None
                and (_epoch(card.get("created_at")) or 0) >= started and score >= REISSUE_SCORE)


def _epoch(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):  # a legacy ISO timestamp: not comparable, so never a re-issue
        return None
