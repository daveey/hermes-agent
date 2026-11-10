"""Resolve a workflow ``run: just <recipe>`` line to the commands the recipe runs.

Workflow invariants that assert a command's shape must see through the justfile
indirection: upstream moved inline step commands into recipes (``just
js-install-tests``), and a contract over the YAML text alone then counts nothing.
This is a deliberately small reader for the subset of just syntax such recipes
use: plain dependencies, linewise bodies, ``@``/``-`` line prefixes, and nested
``just <recipe>`` lines. Anything outside that subset (parameters, interpolation,
shebang bodies, OS-specific variants, imports/modules) raises instead of guessing,
so a recipe the reader cannot model fails the invariant rather than passing it.
tests/ci/test_runtime_install_shape.py checks it against the real ``just --dry-run``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_HEADER = re.compile(r"^(@?)([A-Za-z_][A-Za-z0-9_-]*)([^:]*?):(?!=)(.*)$")
_ATTRIBUTE = re.compile(r"^\[(.+)\]\s*$")
_JUST_LINE = re.compile(r"^just(?:\s+(.*))?$")
_RECIPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_OS_ATTRIBUTES = {"unix", "windows", "linux", "macos", "openbsd", "freebsd", "netbsd", "dragonfly", "android", "ios"}


class UnsupportedRecipe(ValueError):
    """The recipe uses just syntax this reader does not model."""


@dataclass
class Recipe:
    name: str
    attributes: list[str] = field(default_factory=list)
    parameters: str = ""
    dependencies: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)


def parse(path: Path) -> dict[str, list[Recipe]]:
    """Every recipe definition by name (a name may repeat under OS attributes)."""
    recipes: dict[str, list[Recipe]] = {}
    attributes: list[str] = []
    current: Recipe | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if current is not None and (raw[:1] in (" ", "\t") or not raw.strip()):
            if raw.strip():
                current.body.append(raw.strip())
            continue
        current = None
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^(import|mod)\b", line):
            raise UnsupportedRecipe(f"{path}: {line!r} (imports and modules are not modelled)")
        attribute = _ATTRIBUTE.match(line)
        if attribute:
            attributes += [a.strip() for a in attribute.group(1).split(",")]
            continue
        if re.match(r"^(set|alias|export)\b", line) or re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:=", line):
            attributes = []
            continue
        header = _HEADER.match(line)
        if not header:
            raise UnsupportedRecipe(f"{path}: cannot read justfile line {line!r}")
        current = Recipe(name=header.group(2), attributes=attributes, parameters=header.group(3).strip())
        deps = header.group(4).split("#", 1)[0].strip()
        if "(" in deps or "&&" in deps:
            current.dependencies = [deps]  # kept verbatim; rejected when resolved
        else:
            current.dependencies = deps.split()
        recipes.setdefault(current.name, []).append(current)
        attributes = []
    return recipes


def _definition(recipes: dict[str, list[Recipe]], name: str) -> Recipe:
    if name not in recipes:
        raise UnsupportedRecipe(f"no recipe {name!r} in the justfile")
    variants = recipes[name]
    if len(variants) != 1 or any(a in _OS_ATTRIBUTES for a in variants[0].attributes):
        raise UnsupportedRecipe(f"recipe {name!r} has OS-specific variants; resolve per OS explicitly")
    recipe = variants[0]
    if recipe.parameters or "positional-arguments" in recipe.attributes or "script" in recipe.attributes:
        raise UnsupportedRecipe(f"recipe {name!r} takes parameters or runs as a script")
    if recipe.body[:1] and recipe.body[0].startswith("#!"):
        raise UnsupportedRecipe(f"recipe {name!r} has a shebang body")
    return recipe


def recipe_commands(justfile: Path, name: str, _stack: tuple[str, ...] = (), _recipes=None, *,
                    follow_just: bool = True) -> list[str]:
    """The shell commands ``just <name>`` runs, dependencies first, nested recipes expanded.

    ``follow_just=False`` keeps a body's ``just <recipe>`` lines as they are, which is
    what ``just --dry-run`` prints (it does not descend into a nested invocation).
    """
    recipes = _recipes if _recipes is not None else parse(justfile)
    if name in _stack:
        raise UnsupportedRecipe(f"recipe cycle: {' -> '.join(_stack + (name,))}")
    recipe = _definition(recipes, name)
    commands: list[str] = []
    for dep in recipe.dependencies:
        if not _RECIPE_NAME.match(dep):
            raise UnsupportedRecipe(f"recipe {name!r} has a non-plain dependency {dep!r}")
        commands += recipe_commands(justfile, dep, _stack + (name,), recipes, follow_just=follow_just)
    joined: list[str] = []
    for line in recipe.body:
        if joined and joined[-1].endswith("\\"):
            joined[-1] = joined[-1][:-1].rstrip() + " " + line
        else:
            joined.append(line)
    for line in joined:
        if "{{" in line:
            raise UnsupportedRecipe(f"recipe {name!r} interpolates: {line!r}")
        line = line.lstrip("@-").strip()
        commands += expand(justfile, line, _stack + (name,), recipes) if follow_just else [line]
    return commands


def expand(justfile: Path, command: str, _stack: tuple[str, ...] = (), _recipes=None) -> list[str]:
    """``command`` itself, or the commands of the recipe it runs when it is ``just <recipe>``."""
    command = command.strip()
    just = _JUST_LINE.match(command)
    if not just:
        return [command]
    target = (just.group(1) or "").strip()
    if not _RECIPE_NAME.match(target):
        raise UnsupportedRecipe(f"{command!r}: only `just <recipe>` with no flags or arguments is modelled")
    return recipe_commands(justfile, target, _stack, _recipes)
