"""Refuse a second owner for work an open card already does (kanban_create). [daveey]

A chat counted owners by its OWN chat links, found none, and created "Stage B" on the default board while
Stage B was already running on board ``unconventional-collab`` from another chat. Before creating, this scans
open cards on every board of this gateway (live board DBs) and every other gateway (the hermes-hub replica,
``hub.db``), so the answer no longer depends on which chat asks or which board the card lands on.

An open card matches when:
- the new card names its board, is not being created on that board, and the titles share a word (for a
  plain-word slug, 2 words that are a quarter of the longer title: one shared word or a hostname with any of
  the hundreds of cards on ``bb`` or ``hermes`` is chance, not overlap), or
- both titles share at least half of the longer title's significant words (3 or more).

Naming a board means an explicit reference in the title or body: ``board <slug>``, ``--board <slug>``,
``<slug> board``, ``@task:<slug>@...`` / ``@task:<slug>/...`` or ``<gateway>/<slug>`` for a known gateway
(``hub/runners``). A plain-word slug (``hermes``, ``bb``, ``runners``) inside a URL, a filesystem path or a
slash list is NOT a reference: ``github.com/daveey/hermes``, ``~/code/hermes``, ``Slack/Discord/BB Inbox``.
A slug that is not one plain word (``unconventional-collab``) is specific enough that ``/<slug>`` anywhere
or the slug itself in the title still names it (``Metta-AI/unconventional-collab``). A bare plain word that
equals a board slug does not count.

Never matched: closed cards (a replica's stale legacy ``<board>/<id>`` row never outvotes its qualified
``<profile>/<board>/<id>`` twin), the creator's own card, the new card's parents, cards the same chat or the same
worker created (fan-out), the waiting card a capability-owner create quotes, and another capability-owner
card for that same waiting card, a card the new one parks/waits/depends on (:func:`waits_on`), and a card
that names a different target (:func:`distinct_target`: another repo, host or process, or an inverted check at
another revision; a newer sha or another ticket number alone is not a different target). Admitted look-alikes
are logged. Each card counts once per gateway. A missing or unreadable replica is skipped and logged, never a block.
``kanban.create_overlap_check: false`` turns the check off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Optional

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


def _reasons(card: dict, new_words: set[str], title: str, body: str, target_board: str,
             gateways: Iterable[str] = ()) -> tuple[list[str], float]:
    reasons, score = [], 0.0
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
        score = len(shared) / max(len(new_words), len(theirs))
        if len(shared) >= MIN_SHARED and score >= 0.5:
            reasons.append(f"titles share {len(shared)} words ({', '.join(sorted(shared))})")
    return reasons, score


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


def _identity(title: str, body: str, gateways: Iterable[str]) -> dict:
    repo, in_title = _repo_of(title, body)
    return {"title": title or "", **target_identity(title, gateways), "repo": repo, "repo_in_title": in_title}


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
    return None


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


def _local_cards(self_gw: Optional[str]) -> Iterable[dict]:
    from hermes_cli.kanban_session_activity import cached_ro
    marks = ",".join("?" * len(OPEN))
    for slug, path in _board_files():
        if not path.exists():
            continue
        try:
            with cached_ro(path) as c:
                rows = c.execute(
                    "SELECT t.id, t.title, t.body, t.status, t.session_id, (SELECT json_extract(e.payload, "
                    "'$.creator_task_id') FROM task_events e WHERE e.task_id = t.id AND e.kind = 'created' "
                    f"LIMIT 1), t.idempotency_key FROM tasks t WHERE t.status IN ({marks})", OPEN).fetchall()
                try:
                    links = c.execute("SELECT task_id, chat_session FROM task_chat_links WHERE released = 0"
                                      ).fetchall()
                except sqlite3.OperationalError:
                    links = []
        except sqlite3.Error as exc:
            _log.warning("kanban create overlap: board %s unreadable, skipped (%s)", slug, exc)
            continue
        chats: dict[str, set[str]] = {}
        for tid, sess in links:
            chats.setdefault(tid, set()).add(sess)
        for tid, title, body, status, sess, creator, key in rows:
            yield {"id": tid, "board": slug, "gateway": self_gw or "local", "status": status, "title": title,
                   "body": body or "",
                   "session_id": sess, "creator_task_id": creator, "local": True, "key_family": key_family(key),
                   "chats": sorted(chats.get(tid, set()) | ({sess} if sess else set()))}


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
               "local": False,
               "session_id": next((s.get("session") for s in sessions if s.get("rel") == "created"), None),
               "creator_task_id": None, "key_family": None,
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


def _sibling_owner(card_title: str, waiting_id: str) -> bool:
    found = _OWNER_TITLE_RE.match(card_title or "")
    return bool(found and found.group(1) == waiting_id)


def find(*, title: str, body: str, target_board: str, session_id: Optional[str], self_tid: Optional[str],
         parents: Iterable[str], family: Optional[str] = None) -> list[dict]:
    """Open cards elsewhere that already cover this work, best match first. Never raises. ``family`` (the new
    card's idempotency-key family) skips cards from the same keyed creator: its keys already dedupe them."""
    try:
        from hermes_cli import kanban_capability_registry as reg
        self_gw = reg.self_gateway()
        skip = set(parents) | ({self_tid} if self_tid else set())
        waiting = capability_owner_waiting_id(title, body)
        new_words = words(title)
        out, seen, mine = [], set(), None
        cards = [*_local_cards(self_gw), *_replica_cards(self_gw)]
        gateways = {g for g in (self_gw, *(c["gateway"] for c in cards)) if g}
        if gateways & {"hub", "hermes-hub"}:
            gateways |= {"hub", "hermes-hub"}
        for card in cards:
            if (card["id"] in skip or card["id"] == waiting
                    or (waiting and _sibling_owner(card.get("title") or "", waiting))
                    or (session_id and card["session_id"] == session_id)
                    or (self_tid and card["creator_task_id"] == self_tid)
                    or (family and card.get("key_family") == family)
                    or (card["gateway"], card["id"]) in seen):
                continue
            seen.add((card["gateway"], card["id"]))
            reasons, score = _reasons(card, new_words, title, body, target_board, gateways)
            if not reasons:
                continue
            if mine is None:
                mine = _identity(title, body, gateways)
            admit = (f"waits on {card['id']}" if waits_on(card["id"], title, body)
                     else _distinct(mine, _identity(card.get("title") or "", card.get("body") or "", gateways)))
            if admit:
                # Logged so an audit can tell an admitted look-alike from a missed one.
                _log.info("kanban create overlap: admitted %r despite %s/%s/%s (%s): %s", title, card["gateway"],
                          card["board"], card["id"], "; ".join(reasons), admit)
            else:
                out.append({**{k: card[k] for k in ("id", "board", "gateway", "status", "title", "chats")},
                            "why": "; ".join(reasons), "_score": score, "_local": card["local"]})
        out.sort(key=lambda m: (-m["_score"], m["status"] not in ("running", "ready", "scheduled"), m["id"]))
        return out
    except Exception:  # health: allow BLE001 -- a broken check must never block creating a card
        _log.warning("kanban create overlap check failed; creating without it", exc_info=True)
        return []


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


def _no_override(conn: sqlite3.Connection, task_id: str) -> None:
    return None


def check(conn: sqlite3.Connection, args: dict, *, courier: bool, title: str, session_id: Optional[str],
          self_tid: Optional[str], parents: list[str], entry: str = "tool",
          ) -> tuple[Optional[str], Callable[[sqlite3.Connection, str], None]]:
    """``(refusal_json, _)`` stops the create; otherwise ``(None, after_create)``, where ``after_create(conn,
    new_task_id)`` records an accepted ``distinct_from`` override on the new card (a no-op when nothing
    overlapped). A courier-applied card (``courier``) was already checked on the gateway that asked for it.
    ``entry`` (tool | cli | rest) only words the refusal's retry hint for the caller's entry path."""
    from hermes_cli import kanban_db as kb
    if courier or not enabled():
        return None, _no_override
    key = args.get("idempotency_key")
    if key and conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ? AND status != 'archived'",
                            (key,)).fetchone():
        return None, _no_override  # a retry returns the existing card; nothing new is created
    distinct = [str(x) for x in (args.get("distinct_from") or [])]
    reason = str(args.get("distinct_reason") or "")
    matches = find(title=title, body=str(args.get("body") or ""), target_board=args.get("board") or kb.DEFAULT_BOARD,
                   session_id=session_id, self_tid=self_tid, parents=parents, family=key_family(key))
    if not matches:
        return None, _no_override
    if covered(matches, distinct, reason):
        return None, lambda c, tid: record(c, tid, outcome="override", title=title, matches=matches,
                                           reason=reason, session_id=session_id)
    own = bool(self_tid) and not args.get("board")  # the creator's card is on the board this call opened
    record(conn if own else None, self_tid, outcome="refused", title=title, matches=matches,
           session_id=session_id)
    return json.dumps(refusal(matches, distinct, reason, entry)), _no_override
