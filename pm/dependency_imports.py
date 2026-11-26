"""Keep duplicate checkout-installed wheel modules behind their managed owner.

A stray ``uv pip install --target .`` must not supersede the same distribution
in PM's committed environment. Limit this correction to exact modules claimed
by both distributions' RECORDs, not entire top-level namespaces. Core sources,
editable members and other namespace portions keep their original import rules.
No local file is removed, moved, or rewritten; attestation still verifies bytes.
"""
from __future__ import annotations

import csv
import importlib.machinery
import json
from pathlib import Path, PurePosixPath
import re
import sys

_IMPORT_SUFFIXES = tuple(sorted(
    (*importlib.machinery.SOURCE_SUFFIXES, *importlib.machinery.EXTENSION_SUFFIXES),
    key=len, reverse=True,
))


def _distribution_name(info: Path) -> str:
    metadata = info / "METADATA"
    if metadata.is_file():
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.startswith("Name: "):
                return re.sub(r"[-_.]+", "-", line[6:].strip()).lower()
    return ""


def _recorded_modules(info: Path) -> dict[str, str]:
    modules: dict[str, str] = {}
    record = info / "RECORD"
    if not record.is_file():
        return modules
    with record.open(encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream):
            if not row:
                continue
            path = PurePosixPath(row[0])
            if path.is_absolute() or ".." in path.parts:
                continue
            suffix = next((s for s in _IMPORT_SUFFIXES if path.name.endswith(s)), None)
            if suffix is None:
                continue
            stem = path.name[:-len(suffix)]
            parts = (*path.parts[:-1], *((stem,) if stem != "__init__" else ()))
            if parts and all(part.isidentifier() for part in parts):
                modules[".".join(parts)] = str(path.parent.parent if stem == "__init__" else path.parent)
    return modules


def _duplicate_modules(project_root: Path, selected: Path) -> dict[str, str]:
    duplicate: dict[str, set[str]] = {}
    for info in project_root.glob("*.dist-info"):
        name = _distribution_name(info)
        if name:
            duplicate.setdefault(name, set()).update(_recorded_modules(info))
    owners: dict[str, str] = {}
    if not duplicate:
        return owners
    for info in selected.glob("*.dist-info"):
        name = _distribution_name(info)
        if name not in duplicate:
            continue
        direct = info / "direct_url.json"
        if direct.is_file() and json.loads(direct.read_text(encoding="utf-8")).get("dir_info", {}).get("editable"):
            continue  # Source workspaces never become wheel-owned.
        for module, parent in _recorded_modules(info).items():
            if module in duplicate[name]:
                owners[module] = str(selected / parent)
    return owners


class _DuplicateWheelFinder:
    def __init__(self, owners: dict[str, str]):
        self.owners = owners

    def find_spec(self, fullname, path=None, target=None):
        parent = self.owners.get(fullname)
        if parent is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, [parent], target)
        if spec is None:
            raise ModuleNotFoundError(f"selected dependency source missing: {fullname}", name=fullname)
        return spec


def select_wheel_sources(project_root: Path, selected: Path) -> None:
    """Select duplicate wheel modules before .pth execution, without evicting code."""
    owners = _duplicate_modules(project_root, selected)
    for name in owners:
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None)
        if origin:
            loaded = Path(origin).resolve()
            if loaded.is_relative_to(project_root.resolve()) and not loaded.is_relative_to(selected.resolve()):
                raise RuntimeError(f"checkout dependency already imported before PM activation: {name}")
    sys.meta_path[:] = [entry for entry in sys.meta_path if not isinstance(entry, _DuplicateWheelFinder)]
    if owners:
        index = sys.meta_path.index(importlib.machinery.PathFinder)
        sys.meta_path.insert(index, _DuplicateWheelFinder(owners))
