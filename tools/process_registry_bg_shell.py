"""Login-shell argv/env for background commands (spawn_local).

The user's login shell loads the rc files (#42203), then ``exec``s the same bash the foreground
terminal uses, so word splitting, ``PIPESTATUS`` etc. match between foreground and background.
The command text rides in ``HERMES_BG_COMMAND`` (set on the spawn env), never re-quoted into argv.
"""

import shlex

from tools.environments.local import _IS_WINDOWS, _find_bash, _find_shell


def bg_login_argv(command: str) -> list[str]:
    if _IS_WINDOWS:  # unchanged: Windows keeps the command in argv
        return [_find_shell(), "-lic", f"set +m; {command}"]
    return [_find_shell(), "-lic",
            'set +m; _hc=$HERMES_BG_COMMAND; unset HERMES_BG_COMMAND; '
            f'exec {shlex.quote(_find_bash())} -c "$_hc"']


def bg_command_env(env: dict, command: str) -> dict:
    """Add the command text the login shell hands to bash."""
    if not _IS_WINDOWS:
        env["HERMES_BG_COMMAND"] = command
    return env
