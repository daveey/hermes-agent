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
# A flag whose NAME says its value is a secret: --api-key, --webhook-secret, --auth, --db-password, -token ...
_SECRET_FLAG = re.compile(
    r"^-{1,2}[\w.-]*(?:key|token|secret|passw(?:or)?d|passwd|pwd|auth|credential|cookie|bearer)[\w.-]*$",
    re.IGNORECASE,
)
# Flags that LOOK secret-named but carry public, identifying values.
_PUBLIC_FLAGS = frozenset({"--ssh-session-token-file", "--token-file", "--key-file", "--auth-file",
                           "--keychain", "--monkey", "--keyboard", "--author", "--authority",
                           "--max-tokens", "--min-tokens", "--cache-read-tokens", "--token-env",
                           "--idempotency-key", "--step-key", "--passkey-browser"})
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_+=.~-]+$")
_HEX = re.compile(r"^[0-9a-fA-F-]+$")
_HERMES_ID = re.compile(r"^(?:t_[0-9a-f]+|\d{8}_\d{6}_[0-9a-z]+|[a-z]+(?:[-_][a-z]+)+)$")
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
    return bool(_SECRET_FLAG.match(token)) and token.lower() not in _PUBLIC_FLAGS


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
        out.append(_mask_value(token, secret_named=False))
    return out
