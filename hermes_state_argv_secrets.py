"""Mask secrets in a process's argv before it is shown to a person (``hermes doctor`` holder lists).

Fail closed: an argument is shown only when it parses as an identifier -- words, numbers, versions, hosts, model
names, Hermes and cloud ids, and paths and URLs whose parts are such names -- and masked (``***``) otherwise, so a
secret shape nobody listed is still hidden. Values a name marks as secret are masked whatever they look like: after
a secret-named flag (``--webhook-secret``, ``--api-key-value``, ``-Dapi.key=``, Go-style ``-password``), in
``NAME=value`` and ``Header: value`` pairs, ``user:pass@host``, a program's own password flags (``mysql -pPASS``,
``sshpass -p``, ``curl -u user:pass``, ``-b cookie``), also behind ``sudo``/``docker exec``/``env``/``bash -c`` and
env prefixes in shell strings. ``agent.redact`` still runs afterwards for known secret formats and URL credentials.

Limits, measured on generated secrets of every common shape (letters and digits, base32/64, urlsafe, hex, symbol
passwords, single-case letters, grouped keys) as a bare argument, in a path, in a URL and as a ``NAME=`` value: at most
about 6% of 16-character secrets are shown, 2.5% of 24-character, 1% of 32-character and 0.5% of 40-character ones
(the worst shape is random single-case letters, which can read like words); about 0.25% of this repo's identifiers
are masked. This is a rate, not a guarantee, and some secrets look exactly like identifiers and are shown:
pronounceable passwords (``correcthorse``), all-digit values, UUIDs and strings under 12 characters, unless a name
marks them secret. Short hex (up to 12 characters) is shown on purpose: it is how commits are abbreviated; longer hex
(full commits, digests, container ids) is masked with the keys it looks like.
"""

from __future__ import annotations

import os
import re
from itertools import pairwise

MASK = "***"
INLINE_CODE = "<inline code>"  # describe_holder_pid's stand-in for an interpreter's -c source
_MAX_DEPTH = 4

# ---- names that mark a value as secret ---------------------------------------------------------------------------

# A name part that is, or ends with, a secret word (api-key, apikey, client-secret, passphrase); some words only as a
# whole part, since they end other words (bypass, typewriter).
_SECRET_WORD = re.compile(
    r"(?:key|token|secret|passw(?:or)?d|passwd|passphrase|pwd|auth|authorization|credential|cookie|bearer|jwt|hmac)s?$",
    re.IGNORECASE)
_SECRET_PARTS = frozenset({"pass", "pw", "otp", "creds"})
# A part after the secret word that names a setting about it, not the secret (--api-key-timeout, --key-file).
_SETTING_SUFFIXES = frozenset({
    "file", "files", "env", "timeout", "ttl", "name", "names", "id", "ids", "path", "paths", "dir", "url", "uri",
    "endpoint", "host", "port", "count", "limit", "size", "length", "len", "mode", "type", "kind", "format", "store",
    "backend", "provider", "browser", "enabled", "required", "only", "whitelist", "allowlist", "denylist",
    "blacklist", "server", "servers", "scheme", "schemes", "delegate", "signingkey", "version", "versions",
})
_COMMAND_SUFFIXES = frozenset({"cmd", "command", "helper"})
# A first part that makes a flag a switch: it takes no value, so the next argument is not its secret.
_BOOLEAN_PREFIXES = frozenset({"no", "show", "rotate", "migrate", "with", "list", "print", "clear", "reset", "skip",
                               "allow", "require", "enable", "disable", "isolated"})
_PUBLIC_FLAGS = frozenset({"--ssh-session-token-file", "--keychain", "--monkey", "--keyboard", "--author",
                           "--authority", "--idempotency-key", "--step-key", "--passkey-browser", "--session-key",
                           "--no-credentials", "--use-partner-key-as-auth", "--keywords"})
# Values that select an auth mechanism (mcp add --auth oauth): kept after a flag whose only secret word is "auth".
_AUTH_MODES = frozenset({"oauth", "oauth2", "header", "basic", "bearer", "none", "token", "api-key", "apikey", "env",
                         "keychain", "agent", "sso", "iam", "anonymous", "password", "key"})
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
    "curl": (("-b",), (), ("-u", "--user", "-U", "--proxy-user")), "smbclient": ((), (), ("-U", "--user")),
    "lftp": ((), (), ("-u", "--user")),
}
_NO_FLAGS = ((), (), ())
# Programs that run a command given as their first positional argument: that program's own flags apply after it.
_COMMAND_WRAPPERS = frozenset({"sshpass", "sudo", "doas", "env", "nice", "nohup", "timeout", "xargs", "exec", "time",
                               "stdbuf", "ionice", "chroot", "setsid", "runuser", "caffeinate", "watch", "flock"})
# Names whose hex value identifies a key rather than being one (git -c user.signingkey=ABCDEF0123456789).
_KEY_ID_NAMES = frozenset({"signingkey", "keyid", "key-id", "fingerprint", "kid"})

# ---- identifier grammar --------------------------------------------------------------------------------------------

# Common letter pairs: 95% of pairs in /usr/share/dict/words plus the identifiers of this repo and of live processes.
_PAIRS = frozenset((
    "aa ab ac ad ae af ag ah ai ak al am an ap ar as at au av aw ax ay az ba bb be bi bl bo br bs bu ca cc ce ch ci "
    "ck cl co cr cs ct cu cy da dd de di dl dn do dr ds du dy ea eb ec ed ee ef eg eh ei ek el em en eo ep er es et "
    "eu ev ew ex ey fa fe ff fi fl fo fr ft fu ga ge gg gh gi gl gn go gr gs gu gy ha he hi hl hn ho hr ht hu hy ia "
    "ib ic id ie if ig ik il im in io ip ir is it iu iv iz ja je ju ka ke ki kl ko ks la lc ld le li ll lm lo lp ls "
    "lt lu lv ly ma mb me mi mm mo mp mu my na nb nc nd ne nf ng nh ni nk nl nm nn no np nr ns nt nu nv ny oa ob oc "
    "od oe of og oi ok ol om on oo op or os ot ou ov ow ox pa pe ph pi pl po pp pr ps pt pu py qu ra rb rc rd re rf "
    "rg rh ri rk rl rm rn ro rp rr rs rt ru rv ry sa sc se sh si sk sl sm sn so sp sq ss st su sw sy ta tc te th ti "
    "tl tm to tr ts tt tu tw ty ua ub uc ud ue ug ui ul um un up ur us ut va ve vi vo wa we wh wi wo xi xp xt ya yc "
    "yd ye yl ym yn yo yp yr ys yt za ze zi zo"
).split())
# Thresholds measured on random strings vs the repo's identifiers, /usr/share/dict/words and live process arguments.
_COMMON_MIN, _VOWEL_MIN, _RARE_MAX, _CLUSTER_MIN, _CLUSTER_MIN_UPPER = 0.55, 0.1, 0.2, 7, 5
_COMMON_MIN_LONG, _LONG_WORD = 0.7, 14
_MIN_JOINED = 12
_SHORT_HEX = 12  # an abbreviated commit; longer hex is masked with the keys it looks like
_FLIP_DENSITY_MAX = 0.4
_VOWELS = frozenset("aeiouy")
_RARE = frozenset("jqxz")
# Punctuation between the words of an identifier, list or shell text.
_SEPARATORS = re.compile(r"[-_.:/@=,+~%?&#*\\|;()\[\]{}<>\"'`$!^ ]")
# Separators of words inside a name; the others (+ / = ~ % ? & # * ! $ ^) also cut secrets (base64, symbols) and are
# removed, not split on, when a chunk is judged as a whole.
_WORD_SEPARATORS = re.compile(r"[-_.:@,|;()\[\]{}<>\"'` ]")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Ids with a known prefix: Hermes cards/processes/sessions, cloud resources.
_KNOWN_ID_BODY = (r"(?:t_[0-9a-f]+|proc_[0-9a-f]+|\d{8}_\d{6}_[0-9a-z]+|"
                  r"(?:i|vol|snap|sg|subnet|vpc|ami|eni|igw|rtb|nat|acl|lt)-[0-9a-f]{8,17})")
_KNOWN_ID = re.compile(rf"^{_KNOWN_ID_BODY}$")
_KNOWN_ID_INSIDE = re.compile(rf"(?<![0-9A-Za-z]){_KNOWN_ID_BODY}(?![0-9A-Za-z])")
_PATH_START = re.compile(r"^(?:/|~|\.{1,2}/|\\|[A-Za-z]:[\\/])")
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
_HEADER = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s+(.*)$", re.DOTALL)
# user:pass@host without a scheme; not package or image specs (npm:react@18, python:3.12@sha256:…, mailto:a@b).
_USERINFO = re.compile(r"^([^:@/\s]+):([^:@/\s]+)@([A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*|\d+(?:\.\d+){3})"
                       r"(?::\d+)?(?:[:/].*)?$")
_NOT_USERINFO = frozenset({"mailto", "npm", "github", "gitlab", "bitbucket", "pkg", "jsr", "docker", "oci", "git"})
_STRUCTURE = re.compile(r"[/?&#=;@]")  # between the components of a path or URL
_NPM_SPEC = re.compile(r"^@[\w.-]+/[\w.-]+(?:@[\w.^~<>=-]+)?$")  # @playwright/mcp@latest


def _letters_random(*words: str) -> bool:
    """Letters that do not read as words: few common letter pairs, few vowels, many rare letters or a long consonant
    cluster. Several *words* (camelCase parts) are judged together, with pairs taken inside each word only."""
    words = tuple(w for w in words if w)
    lowered = [w.lower() for w in words]
    pairs = [w[i:i + 2] for w in lowered for i in range(len(w) - 1)]
    letters = "".join(lowered)
    if not pairs:
        return False
    common = sum(pair in _PAIRS for pair in pairs) / len(pairs)
    vowels = sum(c in _VOWELS for c in letters) / len(letters)
    rare = sum(c in _RARE for c in letters) / len(letters)
    # compound words have long clusters (launchctl, fullscreen); a long all-caps word with one is random (GPOGJHRLO)
    longest_cluster = max((len(m) for w in lowered for m in re.findall(r"[^aeiouy]+", w)), default=0)
    upper_cluster = max((len(m) for w in words if w.isupper() and len(w) >= 8
                         for m in re.findall(r"[^aeiouy]+", w.lower())), default=0)
    clustered = longest_cluster >= _CLUSTER_MIN or (upper_cluster >= _CLUSTER_MIN_UPPER and common < 0.75)
    # one unbroken word of 14+ letters is rare in identifiers and common in random strings: it must read strongly
    floor = _COMMON_MIN_LONG if len(lowered) == 1 and len(letters) >= _LONG_WORD else _COMMON_MIN
    return common < floor or vowels < _VOWEL_MIN or rare > _RARE_MAX or clustered


def _letters_look_random(letters: str) -> bool:
    """Letters (possibly camelCase) that are not words: random case flips, or letters that do not read as words."""
    if len(set(letters.lower())) <= 2:
        return False  # a repeated letter (padding, placeholders) is no secret
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", letters)
    if len(words) > 1 and sum(len(w) for w in words) / len(words) < 2.5:
        return True  # "XyQwErTy": a capital every other letter
    if len(letters) >= 10 and sum(len(w) <= 2 for w in words) >= 3:
        return True  # names have at most a couple of short words (is, By, Id); random case has many (Ub…Lz…Jw)
    acronyms = [w for w in words if len(w) <= 2 and w.isupper()]
    # one short acronym (JS, IO) carries no letter statistics; several are random case flips (…JPvGXi…)
    judged = [w for w in words if len(w) >= 3] if len(acronyms) <= 1 else words
    return _letters_random(*(judged if len(judged) > 1 else [letters]))


def _alpha_ok(run: str) -> bool:
    """A letter run that is a word, an acronym or camelCase words. Runs of up to 6 letters carry too little to be a
    secret on their own (iOS, gRPC, eBPF); longer runs are judged."""
    return len(run) <= 6 or not _letters_look_random(run)


def _segment_ok(segment: str) -> bool:
    """One separator-free piece of an identifier."""
    if not segment or segment.isdigit():
        return True
    if not segment.isascii():
        return segment.isalnum()  # a word in another script (naïve, café, Cyrillic names)
    if not segment.isalnum():
        return False  # symbols: passwords, not identifiers
    if re.fullmatch(r"[0-9a-fA-F]+", segment) and re.search(r"\d", segment) and re.search(r"[A-Fa-f]", segment):
        return len(segment) <= _SHORT_HEX
    runs = re.findall(r"[A-Za-z]+|\d+", segment)
    if len(segment) >= 10 and len(runs) >= 6:
        return False  # letters and digits alternating: generated, not named
    return all(_alpha_ok(run) for run in runs if run.isalpha())


def _flip_density(alnum: str) -> float:
    """Changes between lowercase, uppercase and digits per character (a capital starting a word is not one):
    camelCase and versions change rarely (Qwen3-Coder-480B ~0.24), generated tokens often (~0.6)."""
    def kind(c: str) -> str:
        return "d" if c.isdigit() else "u" if c.isupper() else "l"
    flips = sum(1 for a, b in pairwise(alnum) if kind(a) != kind(b) and (kind(a), kind(b)) != ("u", "l"))
    return flips / max(1, len(alnum) - 1)


def _joined_random(token: str) -> bool:
    """The token is generated, judged with its symbols removed so that symbols cannot cut a secret into short,
    innocent-looking pieces (``uAkqc7c#pilIq7s9``, ``+QBYaSITMRV…``, ``ab12_cd34_ef56_gh78``): many letter/digit
    alternations across the whole token, or a word chunk whose letters do not read as words."""
    whole = re.sub(r"[^A-Za-z0-9]", "", token)
    short_hex = len(whole) <= _SHORT_HEX and re.fullmatch(r"[0-9a-fA-F]+", whole)
    if len(whole) >= _MIN_JOINED and not short_hex and (
            len(re.findall(r"[A-Za-z]+|\d+", whole)) >= 8 or _flip_density(whole) >= _FLIP_DENSITY_MAX):
        return True
    chunks = [re.sub(r"[^A-Za-z0-9]", "", chunk) for chunk in _WORD_SEPARATORS.split(token)]
    groups = [chunk for chunk in chunks if chunk]
    if (len({len(g) for g in groups}) == 1 and len(groups) >= (3 if len(groups[0]) >= 5 else 4)
            and 3 <= len(groups[0]) <= 8
            and not all(g.isdigit() for g in groups)
            and (any(re.search(r"\d", g) and re.search(r"[A-Za-z]", g) for g in groups)
                 or all(g.isupper() for g in groups))):
        return True  # equal-length groups with digits or all caps: a license or recovery key (OKWV-IDIA-T5UR-…)
    # short groups (N0BZ-AXT6-BODG-…, ZPTK29.PAJDOL.…): the letters of all chunks, pairs taken inside each chunk
    group_letters = [re.sub(r"[^A-Za-z]", "", chunk) for chunk in chunks if chunk]
    if len(group_letters) >= 3 and sum(map(len, group_letters)) >= _MIN_JOINED and not any(
            len(g) >= 3 and g.islower() and not _letters_random(g) for g in group_letters if len(g) >= 5):
        words = [g for g in group_letters if len(g) >= 2]
        if len(words) >= 3 and all(w.isupper() or w.islower() for w in words) and _letters_random(*words):
            return True
    for joined in chunks:
        if len(joined) < _MIN_JOINED or (len(joined) <= _SHORT_HEX and re.fullmatch(r"[0-9a-fA-F]+", joined)):
            continue
        if len(re.findall(r"[A-Za-z]+|\d+", joined)) >= 6:
            return True
        letters = re.sub(r"[^A-Za-z]", "", joined)
        if len(letters) >= 8 and _letters_look_random(letters):
            return True
    return False


def is_identifier(token: str) -> bool:
    """*token* reads as a name a person would recognise (words, numbers, versions, hosts, ids)."""
    if not token or _UUID.match(token) or _KNOWN_ID.match(token):
        return True
    token = _KNOWN_ID_INSIDE.sub("id", token)  # followup:t_01a1ecf2c3d6367b:notify, hermes-kanban-t_…
    if not _NPM_SPEC.match(token):
        unglobbed = re.sub(r"(?<![A-Za-z0-9])\*(?![A-Za-z0-9])", "", token)  # *.example.com, dir/*: globs
        if re.search(r"[A-Za-z0-9]", unglobbed) and (
                re.search(r"[!^$*]", unglobbed) or len(re.findall(r"[&#%@]", unglobbed)) >= 2):
            return False  # shell-unsafe symbols, or several of &#%@, in a name: a password, not an identifier
    if _joined_random(token):
        return False
    return all(_segment_ok(piece) for piece in _SEPARATORS.split(token))


# ---- names ----------------------------------------------------------------------------------------------------------

def _name_parts(name: str) -> list[str]:
    return [part.lower() for part in re.split(r"[-_.]", name.lstrip("-")) if part]


def _secret_index(parts: list[str]) -> int:
    """Index of the last part that marks a secret, else -1."""
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] in _SECRET_PARTS or _SECRET_WORD.search(parts[index]):
            return index
    return -1


def _is_secret_name(name: str) -> bool:
    """A flag, env or option name whose value is a secret. Fail closed: a secret word followed by an unknown part is
    still secret (--client-secret-value, --token-b64); only a setting part after it (or a switch prefix, or a public
    flag) clears it."""
    if "--" + name.lower().lstrip("-") in _PUBLIC_FLAGS:
        return False
    parts = _name_parts(name)
    if not parts:
        return False
    if "webhook" in parts or "webhooks" in parts:
        return True  # a webhook URL is the secret itself
    if parts[0] in _BOOLEAN_PREFIXES:
        return False
    index = _secret_index(parts)
    if index < 0:
        return False
    return not any(part in _SETTING_SUFFIXES or part in _COMMAND_SUFFIXES for part in parts[index + 1:])


def _is_command_name(name: str) -> bool:
    parts = _name_parts(name)
    return len(parts) > 1 and parts[-1] in _COMMAND_SUFFIXES and _secret_index(parts[:-1]) >= 0


def _counts_secrets(name: str) -> bool:
    """--max-tokens, --cap-tokens: a plural secret word counts secrets; a number after it is a count."""
    parts = _name_parts(name)
    index = _secret_index(parts)
    return index >= 0 and parts[index].endswith("s") and not parts[index].endswith("ss")


def _auth_only(name: str) -> bool:
    parts = _name_parts(name)
    return any(p.endswith("auth") for p in parts) and not any(
        p in _SECRET_PARTS or (_SECRET_WORD.search(p) and not p.endswith("auth")) for p in parts)


# ---- values -------------------------------------------------------------------------------------------------------

def _base64_piece(part: str) -> bool:
    """A piece base64 could have cut at "/": its alphabet, with mixed case or digits (not a plain word)."""
    return bool(re.fullmatch(r"[A-Za-z0-9+=]+", part)) and bool(
        re.search(r"\d", part) or (re.search(r"[a-z]", part) and re.search(r"[A-Z]", part)))


def _mask_components(value: str) -> str:
    """A path, URL path or query keeps its structure; each component that is not an identifier is masked. Adjacent
    components are also judged together, since base64 secrets carry "/" (``i0/WDpxZVIeYA8N/gAbxXu8i``)."""
    parts = _STRUCTURE.split(value)
    seps = _STRUCTURE.findall(value)
    bad = [not is_identifier(part) for part in parts]
    for index in range(len(parts) - 1):
        left, right = parts[index], parts[index + 1]
        if (seps[index] in "/#" and not bad[index] and not bad[index + 1] and _base64_piece(left)
                and _base64_piece(right) and not is_identifier(left + right)):
            bad[index] = bad[index + 1] = True
    for index, part in enumerate(parts):
        if re.search(r"[!^$*]", part) and re.search(r"[A-Za-z0-9]", part):
            bad[index] = True  # shell-unsafe symbols do not occur in path or URL names; they do in passwords
    out = [MASK if bad[0] else parts[0]]
    for sep, part, masked in zip(seps, parts[1:], bad[1:]):
        out += [sep, MASK if masked else part]
    return "".join(out)


def _mask_url(value: str) -> str:
    scheme, rest = value.split("://", 1)
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = f"{MASK}@{authority.rsplit('@', 1)[1]}"  # userinfo is a credential either way
    host = authority.rsplit("@", 1)[-1]
    if not is_identifier(host.split(":")[0]):
        authority = authority[: len(authority) - len(host)] + MASK
    return f"{scheme}://{authority}{slash}{_mask_components(path)}" if slash else f"{scheme}://{authority}"


def _mask_userpass(value: str) -> str:
    """``user:pass`` / ``user%pass`` / ``user,pass`` -> the user (if it reads as one) and ``***``."""
    split = re.match(r"^([^:%,]*)([:%,])(.*)$", value, re.DOTALL)
    if split is None:
        return value if is_identifier(value) else MASK
    user, sep, _secret = split.groups()
    return f"{user if user and is_identifier(user) else MASK}{sep}{MASK}"


def _mask_command(value: str) -> str:
    program, _, rest = value.strip().partition(" ")
    return f"{_judge(program, secret=False, depth=1)} {MASK}" if rest.strip() else value


def _judge_secret(value: str, *, name: str) -> str:
    """The value of a secret-named flag or name: masked, except an auth mechanism word, a count, or a file that
    holds the secret (an absolute path of 2+ directories made of names)."""
    if _auth_only(name) and value.lower() in _AUTH_MODES:
        return value
    if _counts_secrets(name) and value.isdigit():
        return value
    if (_PATH_START.match(value) and re.fullmatch(r"[\w./\\:~-]+", value) and value.count("/") >= 2
            and _mask_components(value) == value):
        return value
    return MASK


def _judge_assignment(name: str, inner: str, *, depth: int) -> str:
    if _is_command_name(name):
        return f"{name}={_mask_command(inner)}"
    if name.lower().rsplit(".", 1)[-1] in _KEY_ID_NAMES and re.fullmatch(r"[0-9A-Fa-f]{8,40}", inner):
        return f"{name}={inner}"
    if _is_secret_name(name):
        return f"{name}={_judge_secret(inner, name=name)}"
    return f"{name}={_judge(inner, secret=False, depth=depth + 1)}"


def _mask_shell(words: list[str], *, depth: int) -> str:
    """Shell text: leading ``NAME=value`` env words, then a program and its arguments."""
    out: list[str] = []
    index = 0
    while index + 1 < len(words) and (assignment := _ASSIGNMENT.match(words[index])):
        out.append(_judge_assignment(assignment.group(1), words[index][assignment.end():], depth=depth))
        index += 1
    if index < len(words):
        program = words[index]
        out.append(_judge(program, secret=False, depth=depth + 1))
        out.extend(mask_argv_secrets(words[index + 1:], program=program, _depth=depth + 1))
    return " ".join(out)


def _judge(value: str, *, secret: bool, depth: int, name: str = "") -> str:
    """*value* as shown: kept when it reads as an identifier (and no name marked it secret), masked otherwise."""
    if not value:
        return value
    if depth > _MAX_DEPTH:
        return MASK
    if secret:
        return _judge_secret(value, name=name)
    assignment = _ASSIGNMENT.match(value)
    if assignment and value[assignment.end():] and value[assignment.end()] != "=":
        name = assignment.group(1)
        if any(c.isspace() for c in value) and _ENV_NAME.match(name):
            return _mask_shell(value.split(), depth=depth)  # SLACK_TOKEN=x ./run: env prefix, then a command
        # the name must read as words even run together: AB12_CD34_EF56_GH78=… is random segments, not a name
        if not (is_identifier(name) and _segment_ok(re.sub(r"[^A-Za-z0-9]", "", name))):
            return MASK
        return _judge_assignment(name, value[assignment.end():], depth=depth)
    if header := _HEADER.match(value):
        name, rest = header.groups()
        secret_header = name.lower() in _SECRET_HEADERS or _is_secret_name(name)
        return f"{name}: {MASK if secret_header else _judge(rest, secret=False, depth=depth + 1)}"
    if any(c.isspace() for c in value):
        return _mask_shell(value.split(), depth=depth)
    if _URL.match(value):
        return _mask_url(value)
    if (userinfo := _USERINFO.match(value)) and userinfo.group(1).lower() not in _NOT_USERINFO and not re.match(
            r"sha\d+$", userinfo.group(3)):
        return f"{userinfo.group(1)}:{MASK}@{value[userinfo.end(2) + 1:]}"
    if _PATH_START.match(value):
        return _mask_components(value)
    if sep := next((c for c in ",|" if c in value), None):
        # a list (--enable-features=A,B,C, A|B): each item on its own, so one odd item does not hide the rest
        return sep.join(_judge(item, secret=False, depth=depth + 1) for item in value.split(sep))
    return value if is_identifier(value) else MASK


# ---- argv -----------------------------------------------------------------------------------------------------------

def _program_flags(program: str):
    return _PROGRAM_SECRET_FLAGS.get(os.path.basename(program or "").lower().removesuffix(".exe"))


def _long_flag(token: str) -> str | None:
    """The name of a long flag: ``--name``, Go-style ``-name`` (5+ letters, or with ``=``) or a Java ``-Dname=…``."""
    if token.startswith("--"):
        name = token[2:].partition("=")[0]
        shaped = bool(re.fullmatch(r"[A-Za-z][\w.-]*", name))
    elif re.fullmatch(r"-D[A-Za-z_][\w.-]*=.*", token, re.DOTALL):
        name, shaped = token[2:].partition("=")[0], True
    else:
        name = token[1:].partition("=")[0]
        shaped = bool(re.fullmatch(r"[A-Za-z][\w.-]{2,}", name)) and ("=" in token or len(name) > 4)
    # a name must read as one: a random token that starts with "-" (urlsafe base64) is a value, not a flag
    return name if shaped and is_identifier(name) else None


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
            return f"{short}{MASK if short in attached else _mask_userpass(token[2:])}", None
    # combined short switches ending in a user flag: curl -su alice:pw, -fsSLu alice:pw
    letters = {f[1] for f in userpass if len(f) == 2}
    if re.fullmatch(r"-[A-Za-z]{2,}", token) and token[-1] in letters:
        return token, "userpass"
    return None


def _flag(token: str, name: str, depth: int) -> str:
    """A long flag as shown: its name, and its ``=value`` judged under that name."""
    head, eq, value = token.partition("=")
    if not eq:
        return token
    if _is_command_name(name):
        return f"{head}={_mask_command(value)}"
    if _is_secret_name(name):
        return f"{head}={_judge_secret(value, name=name)}"
    return f"{head}={_judge(value, secret=False, depth=depth)}"


def _pending_value(token: str, kind: str, name: str, *, depth: int) -> str | None:
    """The argument after a flag that takes a secret, or None when it is the next flag instead (the secret flag was
    a switch). Only a long ``--flag`` whose name reads as one counts as a flag: a secret may start with "-"."""
    if re.fullmatch(r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:=.*)?", token, re.DOTALL):
        return None  # a kebab-case --flag: the secret flag was a switch
    if kind == "userpass":
        return _mask_userpass(token)
    if kind == "command":
        return _mask_command(token)
    return _judge_secret(token, name=name)


def mask_argv_secrets(argv: list[str], *, program: str = "", _depth: int = 0) -> list[str]:
    """*argv* (without the program) as it may be shown; *program* (argv[0]) selects its own password flags."""
    flags = _program_flags(program) or _NO_FLAGS
    wrapper = os.path.basename(program or "").lower() in _COMMAND_WRAPPERS
    out: list[str] = []
    pending: tuple[str, str] | None = None  # (what the next argument is, the flag that asked for it)
    for token in argv:
        if token == INLINE_CODE:
            out.append(token)
            pending = None
            continue
        if pending is not None:
            (kind, name), pending = pending, None
            shown = _pending_value(token, kind, name, depth=_depth)
            if shown is not None:
                out.append(shown)
                continue
        if (handled := _program_flag(token, flags)) is not None:
            shown, kind = handled
            out.append(shown)
            pending = (kind, token) if kind else None
            continue
        if token.startswith("-") and (name := _long_flag(token)):
            out.append(_flag(token, name, _depth))
            if "=" not in token and _is_command_name(name):
                pending = ("command", name)
            elif "=" not in token and _is_secret_name(name):
                pending = ("secret", name)
            continue
        if re.fullmatch(r"-[A-Za-z][A-Za-z0-9]{0,3}", token):
            out.append(token)  # a short switch or cluster
            continue
        if (wrapped := _program_flags(token)) is not None:
            flags, wrapper = wrapped, False  # sudo mysql …, docker exec db mysql …, env X=1 mysql …
            out.append(token)
            continue
        if wrapper and "=" not in token and not token.isdigit():
            # sshpass -p x ssh -p 2222 …: from the wrapped program on, its own flags (here none) apply
            flags, wrapper = _NO_FLAGS, os.path.basename(token).lower() in _COMMAND_WRAPPERS
        out.append(_judge(token, secret=False, depth=_depth))
    return out
