"""Shared session-listing helpers for CLI and gateway slash surfaces."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from utils import is_truthy_value

_LIST_WORDS = {"list", "ls", "browse"}
_SEARCH_WORDS = {"search", "find"}
SUBAGENT_SOURCE = "subagent"


def show_subagent_sessions(hermes_home: str | Path) -> bool:
    """``sessions.show_subagents`` from *hermes_home*'s own config.yaml. One ``serve`` lists many
    profiles' stores, so the store's home decides, never the process ``HERMES_HOME``. False when
    the config cannot be read (the listing keeps its default shape)."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(hermes_home))
    try:
        sessions_cfg = load_config_readonly().get("sessions") or {}
    except Exception:
        return False
    finally:
        reset_hermes_home_override(token)
    return isinstance(sessions_cfg, dict) and is_truthy_value(sessions_cfg.get("show_subagents"))


def sidebar_excluded_sources(hermes_home: str | Path) -> list[str]:
    """``sessions.sidebar_exclude_sources`` from *hermes_home*'s own config.yaml: extra session
    sources this profile keeps out of the desktop sidebar (machine traffic such as an
    OpenAI-compatible ``api_server`` backend serving another app). Like ``show_subagents`` the
    store's home decides, so a machine profile hides its own rows wherever it is listed. ``[]``
    when unset or unreadable."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(hermes_home))
    try:
        sessions_cfg = load_config_readonly().get("sessions") or {}
    except Exception:
        return []
    finally:
        reset_hermes_home_override(token)
    raw = sessions_cfg.get("sidebar_exclude_sources") if isinstance(sessions_cfg, dict) else None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(s).strip() for s in raw if str(s).strip()]


AUTOMATED_ALL_SOURCES = "*"


def _sessions_source_list(hermes_home: str | Path, key: str) -> list[str]:
    """Lower-cased ``sessions.<key>`` source list from *hermes_home*'s own config.yaml [daveey];
    ``[]`` when unset or unreadable (a comma string is accepted too)."""
    from hermes_cli.config import load_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(hermes_home))
    try:
        sessions_cfg = load_config_readonly().get("sessions") or {}
    except Exception:
        return []
    finally:
        reset_hermes_home_override(token)
    raw = sessions_cfg.get(key) if isinstance(sessions_cfg, dict) else None
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(s).strip().lower() for s in raw if str(s).strip()]


class AutomatedSourcePolicy(list):
    """``sessions.automated_sources`` (the list itself, so existing list callers keep working) plus
    ``interactive`` = ``sessions.interactive_sources`` [daveey]. A non-empty ``interactive`` turns
    the classifier into an allowlist: any source outside it is automated, so a NEW bot / eval /
    scratch source writing into a human profile is hidden by default instead of leaking."""

    def __init__(self, automated=(), interactive=()):
        super().__init__(automated)
        self.interactive: list[str] = list(interactive)

    def __bool__(self) -> bool:  # an allowlist-only policy is still a policy
        return bool(len(self) or self.interactive)


def _profile_sets_source_policy(hermes_home: str | Path) -> bool:
    """Whether *hermes_home*'s own config.yaml spells out ``sessions.automated_sources`` or
    ``sessions.interactive_sources`` (even as ``[]``), as written, not merged with defaults."""
    from hermes_cli.config import read_raw_config_readonly
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(hermes_home))
    try:
        sessions_cfg = read_raw_config_readonly().get("sessions") or {}
    except Exception:
        return False
    finally:
        reset_hermes_home_override(token)
    return isinstance(sessions_cfg, dict) and (
        "automated_sources" in sessions_cfg or "interactive_sources" in sessions_cfg)


def _store_profile_and_root(hermes_home: str | Path) -> tuple[str, Path]:
    """``(profile name, root home)`` for a store's home: ``<root>/profiles/<name>`` is named
    profile ``<name>``, anything else is the ``default`` profile living in the root itself."""
    from hermes_constants import named_profile_home

    named = named_profile_home(hermes_home)
    if named is not None:
        return named.name, named.parent.parent
    return "default", Path(hermes_home)


_SIDEBAR_POLICY_KEYS = ("automated_sources", "interactive_sources", "sidebar_profiles")


def strip_inherited_sidebar_policy(config_path: Path, profile_name: str, root_home: Path) -> list[str]:
    """Drop the sidebar source lists a ``profile create --clone`` copied from its source [daveey].

    Only when *root_home*'s ``sessions.sidebar_profiles`` is non-empty and *profile_name* is not on
    it: otherwise the copied ``interactive_sources`` / ``automated_sources`` would count as the new
    profile's own explicit choice and override the "unlisted profiles start automated" default.
    Returns the dotted keys removed (``[]`` when nothing applies)."""
    from hermes_cli.config_backend import ConfigBackendUnavailable, config_exists
    try:
        sidebar_profiles = _sessions_source_list(root_home, "sidebar_profiles")
    except ConfigBackendUnavailable:
        # This clone cleanup is best-effort; keep the copied policy rather than
        # abort profile creation when the root's remote config cannot be read.
        return []
    if not sidebar_profiles or profile_name.lower() in sidebar_profiles or not config_exists(config_path):
        return []
    from hermes_cli.config import atomic_config_replace, read_user_config_raw
    raw = read_user_config_raw(config_path)
    sessions_cfg = raw.get("sessions")
    if not isinstance(sessions_cfg, dict):
        return []
    removed = [k for k in _SIDEBAR_POLICY_KEYS if k in sessions_cfg]
    if not removed:
        return []
    for key in removed:
        sessions_cfg.pop(key)
    if not sessions_cfg:
        raw.pop("sessions")
    atomic_config_replace(config_path, raw)
    return [f"sessions.{k}" for k in removed]


def automated_session_sources(hermes_home: str | Path) -> AutomatedSourcePolicy:
    """``sessions.automated_sources`` + ``sessions.interactive_sources`` from *hermes_home*'s own
    config.yaml [daveey]: which session sources this profile marks ``automated`` (bot / smoke-test
    traffic the desktop sidebar hides unless "Show automated & test sessions" is on).
    ``automated_sources``: exact sources, ``"*"`` marks every session in the store.
    ``interactive_sources``: when non-empty, every source NOT listed is automated too.
    Like ``sidebar_exclude_sources`` the store's home decides, never the process ``HERMES_HOME``.
    ``sessions.sidebar_profiles`` in the ROOT home's config (never a profile's): when non-empty, a
    profile NOT listed behaves as ``automated_sources: ["*"]`` unless its own config.yaml sets
    ``automated_sources`` or ``interactive_sources`` explicitly, so a new bot profile starts hidden.
    Empty policy when unset or unreadable."""
    policy = AutomatedSourcePolicy(
        _sessions_source_list(hermes_home, "automated_sources"),
        _sessions_source_list(hermes_home, "interactive_sources"))
    try:
        profile, root = _store_profile_and_root(hermes_home)
    except Exception:
        return policy
    sidebar_profiles = _sessions_source_list(root, "sidebar_profiles")
    if (sidebar_profiles and profile.lower() not in sidebar_profiles
            and not _profile_sets_source_policy(hermes_home)):
        return AutomatedSourcePolicy([AUTOMATED_ALL_SOURCES])
    return policy


def is_automated_source(source: Any, automated_sources: list[str] | None, *, created_source: Any = None) -> bool:
    """Whether a row with *source* is automated under *automated_sources* (``"*"`` = all). When it
    is an :class:`AutomatedSourcePolicy` with ``interactive`` sources, a non-empty source outside
    that allowlist is automated too. A row with no source at all (legacy) is never flagged by the
    allowlist alone: hiding unknown human chats is worse than showing one stray row."""
    if not automated_sources:
        return False
    src = str(source or "").strip().lower()
    if AUTOMATED_ALL_SOURCES in automated_sources or (src and src in automated_sources):
        return True
    # Routing source is not creation provenance. Only the canonical local human surfaces
    # may stand in for local; never let a bot/eval label borrow an unrelated creator.
    if src == "gui":
        src = "desktop"
    elif src == "local" and created_source in {"desktop", "cli", "tui", "acp"}:
        src = created_source
    automated = {"desktop" if value == "gui" else value for value in automated_sources}
    if src in automated:
        return True
    interactive = getattr(automated_sources, "interactive", None)
    allowed = {"desktop" if value == "gui" else value for value in (interactive or ())}
    return bool(allowed) and bool(src) and src not in allowed


def stamp_automated(
    rows: list[dict[str, Any]], hermes_home: str | Path | None = None, *,
    automated_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Set ``automated: bool`` on every row from its store's ``sessions.automated_sources`` /
    ``sessions.interactive_sources`` (read from *hermes_home* unless *automated_sources* is
    given). Returns *rows*."""
    if automated_sources is None:
        automated_sources = automated_session_sources(hermes_home) if hermes_home is not None else []
    for row in rows:
        row["automated"] = is_automated_source(row.get("source"), automated_sources, created_source=row.get("created_source"))
    return rows


def automated_policy_key(policy: AutomatedSourcePolicy) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """*policy* as a hashable cache-key part: a cached list made with ``automated=exclude`` is only
    valid for the policy it was made under, and config.yaml changes without touching state.db."""
    return tuple(policy), tuple(policy.interactive)


def automated_listing_exclusion(
    hermes_home: str | Path, db: Any, policy: AutomatedSourcePolicy | None = None,
) -> tuple[bool, list[str], list[tuple[str, str]]]:
    """``(skip_store, exclude_sources, exclude_source_pairs)`` for SQL list/count filtering.
    Whole-source exclusions handle ordinary producer labels; mixed local routing rows use
    exact (source, created_source) pairs so human surfaces do not admit bot/eval traffic.
    Both filters apply before LIMIT/OFFSET and to pinned backfill. The classifier keeps
    legacy rows with no source, and explicit ``*`` excludes the entire store.
    """
    if policy is None:
        policy = automated_session_sources(hermes_home)
    if not policy:
        return False, [], []
    if AUTOMATED_ALL_SOURCES in policy:
        return True, [], []
    # A routing class can hold both human and machine sessions. Exclude its individual
    # provenance classes in SQL, before paging/counting; never exclude all local rows.
    with db._read_ctx() as conn:
        held = conn.execute("SELECT DISTINCT source, COALESCE(created_source, '') FROM sessions").fetchall()
    sources, pairs = set(), []
    for source, created in held:
        if is_automated_source(source, policy, created_source=created):
            if str(source or "").strip().lower() == "local":
                pairs.append((source, created))
            else:
                sources.add(source)
    return False, sorted(sources), pairs


def subagent_listing_scope(
    hermes_home: str | Path, *, source: str | None = None, sources: list[str] | None = None,
    exclude_sources: list[str] | None = None,
) -> tuple[bool, list[str] | None]:
    """``(include_subagents, exclude_sources)`` for one human-facing session list (#97202).

    With ``sessions.show_subagents`` on, delegate runs join a list that is not scoped to named
    sources and either has no exclusions or excludes the ``subagent`` source itself (the desktop
    recents shape); that exclusion is dropped so the runs actually land. A slice that excludes
    other sources without ``subagent`` (the per-platform messaging lists) keeps its shape.
    """
    excluded = list(exclude_sources or [])
    if source or sources or (excluded and SUBAGENT_SOURCE not in excluded):
        return False, exclude_sources
    if not show_subagent_sessions(hermes_home):
        return False, exclude_sources
    return True, [s for s in excluded if s != SUBAGENT_SOURCE] or None


def parse_session_listing_args(raw_args: str) -> tuple[bool, bool, str, str | None]:
    """Parse `/sessions`-style args into ``(include_all_sources, include_unnamed, target, search_query)``.

    ``all`` widens source scope, ``full`` keeps unnamed sessions, ``search``/``find`` makes the rest
    a query (``None`` = not requested, ``""`` = requested with no terms). Flags are honored only
    before the first positional word so titles containing "all" aren't misparsed; anything else is
    a target so `/sessions <id-or-title>` can delegate to `/resume`.
    """
    parts = shlex.split(raw_args or "")
    flags = {"all": False, "full": False}
    target_parts: list[str] = []
    for i, part in enumerate(parts):
        lower = part.strip().lower()
        if not target_parts:
            if lower in _LIST_WORDS:
                continue
            if lower in {"all", "--all", "full", "--full"}:
                flags[lower.lstrip("-")] = True
                continue
            if lower in _SEARCH_WORDS:
                return flags["all"], flags["full"], "", " ".join(parts[i + 1:]).strip()
        target_parts.append(part)
    return flags["all"], flags["full"], " ".join(target_parts).strip(), None


def query_session_listing(
    session_db: Any,
    *,
    source: str | None,
    session_key: str | None = None,
    current_session_id: str | None = None,
    include_current_session: bool = False,
    include_all_sources: bool = False,
    include_unnamed: bool = False,
    search_query: str | None = None,
    limit: int = 10,
    exclude_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return session rows for interactive listing surfaces (shared CLI/gateway policy).

    Source-scoped unless global is requested; unnamed hidden unless a full listing is asked for;
    current session hidden unless requested (then marked ``is_current_session``); ``session_key``
    restricts gateway callers to one lane before the DB limit applies. With ``search_query`` rows
    are filtered by title/id in SQL, ordered by recent activity, and unnamed sessions stay visible
    since an id match may be the only handle.
    """
    search = (search_query or "").strip()
    rows = session_db.list_sessions_rich(
        source=None if include_all_sources else source,
        session_key=session_key,
        exclude_sources=exclude_sources,
        limit=max(limit * 4, limit),
        search_query=search or None,
        order_by_last_active=bool(search),
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        is_current = bool(current_session_id and row.get("id") == current_session_id)
        if (is_current and not include_current_session) or (
            not include_unnamed and not row.get("title") and not search and not is_current
        ):
            continue
        result.append({**row, "is_current_session": True} if is_current else row)
        if len(result) >= limit:
            break
    return result


def format_gateway_session_listing(
    rows: list[dict[str, Any]],
    *,
    include_source: bool = False,
    title: str = "Sessions",
    notice: str | None = None,
) -> str:
    """Render a compact Markdown-ish session list for gateway messengers.

    ``notice`` adds an explanatory line above the footer — e.g. when a requested scope widening
    (``all``) was declined, so the caller isn't left guessing why sessions are missing.
    """
    if not rows:
        return "\n".join([
            "No sessions found.\n"
            "Use `/title My Session` to name this chat, or `/sessions full` "
            "to include unnamed sessions.",
            *([notice] if notice else []),
        ])
    lines = [f"📋 **{title}**", ""]
    for idx, row in enumerate(rows, start=1):
        current_part = " (current)" if row.get("is_current_session") else ""
        preview = str(row.get("preview") or "")[:40]
        source = str(row.get("source") or "")
        source_part = f" `{source}`" if include_source and source else ""
        preview_part = f" — _{preview}_" if preview else ""
        lines.append(
            f"{idx}. **{row.get('title') or '—'}**{current_part}{source_part}"
            f" — `{row.get('id') or ''}`{preview_part}"
        )
    return "\n".join([
        *lines, "", *([notice] if notice else []),
        "Resume: `/resume <session id>` or `/resume <number>` from `/resume`.",
        "More: `/sessions all`, `/sessions full`, `/sessions search <query>`.",
    ])
