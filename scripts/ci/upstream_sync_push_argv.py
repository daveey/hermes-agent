"""Parse literal Git push argv; option values are not refspecs or config keys."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Push:
    config: list[str] = field(default_factory=list)
    target: str | None = None
    specs: list[str] = field(default_factory=list)
    mirror: bool = False
    refusal: str | None = None
    command: str | None = None


# Optional values are accepted only after '=' (Git's parse-options convention).
_VALUES = {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
_OPTIONAL = {"--force-with-lease", "--signed", "--recurse-submodules"}
_FLAGS = {"-q", "--quiet", "-v", "--verbose", "-u", "--set-upstream", "-f", "--force",
          "--no-force", "--force-if-includes", "--no-force-if-includes", "--no-force-with-lease",
          "--no-verify", "--verify", "--porcelain", "--progress", "--no-progress", "--thin",
          "--no-thin", "--atomic", "--no-atomic", "--follow-tags", "--no-follow-tags",
          "-n", "--dry-run", "--prune", "-d", "--delete", "--ipv4", "--ipv6", "-4", "-6"}


def _globals(args: list[str], result: Push) -> int:
    """Return the subcommand offset, or set a refusal for an unknown global option."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        arg = args[i]
        if arg in {"-c", "--config-env"}:
            if i + 1 >= len(args):
                result.refusal = "unknown"
                return i
            result.config.extend(args[i:i + 2])
            i += 2
        elif arg.startswith("--config-env=") or (arg.startswith("-c") and len(arg) > 2):
            result.config.append(arg)
            i += 1
        elif arg in {"--no-pager", "--paginate"}:
            i += 1
        else:
            result.refusal = "unknown"
            return i
    return i


def parse(argv: list[str]) -> Push:
    """Unknown or incomplete grammar refuses, never guesses which token is a target."""
    result = Push()
    args = list(argv)
    if args and Path(args[0]).name in {"git", "git.exe"}:
        args.pop(0)
    i = _globals(args, result)
    if result.refusal:
        return result
    if i == len(args):
        result.refusal = "unknown"
        return result
    result.command = args[i]
    if result.command != "push":
        result.refusal = "unknown"
        return result
    i += 1
    positional = []
    options = True
    repo_option = None
    while i < len(args):
        arg = args[i]
        key, eq, value = arg.partition("=")
        i += 1
        if options and arg == "--":
            options = False
        elif options and key in _VALUES:
            if not eq:
                if i == len(args):
                    result.refusal = "unknown"
                    return result
                value = args[i]
                i += 1
            if key == "--repo":
                repo_option = value
        elif options and (key in _OPTIONAL or (arg.startswith("-o") and not arg.startswith("--") and len(arg) > 2)):
            pass
        elif options and arg in {"--mirror", "--no-mirror"}:
            result.mirror = arg == "--mirror"
        elif options and arg in {"--all", "--branches", "--tags"}:
            result.refusal = "broad"
        elif options and arg in _FLAGS:
            pass
        elif options and arg.startswith("-"):
            result.refusal = "unknown"
            return result
        else:
            positional.append(arg)
    if repo_option is not None:
        if positional:
            result.refusal = "unknown"
        result.target = repo_option
    elif positional:
        result.target, result.specs = positional[0], positional[1:]
    return result
