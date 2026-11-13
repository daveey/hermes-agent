"""State isolation for agent-launched restart proofs, before bootstrap consumers.

Endpoint isolation (``--isolated``, ``--port 0``) is NOT state isolation: a restart-proof backend
started by an agent on the real home ran startup recovery and adopted an unrelated chat
(gateway-9aaf151a15). Two rules, enforced in ``hermes_bootstrap`` before any config, recovery or
background consumer imports, and re-checked in ``web_server`` before the server is built:

* ``hermes serve|dashboard --proof-home DIR`` (or ``HERMES_PROOF_HOME``) selects ``HERMES_HOME``.
  DIR must be an existing disposable directory: never the production root, ``<root>/profiles/*``
  or the launching agent's home (``HERMES_AGENT_HOME``), resolved through aliases and symlinks.
* An agent-originated restart-proof shape (``--isolated`` or ``HERMES_DESKTOP=1``) on a
  production home exits 2 with a safe recipe.

Agent origin is any stamp below. Tool spawners stamp it after their env scrubbing; the SSH terminal
backend re-exports it remotely; the hub-admin forced command stamps every call. Stamps are
provenance, not authentication: their absence never authorizes anything beyond today's behaviour,
and a process with arbitrary environment control is out of scope. Stdlib-only (bootstrap path).
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
from typing import Mapping, MutableMapping, NoReturn, Optional, Sequence

TOOL_CALL_ENV = "HERMES_AGENT_TOOL_CALL"  # health: allow HX002 -- process provenance stamp, not a setting
ORIGIN_ENV = "HERMES_AGENT_ORIGIN"  # health: allow HX002 -- process provenance stamp, not a setting
AGENT_HOME_ENV = "HERMES_AGENT_HOME"  # health: allow HX002 -- launching home stamp, not a setting
PROOF_HOME_ENV = "HERMES_PROOF_HOME"  # health: allow HX002 -- per-process proof selector, like HERMES_HOME
ORIGIN_KEYS = (TOOL_CALL_ENV, ORIGIN_ENV, "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK",
               "HERMES_DELEGATED_CHILD_CONTEXT")
# Stamps a Desktop launch drops (clean_launch_env, desktop/gui argv, Electron startup —
# apps/desktop/electron/leaked-worker-env.ts AGENT_PROVENANCE_KEYS mirrors this): an app an agent
# reopens is the person's, and its own HERMES_DESKTOP=1 backend must not read as agent-launched.
PROVENANCE_KEYS = (TOOL_CALL_ENV, ORIGIN_ENV, AGENT_HOME_ENV)
_BACKENDS = ("serve", "dashboard")
_LIFECYCLE_FLAGS = ("--help", "-h", "--status", "--stop")
RECIPE = ('Use an existing disposable directory: mkdir -p "$TMPDIR/hermes-proof" && '
          'hermes serve --proof-home "$TMPDIR/hermes-proof" --isolated --port 0. '
          'Never copy production state or credentials into it.')


def agent_origin(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return any(env.get(key) for key in ORIGIN_KEYS)


def _path(raw: str | Path) -> Path:
    return Path(os.path.expandvars(str(raw))).expanduser().resolve()


def production_home(home: str | Path) -> bool:
    """True for (an alias of) the platform/account root, any of its profiles, or the caller's home."""
    from hermes_constants import _get_platform_default_hermes_home, account_default_hermes_home

    target = _path(home)
    roots = {_get_platform_default_hermes_home().resolve(), account_default_hermes_home().resolve()}
    agent = os.environ.get(AGENT_HOME_ENV)
    if agent:
        caller = _path(agent)
        if target == caller:
            return True
        roots.add(caller.parent.parent if caller.parent.name == "profiles" else caller)
    for root in roots:
        profiles = root / "profiles"
        if target == root or target.is_relative_to(profiles.resolve()):
            return True
        # A named profile can itself be a symlink to a directory outside the root.
        if profiles.is_dir() and any(target == p.resolve() for p in profiles.iterdir() if p.is_dir()):
            return True
    return False


def _refuse(reason: str) -> NoReturn:
    print(f"Refused proof backend: {reason}. {RECIPE}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def validate_proof_home(raw: str | Path) -> Path:
    try:
        home = _path(raw)
        unsafe = not home.is_dir() or production_home(home)
    except (OSError, RuntimeError, ValueError):
        _refuse("--proof-home could not be resolved safely")
    if unsafe:
        _refuse("--proof-home must be an existing disposable directory, never a production or agent home")
    return home


def _production_or_unknown(home: str | Path) -> bool:
    try:
        return production_home(home)
    except (OSError, RuntimeError, ValueError):
        return True


def _current_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _scan(argv: Sequence[str]) -> Optional[tuple[bool, list[str]]]:
    """(profile flag given, backend flags) for ``hermes [top flags] serve|dashboard ...``; else None."""
    from hermes_cli._parser import _OPTIONAL_VALUE_FLAGS_FALLBACK, _VALUE_FLAGS_FALLBACK

    profile, index = False, 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-p", "--profile"):
            profile, index = True, index + 2
        elif arg.startswith("--profile="):
            profile, index = True, index + 1
        elif arg.startswith("-") and arg != "--":
            index += 2 if arg in _VALUE_FLAGS_FALLBACK and arg not in _OPTIONAL_VALUE_FLAGS_FALLBACK else 1
        else:
            break
    if index >= len(argv) or argv[index] not in _BACKENDS:
        return None
    return profile, list(argv[index + 1:])


def requested_proof_home(flags: Sequence[str]) -> Optional[str]:
    selected = None
    for pos, flag in enumerate(flags):
        if flag == "--proof-home":
            if pos + 1 >= len(flags) or flags[pos + 1].startswith("-"):
                _refuse("--proof-home needs a directory")
            selected = flags[pos + 1]
        elif flag.startswith("--proof-home="):
            selected = flag.split("=", 1)[1]
    return selected


def _enforce_agent_shape(isolated: bool) -> None:
    if agent_origin() and (isolated or os.environ.get("HERMES_DESKTOP") == "1"):
        if _production_or_unknown(_current_home()):
            _refuse("an agent-launched restart-proof backend cannot use production recovery state")


def prepare_backend_argv(argv: Optional[Sequence[str]] = None) -> None:
    """Bootstrap boundary: select or refuse the home before anything reads it."""
    scanned = _scan(list(sys.argv[1:] if argv is None else argv))
    if scanned is None:
        return
    profile, flags = scanned
    if any(flag in _LIFECYCLE_FLAGS for flag in flags):
        return
    selected = requested_proof_home(flags)
    if selected is None:
        selected = os.environ.get(PROOF_HOME_ENV)
    if selected is None:
        _enforce_agent_shape("--isolated" in flags)
        return
    if not selected.strip():
        _refuse("--proof-home is empty")
    if profile:
        _refuse("--proof-home cannot be combined with --profile")
    home = validate_proof_home(selected)
    os.environ[PROOF_HOME_ENV] = str(home)
    os.environ["HERMES_HOME"] = str(home)


def assert_backend_home(*, isolated: bool, argv: Optional[Sequence[str]] = None) -> None:
    """``web_server`` re-check before the server and lifespan consumers start.

    Config is already imported here, so this can only refuse: a ``--proof-home`` that the bootstrap
    did not select (an entry point that skipped it) must not run on whatever home was loaded.
    """
    scanned = _scan(list(sys.argv[1:] if argv is None else argv))
    flagged = requested_proof_home(scanned[1]) if scanned else None
    selected = flagged if flagged is not None else os.environ.get(PROOF_HOME_ENV)
    if selected is None:
        _enforce_agent_shape(isolated)
        return
    home = validate_proof_home(selected)
    try:
        current = _current_home().resolve()
    except (OSError, RuntimeError, ValueError):
        current = None
    if current != home or os.environ.get(PROOF_HOME_ENV) != str(home):
        _refuse("--proof-home was not selected before configuration loaded")


def recovery_home_allowed(home: str | Path) -> bool:
    """Defense in depth for startup recovery: a proof backend recovers its OWN state, never production."""
    if not agent_origin() and PROOF_HOME_ENV not in os.environ:
        return True
    if _production_or_unknown(home):
        return False
    selected = os.environ.get(PROOF_HOME_ENV)
    if selected is None:
        return True
    try:
        return _path(home) == validate_proof_home(selected)
    except SystemExit:
        return False


def drop_provenance(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    for key in PROVENANCE_KEYS:
        env.pop(key, None)
    return env


def stamp_tool_child(env: dict[str, str]) -> dict[str, str]:
    """Stamp a child's final env (after overrides/scrubbing) with origin and launching home.

    Every child spawned through the shared subprocess sanitizer is stamped, not only the terminal,
    process and execute_code tools: the stamp means "spawned by a Hermes agent process", and its
    only consumer refuses restart-proof backends on production state, which no Hermes child (search
    worker, cron script, helper daemon) legitimately starts. Desktop launches drop it.
    """
    env[TOOL_CALL_ENV] = "1"
    env[ORIGIN_ENV] = os.environ.get(ORIGIN_ENV) or "tool"
    try:
        env[AGENT_HOME_ENV] = str(_current_home().resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return env


def remote_origin_prefix(environ: Optional[Mapping[str, str]] = None) -> str:
    """Shell prefix re-exporting the agent origin on a remote host (sshd starts a clean env)."""
    env = os.environ if environ is None else environ
    origin = env.get(ORIGIN_ENV) or "tool"
    return f"export {TOOL_CALL_ENV}=1 {ORIGIN_ENV}={shlex.quote(origin)}; "
