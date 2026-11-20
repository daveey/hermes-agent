"""Mask secrets in a process's argv before it is shown to a person (``hermes doctor`` holder lists).

Fail closed: an argument is shown only when it parses as an identifier -- words, numbers, versions, paths, hosts,
model names, Hermes and cloud ids -- and is masked (``***``) otherwise, so a secret shape nobody listed is still
hidden. On top of that, values a name marks as secret are masked whatever they look like: after a secret-named flag
(``--webhook-secret``, ``--api-key-value``), in ``NAME=value`` and ``Header: value`` pairs, a program's own password
flags (``mysql -pPASS``, ``sshpass -p``, ``curl -u user:pass``, also behind ``sudo``/``docker exec``/``bash -c``) and
``user:pass@host``. ``agent.redact`` still runs afterwards for known secret formats and URL credentials.

The limit: a short random string can read like a name. Measured on generated secrets of every common shape (letters
and digits, base32/64, urlsafe, hex, symbols, single-case letters), at most ~2% of 16-character ones and well under 1%
of 32-character ones are shown; under 0.5% of this repo's identifiers are masked. Short hex (up to 12 characters) is
shown on purpose: it is how commits and digests are abbreviated.
"""

from __future__ import annotations

import os
import re

MASK = "***"
INLINE_CODE = "<inline code>"  # describe_holder_pid's stand-in for an interpreter's -c source
_MAX_DEPTH = 4

# ---- names that mark a value as secret ---------------------------------------------------------------------------

# A name is secret when one of its parts is, or ends with, a secret word (--api-key, --apikey, --client-secret-value,
# --token-b64) -- fail closed: a new suffix stays secret -- unless its LAST part names a setting about the secret.
_SECRET_WORD = re.compile(r"(?:key|token|secret|passw(?:or)?d|passwd|pwd|auth|credential|cookie|bearer)s?$",
                          re.IGNORECASE)
_SETTING_SUFFIXES = frozenset({
    "file", "files", "env", "timeout", "ttl", "name", "names", "id", "ids", "path", "paths", "dir", "url", "uri",
    "host", "port", "count", "limit", "size", "length", "len", "mode", "type", "kind", "format", "store", "backend",
    "provider", "browser", "enabled", "required", "only", "whitelist", "allowlist", "denylist", "blacklist",
    "server", "servers", "scheme", "schemes", "delegate", "signingkey",
})
_COMMAND_SUFFIXES = frozenset({"cmd", "command", "helper"})
# A first part that makes a flag a switch: it takes no value, so the next argument is not its secret.
_BOOLEAN_PREFIXES = frozenset({"no", "show", "rotate", "migrate", "with", "list", "print", "clear", "reset", "skip",
                               "allow", "require", "enable", "disable", "isolated"})
_PUBLIC_FLAGS = frozenset({"--ssh-session-token-file", "--keychain", "--monkey", "--keyboard", "--author",
                           "--authority", "--max-tokens", "--min-tokens", "--cache-read-tokens", "--idempotency-key",
                           "--step-key", "--passkey-browser", "--session-key", "--no-credentials"})
# Values of a secret-named flag that select a mechanism rather than carry the secret (mcp add --auth oauth).
_MODE_WORDS = frozenset({"oauth", "oauth2", "header", "basic", "bearer", "none", "token", "api-key", "apikey", "env",
                         "file", "keychain", "prompt", "agent", "stdin", "auto", "true", "false", "on", "off"})
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})

# Programs whose own short flags carry a password: separate-value flags, attached-only flags, user:pass flags.
_PROGRAM_SECRET_FLAGS = {
    "mysql": ((), ("-p",), ()), "mysqldump": ((), ("-p",), ()), "mysqladmin": ((), ("-p",), ()),
    "mariadb": ((), ("-p",), ()), "mariadb-dump": ((), ("-p",), ()),
    "sshpass": (("-p",), ("-p",), ()), "redis-cli": (("-a",), (), ()),
    "openssl": (("-k", "-pass", "-passin", "-passout"), (), ()),
    "mongo": (("-p",), (), ()), "mongosh": (("-p",), (), ()), "mongodump": (("-p",), (), ()),
    "ldapsearch": (("-w",), (), ()), "ldapmodify": (("-w",), (), ()), "ldapadd": (("-w",), (), ()),
    "ldapdelete": (("-w",), (), ()), "sqlcmd": (("-P",), (), ()), "bcp": (("-P",), (), ()),
    "keytool": (("-storepass", "-keypass", "-srcstorepass", "-deststorepass", "-srckeypass", "-destkeypass"), (), ()),
    "7z": ((), ("-p",), ()), "7za": ((), ("-p",), ()), "zip": (("-P",), (), ()), "unzip": (("-P",), (), ()),
    "ssh-keygen": (("-N", "-P"), (), ()),
    "curl": ((), (), ("-u", "--user", "-U", "--proxy-user")), "smbclient": ((), (), ("-U", "--user")),
    "lftp": ((), (), ("-u", "--user")), "wget": ((), (), ()),
}
_NO_FLAGS = ((), (), ())
# Programs that run a command given as their first positional argument: that program's own flags apply after it.
_COMMAND_WRAPPERS = frozenset({"sshpass", "sudo", "doas", "env", "nice", "nohup", "timeout", "xargs", "exec", "time",
                               "stdbuf", "ionice", "chroot", "setsid", "runuser", "caffeinate", "watch", "flock"})
# Names whose hex value identifies a key rather than being one (git -c user.signingkey=ABCDEF0123456789).
_KEY_ID_NAMES = frozenset({"signingkey", "keyid", "key-id", "fingerprint", "kid"})

# ---- identifier grammar --------------------------------------------------------------------------------------------

# Common letter pairs (95% of pairs in /usr/share/dict/words plus the identifiers of this repo and of live processes):
# identifiers are made of words, random letters are not.
_PAIRS = frozenset((
    "ab ac ad ae af ag ai ak al am an ap ar as at au av ay ba be bi bl bo br bu ca cc ce ch ci ck cl co cr ct cu cy "
    "da dd de di dl do dr ds du ea eb ec ed ee ef eg ei el em en eo ep er es et eu ev ew ex fa fe ff fi fl fo fr fu "
    "ga ge gh gi gl gn go gr gu gy ha he hi hl ho hr ht hu hy ia ib ic id ie if ig ik il im in io ip ir is it iu iv "
    "iz ka ke ki la ld le li ll lo ls lt lu ly ma mb me mi mm mo mp mu my na nc nd ne nf ng ni nk nl nn no np nr ns "
    "nt nu nv ny oa ob oc od oe of og oi ok ol om on oo op or os ot ou ov ow ox pa pe ph pi pl po pp pr ps pt pu py "
    "qu ra rb rc rd re rg rh ri rk rl rm rn ro rp rr rs rt ru rv ry sa sc se sh si sk sl sm sn so sp ss st su sy ta "
    "tc te th ti tl to tr ts tt tu ty ua ub uc ud ue ug ui ul um un up ur us ut va ve vi vo wa we wi wo xi ya yc yl "
    "ym yn yp yr ys yt za ze"
).split())
_MIN_JOINED = 12
_VOWELS = frozenset("aeiouy")
# Thresholds measured on random strings vs the repo's identifiers and /usr/share/dict/words.
_COMMON_MIN, _VOWEL_MIN, _RARE_MAX, _CLUSTER_MIN = 0.55, 0.1, 0.25, 6
_COMMON_MIN_LONG, _LONG_WORD = 0.7, 14
_RARE = frozenset("jqxzkvw")
# Punctuation between the words of an identifier, list or shell text. A secret cut into short pieces by symbols is
# still caught: _joined_random judges each chunk with its symbols removed.
_SEPARATORS = re.compile(r"[-_.:/@=,+~%?&#*\\|;()\[\]{}<>\"'`$!^ ]")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Ids with a known prefix: Hermes cards/processes/sessions, cloud resources.
_KNOWN_ID_BODY = (r"(?:t_[0-9a-f]+|proc_[0-9a-f]+|\d{8}_\d{6}_[0-9a-z]+|"
                  r"(?:i|vol|snap|sg|subnet|vpc|ami|eni|igw|rtb|nat|acl|lt)-[0-9a-f]{8,17})")
_KNOWN_ID = re.compile(rf"^{_KNOWN_ID_BODY}$")
_KNOWN_ID_INSIDE = re.compile(rf"(?<![0-9A-Za-z]){_KNOWN_ID_BODY}(?![0-9A-Za-z])")
_PATH_START = re.compile(r"^(?:/|~|\.{1,2}/|\\|[A-Za-z]:[\\/])")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=")
_HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s+(.*)$", re.DOTALL)
# user:pass@host without a scheme; not package or image specs (npm:react@18, python:3.12@sha256:…, mailto:a@b).
_USERINFO = re.compile(r"^([^:@/\s]+):([^:@/\s]+)@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}|localhost|\d+(?:\.\d+){3})"
                       r"(?::\d+)?(?:[:/].*)?$")
_NOT_USERINFO = frozenset({"mailto", "npm", "github", "gitlab", "bitbucket", "pkg", "jsr", "docker", "oci", "git"})


def _letters_random(*words: str) -> bool:
    """Letters that do not read as words: few common letter pairs, few vowels, many rare letters or a long consonant
    cluster. Several *words* (camelCase parts) are judged together, with pairs taken inside each word only."""
    lowered = [w.lower() for w in words if w]
    pairs = [w[i:i + 2] for w in lowered for i in range(len(w) - 1)]
    letters = "".join(lowered)
    if not pairs or not letters:
        return False
    common = sum(pair in _PAIRS for pair in pairs) / len(pairs)
    vowels = sum(c in _VOWELS for c in letters) / len(letters)
    rare = sum(c in _RARE for c in letters) / len(letters)
    cluster = max((len(m) for w in lowered for m in re.findall(r"[^aeiouy]+", w)), default=0)
    longest = max(len(w) for w in lowered)
    # one unbroken word of 14+ letters is rare in identifiers and common in random strings: it must read strongly
    floor = _COMMON_MIN_LONG if longest >= _LONG_WORD and len(lowered) == 1 else _COMMON_MIN
    return common < floor or vowels < _VOWEL_MIN or rare > _RARE_MAX or cluster >= _CLUSTER_MIN


def _alpha_ok(run: str) -> bool:
    """A letter run that is a word, an acronym or camelCase words."""
    if len(run) <= 3:
        return not re.search(r"[a-z][A-Z]", run)  # "mP", "qL": random case, not a word
    if len(set(run.lower())) == 1:
        return True  # a repeated letter (padding, placeholders) is no secret
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", run)
    if "".join(words) != run:
        return False
    if len(words) > 1 and sum(len(w) for w in words) / len(words) < 2.5:
        return False  # "XyQwErTy": a capital every other letter
    if len(run) >= 10 and sum(len(w) <= 2 for w in words) >= 3:
        return False  # names have at most a couple of short words (is, By, Id); random case has many (Ub…Lz…Jw)
    if len(words) > 1 and len(run) >= 8 and _letters_random(*words):
        return False  # camelCase whose parts do not read as words (RfilJpvPrieGXi…)
    for word in words:
        if word.isupper():
            if len(word) >= 8 and _letters_random(word):
                return False
            continue
        if len(word) >= 8 and _letters_random(word):
            return False
    return True


def _segment_ok(segment: str) -> bool:
    """One separator-free piece of an identifier."""
    if not segment or segment.isdigit():
        return True
    if not (segment.isascii() and segment.isalnum()):
        return False  # symbols: passwords, not identifiers
    has_letter, has_digit = re.search(r"[A-Za-z]", segment), re.search(r"\d", segment)
    if len(segment) >= 16 and has_letter and has_digit and re.fullmatch(r"[0-9a-fA-F]+", segment):
        return False  # long hex: a key, a token or a full digest
    runs = re.findall(r"[A-Za-z]+|\d+", segment)
    if len(segment) <= 12 and re.fullmatch(r"[0-9a-fA-F]+", segment):
        return True  # a short commit or digest prefix (0362a20c0e39); 16+ hex is masked above
    if len(segment) >= 10 and len(runs) >= 6:
        return False  # letters and digits alternating: generated, not named
    return all(_alpha_ok(run) for run in runs if run.isalpha())


def _joined_random(token: str) -> bool:
    """A chunk between word separators (``-``, ``_``, ``.``), its other symbols removed, is generated: many
    letter/digit alternations or letters that do not read as words. Symbols would otherwise cut a secret
    (``uAkqc7c#pilIq7s9``, ``+QBYaSITMRV…``) into short, innocent-looking pieces; word separators are left alone
    so snake_case and kebab-case names are judged word by word."""
    for chunk in re.split(r"[-_.]", token):
        joined = re.sub(r"[^A-Za-z0-9]", "", chunk)
        if len(joined) < _MIN_JOINED or (len(joined) <= 12 and re.fullmatch(r"[0-9a-fA-F]+", joined)):
            continue  # short, or a short commit/digest prefix
        if len(re.findall(r"[A-Za-z]+|\d+", joined)) >= 6:
            return True
        letters = re.sub(r"[^A-Za-z]", "", joined)
        if len(letters) >= 8 and len(set(letters.lower())) > 2 and _camel_random(letters):
            return True
    return False


def _camel_random(letters: str) -> bool:
    """Letters that do not read as words, judged per camelCase word when the casing is word-like."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", letters)
    acronyms = [w for w in words if len(w) <= 2 and w.isupper()]
    # one short acronym (JS, IO, UI) carries no letter statistics; several are random case flips (…JPvGXi…)
    long_words = [w for w in words if len(w) >= 3] if len(acronyms) <= 1 else words
    if len(words) > 1 and len(long_words) > 1 and min(len(w) for w in long_words) >= 3:
        return _letters_random(*long_words)
    return _letters_random(letters)


def is_identifier(token: str) -> bool:
    """*token* reads as a name a person would recognise (words, numbers, versions, hosts, paths, ids)."""
    if _UUID.match(token) or _KNOWN_ID.match(token):
        return True
    token = _KNOWN_ID_INSIDE.sub("id", token)  # followup:t_01a1ecf2c3d6367b:notify
    if _joined_random(token):
        return False
    return all(_segment_ok(piece) for piece in _SEPARATORS.split(token))


# ---- values -------------------------------------------------------------------------------------------------------

def _is_secret_name(name: str) -> bool:
    if "--" + name.lower().lstrip("-") in _PUBLIC_FLAGS:
        return False
    parts = [part.lower() for part in re.split(r"[-_.]", name.lstrip("-")) if part]
    if not parts:
        return False
    if "webhook" in parts or "webhooks" in parts:
        return True  # a webhook URL is the secret itself
    if parts[0] in _BOOLEAN_PREFIXES or parts[-1] in _SETTING_SUFFIXES or parts[-1] in _COMMAND_SUFFIXES:
        return False
    return any(_SECRET_WORD.search(part) for part in parts)


def _is_command_name(name: str) -> bool:
    parts = [part.lower() for part in re.split(r"[-_.]", name.lstrip("-")) if part]
    return len(parts) > 1 and parts[-1] in _COMMAND_SUFFIXES and any(_SECRET_WORD.search(p) for p in parts[:-1])


def _mask_pieces(value: str) -> str:
    """A path or URL keeps its shape; each piece that is not an identifier is masked."""
    out, last = [], 0
    for match in _SEPARATORS.finditer(value):
        piece = value[last:match.start()]
        out.append(piece if _segment_ok(piece) or _UUID.match(piece) else MASK)
        out.append(match.group())
        last = match.end()
    tail = value[last:]
    out.append(tail if _segment_ok(tail) or _UUID.match(tail) else MASK)
    return "".join(out)


def _existing_path(value: str) -> bool:
    """A path that exists here (or whose directory does) names a file, not a secret: cache, container and mktemp
    names look random but are shown whole. A secret starting with "/" (base64) names nothing."""
    path = os.path.expanduser(value.split("=", 1)[0])
    try:
        parent = os.path.dirname(path.rstrip("/\\"))
        # an existing parent counts only two levels down: "/" or "~" exist for any secret that starts with "/"
        return os.path.lexists(path) or (parent.count(os.sep) >= 2 and os.path.isdir(parent))
    except (OSError, ValueError):
        return False


def _mask_url(value: str) -> str:
    scheme, rest = value.split("://", 1)
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        userinfo, host = authority.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        authority = f"{user if is_identifier(user) else MASK}:{MASK}@{host}" if ":" in userinfo else f"{MASK}@{host}"
    return f"{scheme}://{authority}{slash}{_mask_pieces(path)}" if slash else f"{scheme}://{authority}"


def _mask_userpass(value: str) -> str:
    """``user:pass`` / ``user%pass`` / ``user,pass`` -> the user (if it reads as one) and ``***``."""
    split = re.match(r"^([^:%,]*)([:%,])(.*)$", value, re.DOTALL)
    if split is None:
        return value if is_identifier(value) else MASK
    user, sep, _secret = split.groups()
    return f"{user if user and is_identifier(user) else MASK}{sep}{MASK}"


def _mask_command(value: str) -> str:
    program, _, rest = value.strip().partition(" ")
    return f"{program} {MASK}" if rest.strip() else value


def _judge(value: str, *, secret: bool, depth: int) -> str:
    """*value* as shown: kept when it reads as an identifier (and no name marked it secret), masked otherwise."""
    if not value:
        return value
    if depth > _MAX_DEPTH:
        return MASK
    if secret:
        if value.lower() in _MODE_WORDS:
            return value
        if _PATH_START.match(value) and re.fullmatch(r"[\w./\\:~-]+", value) and _mask_pieces(value) == value:
            return value  # a file holding the secret (--auth ~/.hermes/egress.auth), not the secret
        return MASK
    if header := _HEADER.match(value):
        name, rest = header.groups()
        secret_header = name.lower() in _SECRET_HEADERS or _is_secret_name(name)
        return f"{name}: {MASK if secret_header else _judge(rest, secret=False, depth=depth + 1)}"
    if any(c.isspace() for c in value):
        words = value.split()
        masked = mask_argv_secrets(words[1:], program=words[0], _depth=depth + 1)
        first = words[0] if is_identifier(words[0]) or _PATH_START.match(words[0]) else MASK
        return " ".join([first, *masked])
    if _URL.match(value):
        return _mask_url(value)
    if (userinfo := _USERINFO.match(value)) and userinfo.group(1).lower() not in _NOT_USERINFO:
        return f"{userinfo.group(1)}:{MASK}@{value[userinfo.end(2) + 1:]}"
    if _PATH_START.match(value):
        return value if _existing_path(value) else _mask_pieces(value)
    if (sep := next((c for c in ",|" if c in value), None)) and not any(c.isspace() for c in value):
        # a list (--enable-features=A,B,C, A|B): each item on its own, so one odd item does not hide the rest
        return sep.join(_judge(item, secret=False, depth=depth + 1) for item in value.split(sep))
    if (assignment := _ASSIGNMENT.match(value)) and value[assignment.end():] and not value[assignment.end()] == "=":
        name = assignment.group(1)
        # the name must read as words even run together: AB12_CD34_EF56_GH78=… is random segments, not a name
        if not (is_identifier(name) and _segment_ok(_SEPARATORS.sub("", name))):
            return MASK
        inner = value[assignment.end():]
        if _is_command_name(name):
            return f"{name}={_mask_command(inner)}"
        if name.lower().rsplit(".", 1)[-1] in _KEY_ID_NAMES and re.fullmatch(r"[0-9A-Fa-f]{8,40}", inner):
            return value
        return f"{name}={_judge(inner, secret=_is_secret_name(name), depth=depth + 1)}"
    return value if is_identifier(value) else MASK


# ---- argv -----------------------------------------------------------------------------------------------------------

def _program_flags(program: str):
    name = os.path.basename(program or "").lower().removesuffix(".exe")
    return _PROGRAM_SECRET_FLAGS.get(name)


def _flag_shaped(token: str, *, judged: bool = False) -> bool:
    """``--name`` or a short ``-x``/``-xyz`` cluster. *judged* (the argument after a secret-named flag): the name must
    also read as one, since a secret may start with ``-`` (``--password -Xy9kq2LmZ``)."""
    name = token.lstrip("-")
    if token.startswith("--"):
        return bool(re.fullmatch(r"[A-Za-z][\w-]*", name)) and (not judged or is_identifier(name))
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,3}", name))


def _program_flag(token: str, flags):
    """``(shown token, what the next argument is)`` for a program's own secret flag, else None."""
    separate, attached, userpass = flags
    flag, eq, value = token.partition("=")
    if token in separate:
        return token, "secret"
    if token in userpass:
        return token, "userpass"
    if eq and flag in userpass:
        return f"{flag}={_mask_userpass(value)}", None
    for short in (*attached, *userpass):
        if len(short) == 2 and token.startswith(short) and len(token) > 2 and not token.startswith("--"):
            rest = token[2:]
            return f"{short}{MASK if short in attached else _mask_userpass(rest)}", None
    # combined short switches ending in a user flag: curl -su alice:pw, -fsSLu alice:pw
    letters = {f[1] for f in userpass if len(f) == 2}
    if re.fullmatch(r"-[A-Za-z]{2,}", token) and token[-1] in letters:
        return token, "userpass"
    return None


def mask_argv_secrets(argv: list[str], *, program: str = "", _depth: int = 0) -> list[str]:
    """*argv* (without the program) as it may be shown; *program* (argv[0]) selects its own password flags."""
    flags = _program_flags(program) or _NO_FLAGS
    wrapper = os.path.basename(program or "").lower() in _COMMAND_WRAPPERS
    out: list[str] = []
    pending = None  # what the next argument is: "secret", "command" or "userpass"
    for token in argv:
        if token == INLINE_CODE:
            out.append(token)
            pending = None
            continue
        if pending is not None:
            kind, pending = pending, None
            if not token.startswith("-") or not _flag_shaped(token, judged=True):
                if kind == "userpass":
                    out.append(_mask_userpass(token))
                elif kind == "command":
                    out.append(_mask_command(token))
                else:
                    out.append(_judge(token, secret=True, depth=_depth))
                continue
        handled = _program_flag(token, flags)
        if handled is not None:
            shown, pending = handled
            out.append(shown)
            continue
        if token.startswith("-") and _flag_shaped(token.partition("=")[0]):
            flag, eq, value = token.partition("=")
            if _is_command_name(flag):
                out.append(f"{flag}={_mask_command(value)}" if eq else flag)
                pending = None if eq else "command"
            elif eq:
                out.append(f"{flag}={_judge(value, secret=_is_secret_name(flag), depth=_depth)}")
            else:
                out.append(token)
                pending = "secret" if _is_secret_name(flag) else None
            continue
        if (wrapped := _program_flags(token)) is not None:
            flags, wrapper = wrapped, False  # sudo mysql …, docker exec db mysql …, env X=1 mysql …
            out.append(token)
            continue
        if wrapper and "=" not in token and not token.isdigit():
            # sshpass -p x ssh -p 2222 …: from the wrapped program on, its own flags (here none) apply
            flags, wrapper = _NO_FLAGS, os.path.basename(token).lower() in _COMMAND_WRAPPERS
            out.append(_judge(token, secret=False, depth=_depth))
            continue
        out.append(_judge(token, secret=False, depth=_depth))
    return out
