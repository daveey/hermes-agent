"""Which pull URLs in a card's publication comments arm the ``active_pr`` hold. [daveey]

Input is the card's own or unknown-provenance comments only: comments known to come from
another card's run or a chat are FYI and are separated out by
:func:`kanban_db_dispatch_pr_state._foreign_pr_comment_ids` before this runs.

Among those candidates, a pull that is another live card's ``completion_contract`` is not
this card's work, and neither is a pull that appears only in comments saying it does not
block this card. This card's own ``completion_contract`` always holds.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

# A comment whose purpose is to say the named pull does not block this card.
# "doesn't block the merge" is not that purpose; the object has to be the card.
_NONBLOCKING_RE = re.compile(
    r"\b(?:does(?:n['\u2019]t| not)|do(?:n['\u2019]t| not)|won['\u2019]t|will not)\s+block\s+"
    r"(?:you|this\s+(?:card|task)|the\s+card)\b",
    re.IGNORECASE,
)


# Renamed/transferred GitHub repositories: old ``owner/repo`` -> canonical ``owner/repo``.
# Cards keep naming a PR by the URL they were given while ``gh`` reports the canonical
# one; both name the same pull. Only exact, known repo identities are collapsed: matching
# never falls back to the bare pull number across repositories. Extend per host with
# ``kanban.github_repo_aliases`` in config.yaml.
BUILTIN_GITHUB_REPO_ALIASES = {"daveey/hermes": "softmax-universe/daveey-hermes"}
_REPO_RE = re.compile(r"[\w.-]+/[\w.-]+")
_PULL_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[^/\s]+/[^/\s]+)(?P<tail>/pull/\d+)")
_ALIAS_TTL_SECONDS = 60.0
_alias_cache: tuple[float, dict[str, str]] | None = None


def _configured_aliases() -> dict:
    """``kanban.github_repo_aliases`` from config.yaml; ``{}`` when unreadable."""
    from ruamel.yaml.error import YAMLError

    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except (ImportError, OSError, ValueError, TypeError, AttributeError, YAMLError):
        return {}  # config errors can carry secrets: log nothing, built-ins only
    kanban = cfg.get("kanban") if isinstance(cfg, dict) else None
    aliases = kanban.get("github_repo_aliases") if isinstance(kanban, dict) else None
    return aliases if isinstance(aliases, dict) else {}


def repo_aliases() -> dict[str, str]:
    """Lowercased alias -> canonical repo map (built-ins plus valid config entries)."""
    global _alias_cache
    import time

    now = time.monotonic()
    if _alias_cache is not None and now < _alias_cache[0]:
        return _alias_cache[1]
    merged = dict(BUILTIN_GITHUB_REPO_ALIASES)
    for old, new in _configured_aliases().items():
        if (isinstance(old, str) and isinstance(new, str)
                and _REPO_RE.fullmatch(old.strip()) and _REPO_RE.fullmatch(new.strip())):
            merged[old.strip().lower()] = new.strip().lower()
    aliases = {old: new for old, new in merged.items() if old != new}
    _alias_cache = (now + _ALIAS_TTL_SECONDS, aliases)
    return aliases


def canonical_repo(repo: str) -> str:
    """Canonical lowercased ``owner/repo`` for a possibly renamed repository."""
    repo = repo.lower()
    return repo_aliases().get(repo, repo)


def norm_pr(url: str) -> str:
    """The one comparison key for a pull URL: lowercased, no trailing slash, and a
    renamed repository's URL mapped to its canonical repository."""
    key = url.strip().rstrip("/").lower()
    match = _PULL_URL_RE.fullmatch(key)
    if match is None:
        return key
    return "https://github.com/" + canonical_repo(match["repo"]) + match["tail"]


def _says_pull_does_not_block(body: str) -> bool:
    from hermes_cli import kanban_db_dispatch as dispatch

    # A disclaimer in a mixed-URL comment may refer to a different pull from the
    # publication. Ambiguous prose must not release the duplicate-work guard.
    urls = dispatch._RESPAWN_GUARD_PR_URL_RE.findall(body)
    if len(urls) != 1 or re.search(r"\b(?:opened|created|published|pushed)\b", body, re.IGNORECASE):
        return False
    disclaimer = _NONBLOCKING_RE.search(body)
    if disclaimer is None:
        return False
    # Recognize a deliberately narrow informational statement: the URL is the
    # subject, directly or via "<URL> ... . It ...". Unrelated lint/CI subjects,
    # publication statements, extra clauses and ambiguous prose remain guarded.
    prefix = body[:disclaimer.start()].replace(urls[0], "PULL")
    return bool(
        re.fullmatch(r"\s*(?:See\s+)?PULL(?:\s+[^.!?\n]+\.\s+It)?\s+", prefix, re.IGNORECASE)
        and re.fullmatch(r"[.!]?\s*", body[disclaimer.end():])
    )


def _own_contract_key(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """This card's completion_contract when it is a URL, else None."""
    row = conn.execute("SELECT completion_contract FROM tasks WHERE id = ?", (task_id,)).fetchone()
    contract = row[0] if row is not None else None
    if not isinstance(contract, str) or not contract.lower().startswith("http"):
        return None
    return norm_pr(contract)


def _foreign_live_contract_keys(conn: sqlite3.Connection, task_id: str, keys: set[str]) -> set[str]:
    """Pull URLs that are another live card's completion_contract.

    Done and archived cards are not live: a finished contract is not an owner that
    excuses a duplicate-PR mention.
    """
    if not keys:
        return set()
    # Compare canonical keys in Python: a renamed repo's contract URL and the
    # canonical mention are the same pull, which SQL lower/rtrim cannot see.
    rows = conn.execute(
        "SELECT completion_contract FROM tasks "
        "WHERE id != ? AND status NOT IN ('done', 'archived') "
        "AND lower(completion_contract) LIKE 'http%'",
        (task_id,),
    ).fetchall()
    return {norm_pr(row[0]) for row in rows} & keys


def holding_urls(conn: sqlite3.Connection, task_id: str, mentions: list[tuple[str, str]]) -> set[str]:
    """Normalized pull URLs among candidate ``(url, comment body)`` mentions that hold."""
    by_url: dict[str, list[str]] = {}
    for url, body in mentions:
        by_url.setdefault(norm_pr(url), []).append(body)
    if not by_url:
        return set()
    own = _own_contract_key(conn, task_id)
    foreign = _foreign_live_contract_keys(conn, task_id, set(by_url) - {own})
    return {
        key for key, bodies in by_url.items()
        if key == own or (key not in foreign and not all(map(_says_pull_does_not_block, bodies)))
    }
