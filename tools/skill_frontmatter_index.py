"""Cross-process index of parsed SKILL.md frontmatter, revalidated by stat. [daveey]

A fresh process that resolves a pinned skill (the kanban admission helper) parses every
SKILL.md under each search root to match frontmatter ``name:`` aliases: ~270 YAML reads, which
cost 3-9 s under macOS background-tier I/O. This index lets that process stat each file and
reuse the parse a previous process made of the very same bytes.

Rules, all of which keep results identical to a fresh parse:

* An entry is keyed by the path string looked up and the file's identity at lookup time
  (st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns of the target; a retargeted symlink or any
  edit changes it). Every lookup stats the file; a mismatch is a miss and the file is re-read.
* The index is stamped with :func:`parser_token`: a digest of the parsing code
  (``agent.skill_utils.parse_frontmatter`` / ``yaml_load``) and the YAML modules' files. Another
  token discards the whole index. Callers only use it with the default parser.
* Only values that survive a JSON round trip unchanged are stored (YAML dates, tuples, non-str
  keys are parsed fresh every time).
* The file lives at ``<HERMES_HOME>/cache/skill_frontmatter_index.json`` (0600, atomic replace);
  a missing, unreadable or malformed index is treated as empty.
"""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

INDEX_VERSION = 1
_MAX_ENTRIES = 8192
_FLUSH_EVERY = 64  # new entries between writes, so a killed process still leaves most of its work

_lock = threading.RLock()
_state: dict[str, Any] = {"home": None, "token": None, "entries": None, "dirty": 0}
_atexit_registered = False
_token_cache: Optional[str] = None
_token_functions: tuple = ()  # the YAML entry points the cached token was computed from


def _index_path(home: Path) -> Path:
    return home / "cache" / "skill_frontmatter_index.json"


def _module_file_stat(name: str) -> list:
    try:
        spec = importlib.util.find_spec(name)
        origin = spec.origin if spec is not None else None
        st = os.stat(origin) if origin else None
        return [name, origin, st.st_size if st else None, st.st_mtime_ns if st else None]
    except (ImportError, ValueError, OSError) as exc:  # an unknown module still yields a stable token
        return [name, type(exc).__name__]


def _code_digest(code) -> str:
    """Digest of a code object's behaviour-bearing parts. (``marshal.dumps`` is not used: its
    output varies with object reference counts, so equal code can hash differently.)"""
    h = hashlib.sha256()

    def feed(c) -> None:
        h.update(c.co_code)
        h.update(repr((c.co_names, c.co_varnames, c.co_freevars, c.co_cellvars)).encode())
        for const in c.co_consts:
            if hasattr(const, "co_code"):
                feed(const)
            else:
                h.update(repr(const).encode())
    feed(code)
    return h.hexdigest()


def parser_token() -> str:
    """Digest of the default frontmatter parser's code and the YAML stack it calls."""
    global _token_cache, _token_functions
    if _token_cache is None:
        from agent import skill_utils

        import hermes_yaml

        # The loaded code of the parser and of hermes_yaml's entry points (what this process runs),
        # plus the on-disk stat of the modules and the YAML library underneath.
        parts = [INDEX_VERSION, sys.version, _code_digest(skill_utils.parse_frontmatter.__code__),
                 _code_digest(skill_utils.yaml_load.__code__),
                 _code_digest(hermes_yaml.safe_load.__code__), _code_digest(hermes_yaml._load.__code__),
                 _module_file_stat("agent.skill_utils"), _module_file_stat("hermes_yaml"), _module_file_stat("yaml")]
        _token_cache = hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()
        _token_functions = _yaml_entry_points()
    return _token_cache


def _yaml_entry_points() -> tuple:
    import hermes_yaml
    from agent import skill_utils

    # yaml_load binds hermes_yaml.safe_load on its first call (skill_utils._yaml_load_fn)
    return (skill_utils.yaml_load, skill_utils._yaml_load_fn or hermes_yaml.safe_load)


def default_parser_in_use(parser) -> bool:
    """True when ``parser`` is the stock delegate AND it still delegates to the stock function
    (a patched ``agent.skill_utils.parse_frontmatter`` changes the token, but be explicit)."""
    from agent import skill_utils
    from tools import skills_tool

    if not (parser is skills_tool._DEFAULT_PARSE_FRONTMATTER
            and skill_utils.parse_frontmatter is skill_utils._DEFAULT_PARSE_FRONTMATTER):
        return False
    global _token_functions
    # A YAML entry point rebound after the token was taken (a test's monkeypatch) parses
    # differently from what the token names.
    current = _yaml_entry_points()
    if not _token_functions or _token_functions[1] is None:
        _token_functions = current
    return all(a is b for a, b in zip(current, _token_functions, strict=True))


def _load(home: Path) -> dict:
    """Entries for ``home`` (loaded once per home per process)."""
    if _state["home"] == str(home) and _state["entries"] is not None:
        return _state["entries"]
    _flush_locked()
    entries: dict = {}
    try:
        blob = json.loads(_index_path(home).read_text(encoding="utf-8-sig"))
        if (isinstance(blob, dict) and blob.get("version") == INDEX_VERSION
                and blob.get("token") == parser_token() and isinstance(blob.get("entries"), dict)):
            entries = {k: v for k, v in blob["entries"].items()
                       if isinstance(k, str) and isinstance(v, list) and len(v) == 6
                       and all(isinstance(x, int) for x in v[:5]) and isinstance(v[5], dict)}
    except (OSError, ValueError):
        pass
    _state.update(home=str(home), token=parser_token(), entries=entries, dirty=0)
    return entries


def lookup(home: Path, path: str, ident: tuple) -> Optional[dict]:
    """The recorded frontmatter of ``path`` if it was parsed at exactly ``ident``; else None.
    Returns a fresh object (decoded from the stored JSON), so callers may mutate it."""
    with _lock:
        entry = _load(home).get(path)
        if entry is None or tuple(entry[:5]) != tuple(ident):
            return None
        return json.loads(json.dumps(entry[5]))


def _json_exact(value: Any) -> bool:
    try:
        return json.loads(json.dumps(value, allow_nan=False)) == value and _same_types(value, json.loads(json.dumps(value)))
    except (TypeError, ValueError, RecursionError):
        return False


def _same_types(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_types(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_types(x, y) for x, y in zip(a, b))
    return True


def store(home: Path, path: str, ident: tuple, value: dict) -> None:
    """Record the parse of ``path`` made from bytes whose identity was ``ident``."""
    global _atexit_registered
    if not isinstance(value, dict) or not _json_exact(value):
        return
    with _lock:
        entries = _load(home)
        entries[path] = [*ident, value]
        _state["dirty"] += 1
        if not _atexit_registered:
            atexit.register(flush)
            _atexit_registered = True
        if _state["dirty"] >= _FLUSH_EVERY:
            _flush_locked()


def flush() -> None:
    with _lock:
        _flush_locked()


def _flush_locked() -> None:
    home, entries = _state["home"], _state["entries"]
    if not home or entries is None or not _state["dirty"]:
        return
    _state["dirty"] = 0
    try:
        from utils import atomic_json_write

        path = _index_path(Path(home))
        merged: dict = {}
        try:  # keep what other processes recorded meanwhile; ours wins on the same path
            blob = json.loads(path.read_text(encoding="utf-8-sig"))
            if (isinstance(blob, dict) and blob.get("version") == INDEX_VERSION
                    and blob.get("token") == _state["token"] and isinstance(blob.get("entries"), dict)):
                merged.update(blob["entries"])
        except (OSError, ValueError):
            pass
        merged.update(entries)
        if len(merged) > _MAX_ENTRIES:  # drop entries whose file is gone, then the oldest inserted
            merged = {k: v for k, v in merged.items() if os.path.exists(k)}
            merged = dict(list(merged.items())[-_MAX_ENTRIES:])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"version": INDEX_VERSION, "token": _state["token"], "entries": merged},
                          indent=0, mode=0o600)
    except Exception:
        logger.debug("skill frontmatter index write failed", exc_info=True)


def _reset_for_tests() -> None:
    global _token_cache, _token_functions
    with _lock:
        _state.update(home=None, token=None, entries=None, dirty=0)
        _token_cache = None
        _token_functions = ()
