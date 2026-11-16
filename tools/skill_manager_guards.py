"""Write/delete guards for ``skill_manage``. Every guard returns ``None`` when the
operation may proceed, else a refusal (error dict or message). Origin-owned state
(``_find_skill``, ``_skills_dir``) is reached lazily via ``tools.skill_manager_tool``
so test patches keep working."""

import contextvars as _ctxvars
import logging
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.skill_manager_tool")


def _refusal(message: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _is_background_review() -> bool:
    """True inside the autonomous curator review fork, and on any lookup failure: every guard keyed
    on this is stricter for the fork, so an unknown origin gets the fork's guards."""
    try:
        from tools.skill_provenance import is_background_review
        return bool(is_background_review())
    except Exception:
        logger.warning("write-origin lookup failed; applying background-review guards", exc_info=True)
        return True


def _resolved_str(path: Path) -> str:
    with suppress(Exception):
        return str(path.resolve())
    return str(path)


class _BackgroundReviewReadMarks:
    """Read marks shared by copied tool contexts within one review run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)

    def contains(self, path: str) -> bool:
        with self._lock:
            return path in self._paths


_background_review_read_paths: _ctxvars.ContextVar[Optional[_BackgroundReviewReadMarks]] = (
    _ctxvars.ContextVar("background_review_read_paths", default=None))


# Fork: upstream eedf3944e9e dropped the internal ``_source_path`` from skill_view results, so
# _current_skill_view learns which file skill_view served from the read mark it records instead.
_served_paths_capture: _ctxvars.ContextVar[Optional[list]] = _ctxvars.ContextVar(
    "skill_view_served_paths_capture", default=None)


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file. The fork must not
    patch content it only inferred from the transcript: skill_view/read_file call this, and
    the write guards require the mark."""
    if (capture := _served_paths_capture.get()) is not None:
        capture.append(Path(path))
    if not _is_background_review():
        return
    if (marks := _background_review_read_paths.get()) is None:
        _background_review_read_paths.set(marks := _BackgroundReviewReadMarks())
    marks.add(_resolved_str(path))


def _background_review_has_read(path: Path) -> bool:
    marks = _background_review_read_paths.get()
    return marks is not None and marks.contains(_resolved_str(path))


def _reset_background_review_read_marks() -> None:
    """Start a fresh, isolated read set for the current review context."""
    _background_review_read_paths.set(_BackgroundReviewReadMarks())


def _transcript_skill_views(messages: list) -> list[tuple[str, Optional[str], str, Optional[str]]]:
    """``(name, file_path, content, source_path)`` for every full skill_view result in ``messages``.

    Dedup stubs (``content_returned: False``), failures and non-JSON (pruned/summarised) results
    are skipped; the tool name comes from the assistant tool_call the result answers."""
    import json

    call_names: dict[str, str] = {}
    views: list[tuple[str, Optional[str], str, Optional[str]]] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict) and tc.get("id"):
                call_names[tc["id"]] = str((tc.get("function") or {}).get("name") or "")
        if msg.get("role") != "tool" or not isinstance(content := msg.get("content"), str):
            continue
        tool = msg.get("tool_name") or msg.get("name") or call_names.get(msg.get("tool_call_id") or "")
        if tool != "skill_view":
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if (isinstance(payload, dict) and payload.get("success") is True and payload.get("name")
                and isinstance(payload.get("content"), str) and not payload.get("is_binary")):
            views.append((str(payload["name"]), payload.get("file"), payload["content"],
                          payload.get("_source_path")))
    return views


def seed_background_review_reads_from_history(messages: list, session_id: Optional[str] = None,
                                              stale: Optional[list] = None) -> int:
    """Treat skill_view results the review fork replays verbatim as reads of the current file.

    The fork resumes the parent's transcript, so a skill the parent loaded is already in the fork's
    context. When that copy is byte-identical to what skill_view serves NOW, the fork holds the exact
    current content, which is what the read-before-write guard demands; refusing its first patch only
    forced a redundant re-view (a 90 KB SKILL.md reloaded per review). A stale, pruned or stubbed copy
    does not match and stays unmarked, so the guard still fires for it. Call after
    ``_reset_background_review_read_marks()`` in the context the fork's tool workers copy. Returns the
    number of files marked. When ``stale`` is a list, ``(name, file_path)`` is appended for every
    replayed copy that no longer matches the file (edited by another session since it was loaded), so
    the fork can be told to re-view those before writing instead of being refused first."""
    if (marks := _background_review_read_paths.get()) is None:
        _background_review_read_paths.set(marks := _BackgroundReviewReadMarks())
    seen: set[tuple[str, Optional[str]]] = set()
    marked = 0
    for name, file_path, content, source in reversed(_transcript_skill_views(messages)):
        if (name, file_path) in seen:  # newest copy wins: an older one may predate a patch
            continue
        seen.add((name, file_path))
        try:
            current = _current_skill_view(name, file_path, session_id)
        except Exception:
            logger.debug("review read seeding failed for %s/%s", name, file_path, exc_info=True)
            continue
        if current is None:  # deleted/renamed since: nothing to re-view
            continue
        resolved = _resolved_str(current[0])
        # Same name but a different file (a shadowing copy appeared since): not a read of the target.
        if current[1] != content or (source and _resolved_str(Path(source)) != resolved):
            if stale is not None:
                stale.append((name, file_path))
            continue
        marks.add(resolved)
        marked += 1
    return marked


def stale_skill_views_note(stale: list) -> str:
    """Review-prompt line naming replayed skill copies that are out of date, or ''.

    Without it the fork patches from the parent's old copy, read-before-write refuses (correctly),
    and every such review burns a failed call before the re-view (coach: kanban-orchestrator)."""
    if not stale:
        return ""
    labels = ", ".join(sorted({f"{n} ({fp or 'SKILL.md'})" for n, fp in stale}))
    return (f"\n\nThese skills changed on disk after this conversation loaded them, so the copies above "
            f"are stale: {labels}. Before any skill_manage write to them, call skill_view (with "
            f"file_path for a supporting file) and base the edit on the content it returns.")


def _current_skill_view(name: str, file_path: Optional[str], session_id: Optional[str]):
    """``(source path, content)`` skill_view would serve now, or None. Runs in a throwaway context
    so skill_view's own read-mark side effect cannot leak into the fork's marks."""
    import contextvars
    import json

    served: list = []

    def _view():
        _background_review_read_paths.set(_BackgroundReviewReadMarks())
        _served_paths_capture.set(served)
        from tools.skills_tool import skill_view
        return skill_view(name, file_path=file_path, task_id=session_id)

    payload = json.loads(contextvars.copy_context().run(_view))
    source = payload.get("_source_path") if isinstance(payload, dict) else None
    source = Path(source) if source else (served[-1] if served else None)
    if not (isinstance(payload, dict) and payload.get("success") is True and source is not None
            and isinstance(payload.get("content"), str)):
        return None
    return source, payload["content"]


def _resolved_roots(skill_path: Path):
    """``(resolved skill_path, [(root, resolved_root), ...])`` over every resolvable skills root."""
    from agent.skill_utils import get_all_skills_dirs
    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path
    roots = []
    for root in get_all_skills_dirs():
        with suppress(OSError):
            roots.append((root, root.resolve()))
    return resolved, roots


def _containing_skills_root(skill_path: Path) -> Path:
    """Skills root (local or external_dirs) containing ``skill_path``; local dir if none match."""
    from tools import skill_manager_tool as _smt
    resolved, roots = _resolved_roots(skill_path)
    return next((root for root, r in roots if resolved.is_relative_to(r)), _smt._skills_dir())


def _is_path_redirect(path: Path) -> bool:
    """Symlink or (Windows 3.12+) junction — either lets a poisoned tree redirect rmtree outside."""
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before rmtree: even a poisoned tree must never delete (1) a path outside
    every known skills root, (2) a skills root itself, (3) a symlink/junction (rmtree follows it).

    ``_find_skill`` already restricts ``skill_dir`` to a real ``SKILL.md`` parent discovered by walking the
    skills roots, so the agent cannot inject an arbitrary path the way Kilo Code's HTTP endpoint could
    (their issue 11227: a built-in-skill sentinel resolved to the server cwd and a recursive delete wiped
    the user's entire working directory). This is the matching defense-in-depth for our agent-facing
    ``skill_manage`` delete path: even if discovery or a poisoned tree hands us a bad directory, never
    recursively delete See #11227.
    """
    if _is_path_redirect(skill_dir):
        return (f"Refusing to delete '{skill_dir}': the skill directory is a "
                f"symlink/junction. Remove the link target manually if intended.")
    try:
        skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."
    resolved, roots = _resolved_roots(skill_dir)
    for _root, root in roots:
        if resolved == root:
            return (f"Refusing to delete '{skill_dir}': resolves to the skills root "
                    f"itself, which would remove every installed skill.")
        if resolved.is_relative_to(root):
            return None
    return f"Refusing to delete '{skill_dir}': path does not resolve inside any known skills root."


def _is_pinned(name: str, what: str) -> Optional[bool]:
    """skill_usage pinned flag; None (logged at debug) when the record is unreadable."""
    try:
        from tools import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        logger.debug("%s lookup failed for %s", what, name, exc_info=True)
        return None


def _pinned_guard(name: str) -> Optional[str]:
    """Refusal message if *name* is pinned or essential, else None. Pin only guards DELETION;
    patches/edits stay allowed. ESSENTIAL_SKILLS are permanently pinned (the system prompt
    references them). Best-effort: an unreadable sidecar lets the delete through."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        if name in ESSENTIAL_SKILLS:
            return (
                f"Skill '{name}' is essential to Hermes (the agent's own "
                f"operating manual referenced by the system prompt) and "
                f"cannot be deleted. Patches and edits are still allowed.")
    except Exception:
        logger.debug("essential-guard lookup failed for %s", name, exc_info=True)
    if _is_pinned(name, "pinned-guard"):
        return (
            f"Skill '{name}' is pinned and cannot be deleted by skill_manage. Ask the user to "
            f"run `hermes curator unpin {name}` if they want to delete it. Patches and edits "
            f"are allowed on pinned skills; only deletion is blocked.")
    return None


def _background_review_delete_guard(name: str, skill_dir: Path) -> Optional[dict[str, Any]]:
    """Refuse autonomous deletes of anything but curator-owned sediment. Content writes are not
    gated by ownership: the review fork exists to improve every skill it learns from, and every
    write is ledgered and reversible, while an archive removes a skill the user relies on."""
    if not _is_background_review():
        return None
    refuse = "Refusing background curator delete for"
    if _is_pinned(name, "pinned skill guard"):
        return _refusal(
            f"{refuse} pinned skill '{name}': pinned skills "
            f"are off-limits to autonomous maintenance. Ask the user to run `hermes curator "
            f"unpin {name}` if they want it changed.")
    try:
        from agent.skill_utils import is_external_skill_path
        if is_external_skill_path(skill_dir):
            return _refusal(
                f"{refuse} skill '{name}': the skill lives in skills.external_dirs, which are "
                f"externally owned and read-only to autonomous curation.")
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)
    try:
        from tools import skill_usage
        for predicate, label in (
            (skill_usage.is_protected_builtin, "protected built-in"),
            (skill_usage.is_hub_installed, "hub-installed"),
            (skill_usage.is_bundled, "bundled")):
            if predicate(name):
                return _refusal(f"{refuse} {label} skill '{name}'.")
        # Not curator-managed (no `created_by: "agent"`) => user-owned, never archived
        # autonomously. A MISSING record and `created_by: null` must resolve identically:
        # keying on presence let the guard's own bookkeeping flip the verdict (#67140).
        usage_rec = skill_usage.load_usage().get(name)
        if not skill_usage._is_curator_managed_record(usage_rec):
            _detail = (f"created_by={usage_rec.get('created_by')!r}" if isinstance(usage_rec, dict)
                       else "no usage record")
            return _refusal(
                f"{refuse} skill '{name}': the skill is not "
                f"curator-managed ({_detail}). Only curator-managed skills may be archived "
                f"autonomously. Run `hermes curator adopt {name}` to opt it in.")
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return _refusal(
            f"{refuse} skill '{name}': agent ownership could not "
            f"be verified because the provenance record is unavailable or unreadable.")
    return None


# Inline the refused file's current text up to this size: past it the refusal falls back to
# "call skill_view" (SKILL.md content is capped well below this, so in practice always inline).
_INLINE_REFUSAL_MAX_CHARS = 120_000


def _background_review_read_before_write_guard(
    name: str, target: Path, action: str, file_label: str) -> Optional[dict[str, Any]]:
    """Require review forks to load the exact target before mutating it.

    The refusal itself serves the current file and counts as the read, so the fork's next call
    can be the corrected write. Refuse -> skill_view -> retry was a three-step recovery and review
    forks kept stopping after the re-view, so the learning was never saved (coach:
    babybear-devices-home, seen only through a search_files snippet, patched, refused, re-viewed,
    turn ended with nothing written). The write is still refused: the fork must rebuild the edit
    against the full current text, not resend the blind one."""
    if not _is_background_review() or _background_review_has_read(target):
        return None
    current: Optional[str] = None
    with suppress(Exception):
        text = target.read_text(encoding="utf-8-sig")
        if len(text) <= _INLINE_REFUSAL_MAX_CHARS:
            current = text
    if current is not None:
        mark_background_review_skill_read(target)
        return _refusal(
            f"Refusing background curator {action} for skill '{name}': the current {file_label} "
            f"content had not been loaded in this review turn, so nothing was written. Its current "
            f"text is in current_content and now counts as loaded. Do not stop here: rebuild the "
            f"edit from current_content (anchor old_string on it, skip guidance it already has, "
            f"keep newer content) and send the corrected skill_manage call now. In a batch, resend "
            f"every operation; the earlier ones were rolled back.",
            _read_before_write_required=True, current_file=file_label, current_content=current)
    return _refusal(
        f"Refusing background curator {action} for skill '{name}': the current {file_label} "
        f"content has not been loaded in this review turn. Call skill_view(name) for SKILL.md, or "
        f"skill_view(name, file_path=...) for a supporting file, then retry the write using the "
        f"content just returned.",
        _read_before_write_required=True)


def _locked_guard(name: str, action: str) -> Optional[dict[str, Any]]:
    """Refuse every mutating ``skill_manage`` action on a skill the user locked (``hermes curator
    lock`` / the Desktop lock switch), whoever asks: foreground turns, the background review and the
    curator pass alike. ``create`` is exempt (the collision check already refuses a name that resolves
    anywhere). An unreadable sidecar fails CLOSED for the autonomous passes (silently relaxing a
    lock is the failure this exists to prevent) and open for normal turns, so one corrupt file does
    not freeze every skill edit. Keyed by directory name, so the bare, ``cat/name`` and
    frontmatter-name forms all resolve to the same record."""
    if action == "create" or not name:
        return None
    try:
        from tools import skill_manager_tool as _smt
        from tools import skill_usage
        candidates = {name, Path(name).name}
        if existing := _smt._find_skill(name):
            candidates.add(existing["path"].name)
        locked = next((c for c in candidates if skill_usage.is_locked(c)), None)
    except Exception:
        logger.warning("locked skill guard lookup failed for %s", name, exc_info=True)
        if not _is_background_review():
            return None  # a corrupt sidecar must not freeze every skill edit in normal turns
        return _refusal(
            f"Refusing {action} for skill '{name}': its lock state could not be verified because the "
            f"skills/.usage.json sidecar is unreadable. Leave the skill alone and tell the user; they "
            f"can repair the sidecar or run `hermes curator unlock {name}`.", _locked=True)
    if locked:
        from tools.skill_lock import skill_refusal
        return _refusal(f"Refusing {action}: {skill_refusal(locked)}", _locked=True)
    return None

def _background_review_preflight(action: str, name: str) -> Optional[dict[str, Any]]:
    """Guards that run on the bare action/name before any handler, for the single-op AND the
    batch path: the content lock (every mutating action) and the ownership delete guard."""
    if (locked := _locked_guard(name, action)) is not None:
        return locked
    if action != "delete":
        return None
    from tools import skill_manager_tool as _smt
    existing = _smt._find_skill(name)
    return _background_review_delete_guard(name, existing["path"]) if existing else None


def _curator_consolidation_delete_guard(
    name: str, absorbed_into: Optional[str]) -> Optional[dict[str, Any]]:
    """Fail closed on unverified deletes during the curator consolidation pass. The fork's only
    legitimate delete is a consolidation declared via ``absorbed_into=<umbrella>`` (existence
    validated in ``_delete_skill``); the deterministic inactivity prune never calls skill_manage,
    so a bare delete here can only be the LLM pass pruning without evidence.

    A delete with no forwarding target — ``absorbed_into`` omitted (``None``) or empty (``""``) — is the
    fail-open behavior reported in #29912: the consolidation pass archived whole clusters of active skills
    with zero verified consolidations (``consolidated_this_run == 0``), leaving active automations pointing
    at names that no longer resolve. Refuse it; keep the skill active.
    """
    if not _is_background_review() or (isinstance(absorbed_into, str) and absorbed_into.strip()):
        return None
    return _refusal(
        f"Refusing background curator delete of skill '{name}': the consolidation pass may only "
        f"archive a skill it has absorbed into an umbrella. Pass absorbed_into=<umbrella> (the "
        f"umbrella must already exist) to record a verified consolidation. Pruning a skill with no "
        f"forwarding target is not permitted here — the deterministic inactivity prune handles "
        f"staleness archival separately. Keeping '{name}' active.",
        _fail_closed=True)
