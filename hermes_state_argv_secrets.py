"""Mask secrets in a process's argv before it is shown to a person (``hermes doctor`` holder lists).

``agent.redact`` knows secret *formats* and a few flag names. A command line also carries secrets it
has no pattern for: the value after a secret-named flag it does not list (``--webhook-secret``,
``--signing-key=…``) and bare random tokens passed positionally. Those are masked whole here; the
shared redactor still runs afterwards. Identifiers people need to tell processes apart stay: paths,
URLs, hex (git commits, UUIDs), Hermes ids (``t_…``, ``20261119_163442_…`` session ids) and words.
"""

from __future__ import annotations

import math
import re
from collections import Counter

MASK = "***"
# A flag is secret-named when a part of its name is, or ends with, a secret word (--api-key, --apikey,
# --client-secret-value, --token-b64, --auth-header) -- fail closed: a new suffix stays secret -- unless its LAST
# part names a setting about the secret rather than the secret (--api-key-timeout, --token-env, --key-file).
_SECRET_WORD = re.compile(r"(?:key|token|secret|passw(?:or)?d|passwd|pwd|auth|credential|cookie|bearer)s?$",
                          re.IGNORECASE)
_SETTING_SUFFIXES = frozenset({
    "file", "files", "env", "timeout", "ttl", "name", "names", "id", "ids", "path", "paths", "dir", "url", "uri",
    "host", "port", "count", "limit", "size", "length", "len", "mode", "type", "kind", "format", "store", "backend",
    "provider", "browser", "helper", "cmd", "command", "enabled", "required", "only",
})
# Flags that LOOK secret-named but carry public, identifying values.
_PUBLIC_FLAGS = frozenset({"--ssh-session-token-file", "--token-file", "--key-file", "--auth-file",
                           "--keychain", "--monkey", "--keyboard", "--author", "--authority",
                           "--max-tokens", "--min-tokens", "--cache-read-tokens", "--token-env",
                           "--idempotency-key", "--step-key", "--passkey-browser",
                           # booleans: they take no value, so the next positional (a profile name) is not theirs
                           "--isolated-auth", "--no-credentials", "--no-ask-password"})
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_+=.~-]+$")
_HEX = re.compile(r"^[0-9a-fA-F-]+$")
_HERMES_ID = re.compile(r"^(?:t_[0-9a-f]+|\d{8}_\d{6}_[0-9a-z]+|[a-z]+(?:[-_][a-z]+)+)$")
_ASSIGNMENT = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*)=")
# An environment-style name (HERMES_RELAY_ID_2024, OPENROUTER_API_KEY_2): a name, never a random token.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_PATH_START = re.compile(r"^(?:/|~|\.{1,2}/|\\|[A-Za-z]:[\\/])")
_MIN_BARE_SECRET = 20
_MIN_ENTROPY_BITS = 3.5


def _looks_like_bare_secret(token: str) -> bool:
    """A long random-looking token: mixed letters and digits with high per-character entropy, and not
    a path, URL, hex digest/UUID, Hermes id or ordinary hyphenated words."""
    if len(token) < _MIN_BARE_SECRET or not _TOKEN_CHARS.match(token):
        return False
    if _HEX.match(token) or _HERMES_ID.match(token):
        return False
    if any(len(part) >= 5 and part.isalpha() and (part.islower() or part.istitle())
           for part in re.split(r"[-_.]", token)):
        return False  # a separated plain word (``…-production``, ``Qwen3-Coder-…``): an identifier, not random
    if not (re.search(r"[A-Za-z]", token) and re.search(r"\d", token)):
        return False
    counts = Counter(token)
    entropy = -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())
    return entropy >= _MIN_ENTROPY_BITS


def _is_secret_flag(token: str) -> bool:
    if not token.startswith("-") or token.lower() in _PUBLIC_FLAGS:
        return False
    parts = [part for part in re.split(r"[-_.]", token.lstrip("-")) if part]
    if not parts or parts[-1].lower() in _SETTING_SUFFIXES:
        return False
    return any(_SECRET_WORD.search(part) for part in parts)


def _looks_like_path(value: str) -> bool:
    """A file path names where a secret lives, not the secret: keep it (``--password-file /x/y.pass``). Judged by
    how it starts: base64 secrets contain ``/`` too (``wJalrXUtnFEMI/K7MDENG/…``)."""
    return bool(_PATH_START.match(value))


def _mask_value(value: str, *, secret_named: bool) -> str:
    if not value or _looks_like_path(value):
        return value
    if _NUMBER.match(value) and not secret_named:
        return value  # a count or id; after a secret-named flag a number is the secret (PINs, numeric keys)
    return MASK if secret_named or _looks_like_bare_secret(value) else value


def mask_argv_secrets(argv: list[str]) -> list[str]:
    """*argv* with secret-flag values and bare random tokens replaced by ``***`` (paths are kept)."""
    out: list[str] = []
    mask_next = False
    for token in argv:
        if mask_next and not token.startswith("-"):
            out.append(_mask_value(token, secret_named=True))
            mask_next = False
            continue
        mask_next = False
        if token.startswith("-"):
            flag, eq, value = token.partition("=")
            secret_named = _is_secret_flag(flag)
            if eq:
                out.append(f"{flag}={_mask_value(value, secret_named=secret_named)}")
            else:
                out.append(token)
                mask_next = secret_named
            continue
        assignment = _ASSIGNMENT.match(token)
        name, value = (assignment.group(1), token[assignment.end():]) if assignment else ("", "")
        # ssh -o ServerAliveInterval=15, env-style NAME=value -- not base64 padding (``Zm9v…MA==``: a random "name"
        # followed by "=" or nothing), which is judged as one token below.
        if (assignment and value and not value.startswith("=")
                and (_ENV_NAME.match(name) or not _looks_like_bare_secret(name))):
            out.append(f"{name}={_mask_value(value, secret_named=_is_secret_flag('--' + name))}")
            continue
        out.append(_mask_value(token, secret_named=False))
    return out
