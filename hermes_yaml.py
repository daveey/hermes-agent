"""Shared YAML 1.1 policy for config, manifests, and frontmatter.

Ruamel's native schema includes bare y/n booleans and rejects duplicate keys.
Loaders are reused per thread; emitters/editors remain operation-owned.
"""

import hashlib
import marshal
import os
import threading
from io import StringIO
from pathlib import Path
from typing import Any, IO, overload

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.resolver import VersionedResolver


class _Yaml11Resolver(VersionedResolver):
    # Quote strings like "off" without adding a %YAML directive to every config/snippet.
    @property
    def processing_version(self) -> tuple[int, int]:
        return (1, 1)


_loaders = threading.local()


def _load(document: str | bytes, *, pure: bool) -> Any:
    # Reuse only successful parsers, per thread and parser implementation. Pop
    # while loading so reentrant calls also own a distinct instance. Ruamel
    # documents that an instance may not be reusable after a parse error.
    if not hasattr(_loaders, 'parsers'):
        _loaders.parsers = {}
    yaml = _loaders.parsers.pop(pure, None)
    if yaml is None:
        yaml = YAML(typ="safe", pure=pure)
    yaml.version = (1, 1)
    yaml.doc_infos.clear()
    value = yaml.load(document)
    _loaders.parsers[pure] = yaml
    return value


# Parsed-document cache. The pure-Python parser costs ~0.07 s per 120 KB config and ~0.4 s for the
# largest bundled locale; a freshly started backend parses every profile's config plus every locale
# while serving its first requests, so those parses compete with them for the GIL. Large documents are
# therefore kept on disk, keyed by the sha256 of their exact bytes (+ parser version), so a restart
# re-reads instead of re-parsing. ``marshal`` only round-trips plain str/int/float/bool/None/list/dict
# and loads without executing code; a document with anything else (timestamps, ...) is not cached.
_CACHE_MIN_BYTES = 16 * 1024
_CACHE_MAX_ENTRIES = 256
_CACHE_TAG = "hy1"


def _cache_dir() -> Path | None:
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root() / "cache" / "yaml-parse"
    except (ImportError, OSError):
        return None


def _cache_key(document: bytes) -> str:
    import ruamel.yaml

    h = hashlib.sha256(f"{_CACHE_TAG}|{ruamel.yaml.__version__}|".encode())
    h.update(document)
    return h.hexdigest()


def _cache_read(path: Path) -> tuple[bool, Any]:
    try:
        return True, marshal.loads(path.read_bytes())
    except (OSError, ValueError, EOFError, TypeError):
        return False, None


def _cache_write(directory: Path, path: Path, value: Any) -> None:
    try:
        blob = marshal.dumps(value)
        directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        entries = sorted(directory.glob("*.bin"), key=lambda p: p.stat().st_mtime)
        for old in entries[: max(0, len(entries) - _CACHE_MAX_ENTRIES)]:
            old.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def safe_load(stream: str | bytes | IO[str] | IO[bytes]) -> Any:
    """Read standard YAML data; existing configs use YAML 1.1 booleans.

    The pure parser defines what parses: Windows ARM64 has no C extension, and libyaml rejects
    documents the pure parser accepts (``[{url: http://h}]``), so a C rejection is re-read pure.
    """
    document = stream if isinstance(stream, (str, bytes)) else stream.read()
    raw = document.encode("utf-8") if isinstance(document, str) else document
    directory = _cache_dir() if len(raw) >= _CACHE_MIN_BYTES else None
    path = directory / f"{_cache_key(raw)}.bin" if directory is not None else None
    if path is not None:
        hit, value = _cache_read(path)
        if hit:
            return value
    try:
        value = _load(document, pure=False)
    except YAMLError:
        value = _load(document, pure=True)
    if path is not None:
        _cache_write(directory, path, value)
    return value


@overload
def safe_dump(
    data: Any, stream: None = None, *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> str: ...


@overload
def safe_dump(
    data: Any, stream: IO[str], *, default_flow_style: bool = False,
    sort_keys: bool = True, allow_unicode: bool = True, width: int = 80,
) -> None: ...


def safe_dump(
    data: Any,
    stream: IO[str] | None = None,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = True,
    allow_unicode: bool = True,
    width: int = 80,
) -> str | None:
    """Write standard YAML data with readable Unicode and indented block lists."""
    # The C emitter ignores sequence offsets and escapes astral Unicode.
    yaml = YAML(typ="safe", pure=True)
    yaml.Resolver = _Yaml11Resolver
    yaml.default_flow_style = default_flow_style
    yaml.allow_unicode = allow_unicode
    yaml.width = width
    yaml.sort_base_mapping_type_on_output = sort_keys
    yaml.indent(mapping=2, sequence=4, offset=2)
    if stream is not None:
        yaml.dump(data, stream)
        return None
    output = StringIO()
    yaml.dump(data, output)
    return output.getvalue()


# ruamel's emitter can change a double-quoted value when it folds a long line right after an
# escaped backslash (``D:\\Cent…`` → ``D:\\`` + bare newline): the fold reloads as a literal space
# and a no-op save mutates the stored value (#119844). Config writes must be value-preserving, so
# every round-trip emitter in the tree keeps scalars on one line instead of folding (``None``
# does NOT disable folding on 0.18.x; only a large width does).
ROUNDTRIP_YAML_WIDTH = 2**31 - 1


def roundtrip_yaml() -> YAML:
    """Create a fresh comment/quote-preserving editor for user-authored YAML."""
    yaml = YAML(typ="rt")
    yaml.width = ROUNDTRIP_YAML_WIDTH
    yaml.Resolver = _Yaml11Resolver
    yaml.preserve_quotes = True
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml
