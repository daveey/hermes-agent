"""Mask secrets in a process's argv before it is shown to a person (``hermes doctor`` holder lists).

``agent.redact`` knows secret *formats* and a few flag names. A command line also carries secrets it
has no pattern for: the value after a secret-named flag it does not list (``--webhook-secret``,
``--signing-key=…``), bare random tokens passed positionally, a program's own short password flags
(``mysql -pPASS``, ``sshpass -p``, ``curl -u user:pass``) and ``user:pass@host``. Those are masked here; the
shared redactor still runs afterwards. Identifiers people need to tell processes apart stay: paths,
URLs, hex (git commits, UUIDs), Hermes ids (``t_…``, ``20261119_163442_…`` session ids) and words.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from itertools import pairwise

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
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=")
# An environment-style name (HERMES_RELAY_ID_2024, OPENROUTER_API_KEY_2): a name, never a random token.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
# user:pass@host without a scheme (git, rsync, proxies): the password part.
_USERINFO = re.compile(r"^([^:@/\s]+):([^@\s]+)@(?=[^@\s]+$)")
# A secret-named flag whose value is a command that prints the secret (--password-cmd "pass show x"): only the
# program is shown, since the rest may hold the secret inline.
_COMMAND_SUFFIXES = frozenset({"cmd", "command", "helper"})
# Programs whose own short flags carry a password: (separate-value flags, attached-only flags, user:pass flags).
_PROGRAM_SECRET_FLAGS = {
    "mysql": ((), ("-p",), ()), "mysqldump": ((), ("-p",), ()), "mysqladmin": ((), ("-p",), ()),
    "mariadb": ((), ("-p",), ()), "mariadb-dump": ((), ("-p",), ()),
    "sshpass": (("-p",), ("-p",), ()), "redis-cli": (("-a",), (), ()),
    "openssl": (("-k", "-pass", "-passin", "-passout"), (), ()),
    "curl": ((), (), ("-u", "--user", "-U", "--proxy-user")), "smbclient": ((), (), ("-U",)),
}
_NUMBER = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_PATH_START = re.compile(r"^(?:/|~|\.{1,2}/|\\|[A-Za-z]:[\\/])")
_MIN_BARE_SECRET = 16
_MIN_LETTER_SECRET = 20
_MIN_ENTROPY_BITS = 3.5


def _entropy(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _random_letters(token: str) -> bool:
    """A letters-only token that is not words: case flips far more often than camelCase does, or far fewer
    vowels than any run of words (``qzxkvbnmplkjhgfdrtw``)."""
    if len(token) < _MIN_LETTER_SECRET or not token.isalpha() or not token.isascii():
        return False
    if _entropy(token) < _MIN_ENTROPY_BITS:
        return False  # repeated letters (``pppp…``) are padding or a placeholder, not a random secret
    flips = sum(1 for a, b in pairwise(token) if a.isupper() != b.isupper())
    vowels = sum(1 for c in token.lower() if c in "aeiouy")
    return flips >= len(token) / 3 or vowels / len(token) < 0.2


def _looks_like_bare_secret(token: str) -> bool:
    """A random-looking token: mixed letters and digits with high per-character entropy (or random letters),
    and not a path, URL, hex digest/UUID, Hermes id or ordinary hyphenated words."""
    if _random_letters(token):
        return True
    if len(token) < _MIN_BARE_SECRET or not _TOKEN_CHARS.match(token):
        return False
    if _HEX.match(token) or _HERMES_ID.match(token):
        return False
    if any(len(part) >= 5 and part.isalpha() and (part.islower() or part.istitle())
           for part in re.split(r"[-_.]", token)):
        return False  # a separated plain word (``…-production``, ``Qwen3-Coder-…``): an identifier, not random
    if not (re.search(r"[A-Za-z]", token) and re.search(r"\d", token)):
        return False
    return _entropy(token) >= _MIN_ENTROPY_BITS


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


def _env_name(name: str) -> bool:
    """UPPER_SNAKE made mostly of words (HERMES_RELAY_ID_2024), not random segments posing as one."""
    if not _ENV_NAME.match(name):
        return False
    parts = name.split("_")
    return sum(1 for part in parts if len(part) >= 2 and part.isalpha()) * 2 > len(parts)


def _is_command_flag(flag: str) -> bool:
    parts = [part for part in re.split(r"[-_.]", flag.lstrip("-")) if part]
    return (len(parts) > 1 and parts[-1].lower() in _COMMAND_SUFFIXES
            and any(_SECRET_WORD.search(part) for part in parts[:-1]))


def _mask_command(value: str) -> str:
    program, _, rest = value.strip().partition(" ")
    return f"{program} {MASK}" if rest.strip() else value


def _mask_userinfo(token: str, *, after_colon_only: bool = False) -> str:
    """``user:pass@host`` -> ``user:***@host``; with *after_colon_only* (``curl -u user:pass``,
    ``smbclient -U user%pass``) any ``user:pass`` or ``user%pass``."""
    if match := _USERINFO.match(token):
        return f"{match.group(1)}:{MASK}@{token[match.end():]}"
    if after_colon_only:  # user:pass (curl) or user%pass (smbclient)
        split = re.match(r"^([^:%]*)([:%])(.+)$", token)
        return f"{split.group(1)}{split.group(2)}{MASK}" if split else token  # a bare user: the program prompts
    return token


def _split_assignment(token: str):
    """``(name, value)`` when *token* is ``NAME=value`` (ssh -o ServerAliveInterval=15, --annotation=platform=x),
    else None -- never base64 padding (``Zm9v…MA==``: a random "name" followed by "=" or nothing)."""
    match = _ASSIGNMENT.match(token)
    if match is None:
        return None
    name, value = match.group(1), token[match.end():]
    if not value or value.startswith("=") or (not _env_name(name) and _looks_like_bare_secret(name)):
        return None
    return name, value


def _mask_value(value: str, *, secret_named: bool) -> str:
    if not value or _looks_like_path(value):
        return value
    if not secret_named and (nested := _split_assignment(value)):
        name, inner = nested
        return f"{name}={_mask_value(inner, secret_named=_is_secret_flag('--' + name))}"
    if _NUMBER.match(value) and not secret_named:
        return value  # a count or id; after a secret-named flag a number is the secret (PINs, numeric keys)
    return MASK if secret_named or _looks_like_bare_secret(value) else value


def _program_flags(program: str):
    name = os.path.basename(program or "").lower().removesuffix(".exe")
    return _PROGRAM_SECRET_FLAGS.get(name, ((), (), ()))


def _program_flag(token, separate, attached, userpass):
    """``(masked token, what the next token is)`` for a program's own short secret flag, else None."""
    if token in separate:
        return token, "secret"
    if token in userpass:
        return token, "userpass"
    for flag in (*attached, *userpass):
        if token.startswith(flag) and len(token) > len(flag) and not token.startswith("--"):
            value = token[len(flag):]
            masked = MASK if flag in attached else _mask_userinfo(value, after_colon_only=True)
            return f"{flag}{masked}", None
    return None


def _random_dash_value(token: str) -> bool:
    """A secret that happens to start with "-" (``--password -Xy9kq2LmZ``): letters and digits after the dash."""
    body = token.lstrip("-")
    return (token.startswith("-") and not token.startswith("--") and len(body) >= 8
            and bool(re.search(r"\d", body)) and bool(re.search(r"[A-Za-z]", body)))


def mask_argv_secrets(argv: list[str], *, program: str = "") -> list[str]:
    """*argv* (without the program) with secrets replaced by ``***``; *program* (argv[0]) selects the
    program's own short password flags. Paths and identifiers are kept."""
    separate, attached, userpass = _program_flags(program)
    out: list[str] = []
    pending = None  # what the next token is: "secret", "command" or "userpass"
    for token in argv:
        if pending is not None:
            kind, pending = pending, None
            if kind == "userpass":
                out.append(_mask_userinfo(token, after_colon_only=True))
                continue
            if not token.startswith("-") or _random_dash_value(token):
                out.append(_mask_command(token) if kind == "command" else _mask_value(token, secret_named=True))
                continue
        handled = _program_flag(token, separate, attached, userpass)
        if handled is not None:
            masked, pending = handled
            out.append(masked)
            continue
        if token.startswith("-"):
            flag, eq, value = token.partition("=")
            if _is_command_flag(flag):
                out.append(f"{flag}={_mask_command(value)}" if eq else token)
                pending = None if eq else "command"
                continue
            secret_named = _is_secret_flag(flag)
            if eq:
                out.append(f"{flag}={_mask_value(value, secret_named=secret_named)}")
            else:
                out.append(token)
                pending = "secret" if secret_named else None
            continue
        userinfo = _mask_userinfo(token)
        out.append(userinfo if userinfo != token else _mask_value(token, secret_named=False))
    return out
