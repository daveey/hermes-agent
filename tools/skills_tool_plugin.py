"""Plugin-provided skill serving for ``skill_view`` (``plugin:skill`` names) plus the JSON /
file-serving helpers shared with the local-skill path. Helpers tests patch on the origin module
(``_is_skill_disabled``, ``_parse_frontmatter``, ``skill_matches_platform``) resolve lazily."""

import json
import os
import logging
import re
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List

from tools.skills_tool_setup import SkillReadinessStatus

logger = logging.getLogger("tools.skills_tool")

MAX_NAME_LENGTH = 64  # Anthropic-recommended progressive-disclosure limits
MAX_DESCRIPTION_LENGTH = 1024
_INJECTION_PATTERNS: list = [  # shared by local-skill and plugin-skill paths
    "ignore previous instructions", "ignore all previous", "you are now",
    "disregard your", "forget your instructions", "new instructions:",
    "system prompt:", "<system>", "]]>"]
_SUPPORT_DIRS = ("references", "templates", "assets", "scripts")
_SKILL_FILE_EXTS = {".md", ".py", ".yaml", ".yml", ".json", ".tex", ".sh"}


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fail(error: str, **extra) -> str:
    return _json({"success": False, "error": error, **extra})


def _read_skill_text(path: Path) -> str:
    """utf-8-sig + errors="replace": user-authored SKILL.md may carry a Notepad BOM or stray
    bytes; pinning UTF-8 keeps skill_view deterministic across host locales."""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _truncate_description(description: str) -> str:
    if len(description) <= MAX_DESCRIPTION_LENGTH:
        return description
    return description[: MAX_DESCRIPTION_LENGTH - 3] + "..."


_FRONTMATTER_CACHE: dict = {}
_FRONTMATTER_CACHE_MAX = 4096


def _safe_frontmatter(path: Path | None = None, *, content: str | None = None) -> dict[str, Any]:
    """Frontmatter of *path* (or of *content*), ``{}`` on any read/parse failure.
    Parses via ``tools.skills_tool._parse_frontmatter``; replacing that callable invalidates cached entries.

    Path parses are memoized per process, keyed on the file's identity and stat
    (path, dev, ino, size, mtime_ns, ctime_ns) plus the parser in use, so a catalog scan that
    resolves many skills parses each unchanged SKILL.md once; any edit, replace or deletion
    changes the key (or the stat fails) and re-parses. Callers get a private deep copy."""
    import copy
    from tools import skills_tool as _st
    parser = _st._parse_frontmatter

    def _ident(p) -> tuple:
        s = Path(p).stat()
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

    key = before = text = None
    persisted_home = None
    if content is None and path is not None:
        with suppress(Exception):
            before = _ident(path)
            hit = _FRONTMATTER_CACHE.get((str(path), *before))
            if hit is not None and hit[0] is parser:  # entry holds the parser, so no id() reuse
                return copy.deepcopy(hit[1])
        # [daveey] Cross-process tier (tools.skill_frontmatter_index): same identity rule, parses
        # made by an earlier process with the same parser code; the stock parser only.
        with suppress(Exception):
            from tools import skill_frontmatter_index as _index
            if before is not None and _index.default_parser_in_use(parser):
                from hermes_constants import get_hermes_home
                persisted_home = get_hermes_home()
                stored = _index.lookup(persisted_home, str(path), before)
                if stored is not None:
                    if len(_FRONTMATTER_CACHE) >= _FRONTMATTER_CACHE_MAX:
                        _FRONTMATTER_CACHE.clear()
                    _FRONTMATTER_CACHE[(str(path), *before)] = (parser, copy.deepcopy(stored))
                    return stored
        with suppress(Exception):
            # Bind the bytes parsed to the identity they are cached under: fstat the very
            # descriptor that is read, so a symlink retargeted mid-lookup files the content
            # under the identity of what was really read (a later lookup of the old target misses).
            with open(path, encoding="utf-8-sig", errors="replace") as fh:
                s = os.fstat(fh.fileno())
                text = fh.read()
            key = (str(path), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    with suppress(Exception):
        if text is None:
            text = _read_skill_text(path) if content is None else content
        result = parser(text)[0]
        if key is not None and isinstance(result, dict):
            if len(_FRONTMATTER_CACHE) >= _FRONTMATTER_CACHE_MAX:
                _FRONTMATTER_CACHE.clear()
            _FRONTMATTER_CACHE[key] = (parser, copy.deepcopy(result))
            if persisted_home is not None:
                with suppress(Exception):
                    from tools import skill_frontmatter_index as _index
                    _index.store(persisted_home, key[0], key[1:], result)
        return result
    return {}


def _available_skill_files(skill_dir: Path) -> dict[str, list[str]]:
    """Non-SKILL.md files grouped by support dir (+ "other" for known source extensions
    elsewhere); empty groups dropped."""
    groups: dict[str, list[str]] = {}
    for f in skill_dir.rglob("*"):
        if not f.is_file() or f.name == "SKILL.md":
            continue
        rel = f.relative_to(skill_dir).as_posix()
        top = rel.split("/", 1)[0] if "/" in rel else None
        if top in _SUPPORT_DIRS or f.suffix in _SKILL_FILE_EXTS:
            groups.setdefault(top if top in _SUPPORT_DIRS else "other", []).append(rel)
    return {k: groups[k] for k in (*_SUPPORT_DIRS, "other") if k in groups}


def _inline_reference(skill_root: Path, file_path: str, label: str, mark_read: bool) -> str | None:
    """Recover a section citation mistaken for a reference path; never guess a file.

    Only exact heading/leading-bold slugs (optionally qualified with ``for ...``)
    qualify. Serve the whole source so surrounding rules are not silently omitted.
    """
    from tools.path_security import validate_within_dir

    requested = re.fullmatch(r"references/([a-z0-9]+(?:-[a-z0-9]+)+)\.md", file_path)
    source = skill_root / "SKILL.md"
    if not requested or validate_within_dir(source, skill_root) or not source.is_file():
        return None
    try:
        content = _read_skill_text(source)
    except OSError:
        return None
    slug = requested[1]
    matches = []
    fence = ""
    frontmatter = content.startswith("---\n")
    for index, line in enumerate(content.splitlines()):
        if frontmatter:
            if index and line.strip() == "---":
                frontmatter = False
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}\s*", line):
                fence = ""
            continue
        if marker:
            fence = marker[1]
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$", line)
        bold = re.match(r"^\*\*([^*]+)\*\*(?:[.:]|\s*$)", line)
        if not (section := heading or bold):
            continue
        title = section[1].strip()
        title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        if title_slug == slug or title_slug.startswith(slug + "-for-"):
            matches.append(title)
    if len(matches) != 1:
        return None
    if mark_read:
        _mark_background_review_read(source)
    return _json({
        "success": True, "name": label, "file": "SKILL.md", "file_type": ".md",
        "requested_file": file_path, "inline_section": matches[0], "content": content,
        "hint": "The requested file does not exist; this section is inline in SKILL.md. Full source returned."})


def _serve_skill_file(
    skill_root: Path, file_path: str, label: str, *, hint: str | None = None,
    list_available: bool = False, read_error_prefix: bool = False, mark_read: bool = False) -> str:
    """Serve one linked file from a skill directory as a skill_view JSON result. ``hint``
    decorates traversal/containment errors; ``list_available`` adds the available-files listing
    on not-found (local); ``read_error_prefix`` wraps non-decode read errors (plugin) instead
    of propagating to the caller's handler."""
    from tools.path_security import has_traversal_component, validate_within_dir
    extra = {"hint": hint} if hint else {}
    if has_traversal_component(file_path):
        return _fail("Path traversal ('..') is not allowed.", **extra)
    target = skill_root / file_path
    if path_error := validate_within_dir(target, skill_root):
        return _fail(path_error, **extra)
    # is_file(), not exists(): a bare directory must take the not-found branch.
    if not target.is_file():
        if not target.exists() and (recovered := _inline_reference(skill_root, file_path, label, mark_read)):
            return recovered
        listing = {} if not list_available else {
            "available_files": _available_skill_files(skill_root),
            "hint": "Use one of the available file paths listed above"}
        return _fail(f"File '{file_path}' not found in skill '{label}'.", **listing)
    try:
        content = _read_skill_text(target)
    except UnicodeDecodeError:
        return _json({
            "success": True, "name": label, "file": file_path, "is_binary": True,
            "content": f"[Binary file: {target.name}, size: {target.stat().st_size} bytes]"})
    except Exception as exc:
        if not read_error_prefix:
            raise
        return _fail(f"Failed to read '{file_path}': {exc}")
    if mark_read:
        _mark_background_review_read(target)
    return _json({
        "success": True, "name": label, "file": file_path, "content": content,
        "file_type": target.suffix})


def _mark_background_review_read(path: Path) -> None:
    try:
        from tools.skill_manager_guards import mark_background_review_skill_read
        mark_background_review_skill_read(path)
    except Exception:
        logger.debug("Could not record background-review skill read for %s", path, exc_info=True)


def _preprocess_skill(content: str, skill_dir, session_id, debug_msg: str, *args) -> str:
    """Apply the configured SKILL.md preprocessing; on failure log and serve raw."""
    try:
        from agent.skill_preprocessing import preprocess_skill_content
        return preprocess_skill_content(content, skill_dir, session_id=session_id)
    except Exception:
        logger.debug(debug_msg, *args, exc_info=True)
        return content


def _serve_plugin_skill(
    skill_md: Path, namespace: str, bare: str, file_path: str | None = None, *,
    preprocess: bool = True, session_id: str | None = None) -> str:
    """Read a plugin-provided skill, apply guards, return JSON."""
    from hermes_cli.plugins import _get_disabled_plugins, get_plugin_manager
    from hermes_cli.plugins_content import plugin_skill_owners
    from tools import skills_tool as _st
    qualified_name = f"{namespace}:{bare}"
    if disabled := sorted(plugin_skill_owners(get_plugin_manager(), qualified_name) & _get_disabled_plugins()):
        return _fail(f"Plugin '{disabled[0]}' is disabled. Re-enable with: hermes plugins enable {disabled[0]}")
    try:
        content = _read_skill_text(skill_md)
    except Exception as e:
        return _fail(f"Failed to read skill '{qualified_name}': {e}")
    parsed_frontmatter = _safe_frontmatter(content=content)
    if _st._is_skill_disabled(qualified_name):
        return _fail(f"Skill '{qualified_name}' is disabled.")
    if not _st.skill_matches_platform(parsed_frontmatter):
        return _fail(f"Skill '{qualified_name}' is not supported on this platform.",
                     readiness_status=SkillReadinessStatus.UNSUPPORTED.value)
    if file_path:
        return _serve_skill_file(skill_md.parent, file_path, qualified_name, read_error_prefix=True)
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        logger.warning(
            "Plugin skill '%s:%s' contains patterns that may indicate prompt injection", namespace, bare)
    banner = ""
    with suppress(Exception):  # bundle-context banner: sibling skills of the same plugin
        siblings = [s for s in get_plugin_manager().list_plugin_skills(namespace) if s != bare]
        banner = f"[Bundle context: This skill is part of the '{namespace}' plugin." + (
            f"\nSibling skills: {', '.join(siblings)}.\nUse qualified form to invoke siblings "
            f"(e.g. {namespace}:{siblings[0]})." if siblings else "") + "]\n\n"
    rendered_content = content if not preprocess else _preprocess_skill(
        content, skill_md.parent, session_id, "Could not preprocess plugin skill %s:%s",
        namespace, bare)
    return _json({
        "success": True, "name": qualified_name, "content": banner + rendered_content,
        "description": _truncate_description(str(parsed_frontmatter.get("description", ""))),
        "linked_files": _plugin_skill_linked_files(skill_md.parent),
        "readiness_status": SkillReadinessStatus.AVAILABLE.value,
        "skill_dir": str(skill_md.parent)})


def _plugin_skill_linked_files(skill_root: Path) -> dict[str, list[str]] | None:
    from tools.path_security import validate_within_dir
    linked: dict[str, list[str]] = {}
    for category in _SUPPORT_DIRS:
        files = [
            str(path.relative_to(skill_root)) for path in sorted((skill_root / category).rglob("*"))
            if path.is_file() and validate_within_dir(path, skill_root) is None]
        if files:
            linked[category] = files
    return linked or None
