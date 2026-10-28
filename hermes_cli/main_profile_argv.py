"""Import-time ``-p``/``--profile`` scan for the Hermes CLI.

``hermes_cli.main`` imports this before profile resolution. Keep the module
free of import-time Hermes imports: other modules cache ``HERMES_HOME`` at
import, and this file is loaded before that home is chosen.
"""

from __future__ import annotations

import os
import re
import sys

_PROFILE_NAME_RE = r"^[a-z0-9][a-z0-9_-]{0,63}$"  # mirrors hermes_cli.profiles._PROFILE_ID_RE


def _inside_mcp_add_args(argv: list, index: int) -> bool:
    """True once argv reaches `hermes mcp add ... --args <command argv>`.

    ``mcp add --args`` is command-argv passthrough. Flags after that point
    belong to the child MCP command (for example Docker MCP Toolkit's
    ``--profile``), not to Hermes' own profile selector.
    """
    try:
        mcp_index = argv.index("mcp", 0, index)
        argv.index("add", mcp_index + 1, index)
    except ValueError:
        return False
    return True


def _looks_like_hermes_invocation() -> bool:
    """True only when this process was started as the Hermes CLI.

    pytest's separate ``-p <plugin>`` and any other entry point's argv must
    not select a profile. ``python -m hermes_cli.main`` sets ``argv[0]`` to
    this package's ``main.py``; the console script's basename is ``hermes``.
    The POSIX source launcher is ``python -c`` (``argv[0]`` is ``-c``) and
    sets ``sys._hermes_pin_default_home`` before this import
    (``hermes_cli._launchers.PIN_DEFAULT_HOME_FLAG``). Both are required:
    a bare ``python -c`` has no mark, and ``hermes --run-module pytest``
    keeps the mark but runpy replaces ``argv[0]``, so that ``-p`` stays foreign.
    """
    # Launcher mark, not argv[0] alone: the wrapper rewrites argv[0] only
    # after this module has already scanned for ``-p``.
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 == "-c" and getattr(sys, "_hermes_pin_default_home", False):
        return True
    base = os.path.basename(argv0).lower()
    for suffix in (".exe", "-script.pyw", "-script.py"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base in {"hermes", "hermes-agent", "hermes-acp"}:
        return True
    normalized = argv0.replace("\\", "/")
    return (
        normalized.endswith("/hermes_cli/main.py")
        or normalized.endswith("/hermes_cli/main.pyc")
    )


def _exit_invalid_profile_name(value: str) -> None:
    from hermes_cli.profiles import _invalid_profile_name_error

    print(f"Error: {_invalid_profile_name_error(value)}", file=sys.stderr)
    print("Run `hermes profile list` to see your profiles.", file=sys.stderr)
    sys.exit(2)


def _looks_like_option_value(value: str) -> bool:
    """A ``-p`` value that clearly belongs to some other tool (pytest's ``-p no:xdist``, a
    third-party ``-p --flag``), never a mistyped profile name."""
    return value.startswith("-") or ":" in value


def _scan_profile_flag(argv: list) -> tuple:
    """Find -p/--profile/--profile= in argv -> (name, tokens_consumed, index).

    Historically the flag worked even after the subcommand (`hermes chat -p
    coder`), so scan broadly; stop at ``--`` and at the `mcp add --args`
    passthrough region. The value is normalised (strip + casefold, matching
    ``profiles.normalize_profile_name``) before validation so ``-p Work`` selects
    ``work``. ``_apply_profile_override`` calls this only for a Hermes CLI
    process, so another entry point's ``-p <plugin>`` is not resolved at import.
    A value that cannot be a profile name is rejected so resolve_profile_env
    never sys.exits on it; the rejection is explained (exit 2) only when the
    flag comes BEFORE the first subcommand token under a real ``hermes`` run —
    after a subcommand, ``-p`` may belong to that subcommand or a plugin
    (`hermes kanban ... -p 8080`), and option-looking values (``no:xdist``,
    ``--flag``) are always a silent skip.
    """
    from hermes_cli._parser import top_level_value_flag_sets

    value_flags, optional_value_flags = top_level_value_flag_sets()
    i = 0
    saw_subcommand = False
    while i < len(argv):
        arg = argv[i]
        if arg == "--" or (arg == "--args" and _inside_mcp_add_args(argv, i)):
            break
        if arg in {"--profile", "-p"} and i + 1 < len(argv):
            raw = argv[i + 1]
            value = raw.strip().casefold()
            if re.match(_PROFILE_NAME_RE, value):
                return value, 2, i
            if not saw_subcommand and not _looks_like_option_value(raw) and _looks_like_hermes_invocation():
                _exit_invalid_profile_name(raw)
            break
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1].strip().casefold(), 1, i
        takes_value = "=" not in arg and i + 1 < len(argv) and (
            arg in value_flags
            or (arg in optional_value_flags and not argv[i + 1].startswith("-"))
        )
        if not takes_value and not arg.startswith("-"):
            saw_subcommand = True
        i += 2 if takes_value else 1
    return None, 0, None
