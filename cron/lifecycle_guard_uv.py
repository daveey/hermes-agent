"""How ``uv [global options] run [options] <command>`` reads its argv, for the lifecycle guard.

A value option read as the command hides the script that actually runs (``uv run -w requests
f.py``), so the value table follows ``uv help run`` (uv 0.13.7) and an option in neither table
is read both ways (fail closed).
"""

from __future__ import annotations

# Every option `uv help run` (uv 0.13.7) shows with a `<VALUE>`, plus the hidden
# `--python-preference`.
_UV_VALUE_OPTIONS = frozenset({
    "--directory", "--project", "--config-file", "--cache-dir", "--color", "-p", "--python",
    "-w", "--with", "--with-editable", "--with-requirements", "--env-file", "--package",
    "--extra", "--no-extra", "--group", "--no-group", "--only-group", "--no-editable-package",
    "--index", "--default-index", "-i", "--index-url", "--extra-index-url", "-f",
    "--find-links", "--index-strategy", "--keyring-provider", "-P", "--upgrade-package",
    "--upgrade-group", "--resolution", "--prerelease", "--prerelease-package",
    "--fork-strategy", "--exclude-newer", "--exclude-newer-package", "--no-sources-package",
    "--reinstall-package", "--link-mode", "-C", "--config-setting", "--config-settings-package",
    "--no-build-isolation-package", "--no-build-package", "--no-binary-package",
    "--refresh-package", "--python-platform", "--allow-insecure-host", "--python-preference",
})
# `uv help run` flags that take no value.
_UV_FLAG_OPTIONS = frozenset({
    "--all-extras", "--no-dev", "--only-dev", "--no-default-groups", "--all-groups", "-m",
    "--module", "--no-editable", "--exact", "--no-env-file", "--isolated", "--active",
    "--no-sync", "--locked", "--frozen", "-s", "--script", "--gui-script", "--all-packages",
    "--no-project", "--no-index", "-U", "--upgrade", "--no-sources", "--reinstall",
    "--compile-bytecode", "--no-build-isolation", "--no-build", "--no-binary",
    "--require-build-hashes", "--no-require-build-hashes", "-n", "--no-cache", "--refresh",
    "--managed-python", "--no-managed-python", "--no-python-downloads", "-q", "--quiet", "-v",
    "--verbose", "--system-certs", "--offline", "--no-progress", "--no-config", "-h", "--help",
    "--native-tls", "--preview", "--no-preview",
})
# Bound on the argv readings one `uv` explores (each unknown option doubles them).
_MAX_UV_RUN_READINGS = 16


def _uv_option_arity(token: str) -> str:
    """``"value"`` when a ``uv`` option *token* consumes the next word, ``"flag"`` when it does
    not, ``"unknown"`` when neither table says (it may be either)."""
    if token.startswith("--"):
        if "=" in token:
            return "flag"
        if token in _UV_VALUE_OPTIONS:
            return "value"
        return "flag" if token in _UV_FLAG_OPTIONS else "unknown"
    # A short cluster reads like getopt: `-qw` ends in `-w` (takes the next word); in `-wfoo`
    # the value is glued.
    for position, char in enumerate(token[1:], start=1):
        option = "-" + char
        if option in _UV_VALUE_OPTIONS:
            return "value" if position == len(token) - 1 else "flag"
        if option not in _UV_FLAG_OPTIONS:
            return "unknown"
    return "flag"


def _uv_run_command_indices(segment: list[str], index: int) -> list[int]:
    """Every index ``uv [opts] run [opts] <command>`` at *index* may run (each may be
    ``len(segment)``), the reading that takes unknown options as flags first; empty when it is
    not such a ``uv`` (``uv pip ...``, ``uv tool run``). An option neither table knows is read
    both ways, so a value the guard does not know about never hides the command (fail closed).
    """
    results: list[int] = []
    pending = [(index + 1, False)]
    readings = 0
    while pending and readings < _MAX_UV_RUN_READINGS:
        readings += 1
        position, seen_run = pending.pop()
        while position < len(segment):
            token = segment[position]
            if token == "--" and seen_run:
                position += 1
                break
            if token.startswith("-") and token != "-":
                arity = _uv_option_arity(token)
                if arity == "unknown":
                    pending.append((position + 2, seen_run))
                position += 2 if arity == "value" else 1
            elif seen_run:
                break
            elif token == "run":
                seen_run = True
                position += 1
            else:
                break
        if seen_run and position not in results:
            results.append(position)
    return results
