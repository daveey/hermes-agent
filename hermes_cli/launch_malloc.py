"""Exec-time Darwin malloc policy, scrubbed before servers launch child commands.

Stdlib only: bootstrap imports this before activating dependencies.
"""

from __future__ import annotations

import os
import sys

ENV = "MallocSpaceEfficient"
MARK = "HERMES_LAUNCH_MALLOC_SET"
OPT_OUT = "HERMES_MALLOC_SPACE_EFFICIENT"


def server_argv(argv: list[str]) -> bool:
    """True when *argv* (``sys.argv[1:]``) launches a long-lived server: ``serve``, ``dashboard`` or a
    foreground ``gateway`` (``gateway`` / ``gateway run``)."""
    from hermes_cli._parser import command_argv

    args = command_argv(argv)
    if not args:
        return False
    if args[0] in ("serve", "dashboard"):
        return True
    return args[0] == "gateway" and (len(args) == 1 or args[1] == "run" or args[1].startswith("-"))


def launch_env(argv, environ=os.environ, platform=sys.platform) -> dict[str, str]:
    """Environment delta for server exec; an operator's malloc setting always wins."""
    if (platform != "darwin" or not server_argv(argv) or ENV in environ
            or environ.get(OPT_OUT, "").lower() in ("0", "false", "no", "off")):
        return {}
    return {ENV: "1", MARK: "1"}


def ensure(argv, *, execv=os.execv) -> None:
    """Re-exec once so libmalloc reads the setting, then hide it from child commands."""
    if MARK in os.environ:
        os.environ.pop(MARK, None)
        os.environ.pop(ENV, None)
        return
    settings = launch_env(argv, os.environ, sys.platform)
    if not settings:
        return
    os.environ.update(settings)
    try:
        execv(sys.executable, sys.orig_argv)
    except OSError:
        # Best effort: a denied exec must neither break startup nor leak to children.
        os.environ.pop(MARK, None)
        os.environ.pop(ENV, None)
