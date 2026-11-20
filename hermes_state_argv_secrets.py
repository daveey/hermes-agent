"""Mask secrets in a process's argv before it is shown to a person (``hermes doctor`` holder lists).

Fail closed: an argument is shown only when it parses as an identifier -- words, numbers, versions, hosts, model
names, Hermes and cloud ids, and paths and URLs whose parts are such names -- and masked (``***``) otherwise, so a
secret shape nobody listed is still hidden. Values a name marks as secret are masked whatever they look like: after
a secret-named flag (``--webhook-secret``, ``--api-key-value``, ``-Dapi.key=``, Go-style ``-password``), in
``NAME=value`` and ``Header: value`` pairs, ``user:pass@host``, a program's own password flags (``mysql -pPASS``,
``sshpass -p``, ``curl -u user:pass``, ``-b cookie``), also behind ``sudo``/``docker exec``/``env``/``bash -c`` and
env prefixes in shell strings. ``agent.redact`` still runs afterwards for known secret formats and URL credentials.

Limits, measured on generated secrets of every common shape (letters and digits, base32/64, urlsafe, hex, symbol
passwords, single- and mixed-case letters, grouped keys) as a bare argument, in an absolute or relative path, in a
URL, as a URL query name or value, in a list, inside a spaced env value or a flag value and as a ``NAME=`` value --
a shown tail of 8+ characters counts as a leak: the worst shape and position shows about 8.5% of 16-character
secrets, 3% of 24-character, 2% of 32-character and 1.5% of 40-character ones (random letters can read like words);
the tests hold these under 10%, 5%, 3% and 2%. About 0.15% of this repo's identifiers and 0.1% of its env names are
masked. This is a rate, not a guarantee, and some secrets look exactly like identifiers and are shown: pronounceable
passwords (``correcthorse``), all-digit values, UUIDs, strings under 12 characters and a secret shaped like a
kebab-case ``--flag`` right after a secret-named flag (read as the next flag, as switches need), unless a name marks
them secret. Short hex (up to 12 characters) is shown on purpose: it is how commits are abbreviated; longer hex (full
commits, digests, container ids) is masked with the keys it looks like. Encoded query values are judged decoded;
non-ASCII characters in them (``%E2%9C%93``) are shown. ``--pin`` and ``--private`` are not secret names on purpose
(switches or provider slugs in Hermes and common tools); the query names ``code``, ``session`` and ``hash`` are, so
harmless values there are masked too.
"""

from __future__ import annotations

import os
import re
import shlex
import urllib.parse
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
    "arn", "fd", "stdin",
})
# Parts that make a plural secret name a count (--max-tokens 4096, --tokens-per-delta 512), not a list of secrets.
# Model families and formats: short words in model names that the letter statistics cannot vouch for.
_MODEL_WORDS = frozenset({"qwen", "llama", "mistral", "mixtral", "gemma", "phi", "deepseek", "gpt", "claude", "grok",
                          "kimi", "glm", "falcon", "olmo", "nemotron", "granite", "gemini", "codestral", "devstral",
                          "smollm", "dwq", "gguf", "awq", "gptq", "mlx", "bnb", "fp", "bf", "int", "bit", "instruct"})
_COUNT_PARTS = frozenset({"max", "min", "cap", "per", "limit", "num", "count", "total", "budget"})
# Query parameters that carry a credential although their name has no secret word.
_QUERY_SECRETS = frozenset({"sid", "sig", "signature", "code", "session", "hash", "pass", "pwd", "otp"})
_COMMAND_SUFFIXES = frozenset({"cmd", "command", "helper"})
# A first part that makes a flag a switch: it takes no value, so the next argument is not its secret.
_BOOLEAN_PREFIXES = frozenset({"no", "show", "rotate", "migrate", "with", "list", "print", "clear", "reset", "skip",
                               "allow", "require", "enable", "disable", "isolated", "ask", "prompt", "keep"})
_PUBLIC_FLAGS = frozenset({"--ssh-session-token-file", "--keychain", "--monkey", "--keyboard", "--author",
                           "--max-tokens", "--min-tokens", "--cache-read-tokens", "--newkey", "--keyform",
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
    "curl": (("-b",), ("-b",), ("-u", "--user", "-U", "--proxy-user")), "smbclient": ((), (), ("-U", "--user")),
    "lftp": ((), (), ("-u", "--user")),
}
_NO_FLAGS = ((), (), ())
# A separate -u/--user argument that is user:pass has its password masked (mytool -u admin:pw, CURL_ARGS='-u …'); a
# bare user and a numeric uid:gid stay. Programs whose -u/--user names a user or user:group keep it whole (ps -u
# daveey:staff, docker --user app:app), and -url=…, -upstream=… are other flags.
_GENERIC_USER_FLAGS = frozenset({"-u", "--user"})
_USER_FLAG_PROGRAMS = frozenset({
    "ps", "pgrep", "pkill", "killall", "top", "htop", "lsof", "w", "who", "last", "crontab", "chown", "chgrp",
    "sudo", "doas", "su", "runuser", "setpriv", "docker", "podman", "nerdctl",
})
# Container runners: their own -u/--user (before the image or container) names a user; from the image on, the
# arguments belong to the program run inside it (docker run --rm img tool -u admin:pw). Positional arguments before
# the image: the subcommand (compose adds one, the service). A flag's value counted as one ends this early, so a
# later user:group is masked, never a password shown.
_CONTAINER_PROGRAMS = frozenset({"docker", "podman", "nerdctl"})
# A wrapper's own flags that take a value, which is not the wrapped program (sudo -u admin ps …, nice -n 5 cmd).
_WRAPPER_VALUE_FLAGS = {
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D", "-R", "-T"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "runuser": frozenset({"-u", "-g", "-G", "--user", "--group"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "xargs": frozenset({"-n", "-I", "-P", "-d", "-L", "-s", "-E", "-a"}),
    "chroot": frozenset({"--userspec", "--groups"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "watch": frozenset({"-n", "-d", "--interval"}),
    "flock": frozenset({"-w", "-E", "--timeout", "--conflict-exit-code"}),
}
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")  # timeout 5m cmd, sleep-like positional values
_COOKIE_FLAGS = frozenset({"-b", "--cookie", "cookie"})
# Flags whose value is a file glob, where a leading "!" negates it (rg --glob=!node_modules, -g '!*.lock').
_GLOB_FLAGS = frozenset({"-g", "--glob", "--iglob", "--exclude", "--include", "--exclude-dir", "--ignore",
                         "--ignore-glob"})
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
_COMMON_MIN, _VOWEL_MIN, _RARE_MAX, _CLUSTER_MIN = 0.55, 0.1, 0.25, 7
# a long all-caps word with a 5-consonant cluster is random unless its letter pairs read strongly (PASSPHRASE)
_CLUSTER_MIN_UPPER, _UPPER_LEN, _UPPER_CLUSTER_COMMON = 5, 8, 0.75
_ALPHA_FREE = 6  # letter runs this short carry too little to be a secret on their own (iOS, gRPC)
_ALPHA_FREE_LOWER = 7  # one letter longer when all lowercase (execvpe, openxml): random runs are mostly mixed case
_SHORT_WORD, _MAX_SHORT_WORDS, _MIN_AVG_WORD = 2, 3, 2.5  # camelCase word-shape limits
# key groups: equal-length chunks, 3+ of 5-8 characters or 4+ of 3-4, mostly not words (OKWV-IDIA-T5UR-…)
_KEY_GROUP_LEN, _KEY_GROUPS_LONG, _KEY_GROUPS_SHORT = (3, 8), 3, 4
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
_KNOWN_ID_BODY = (r"(?:t_[0-9a-f]+|proc_[0-9a-f]+|\d{8}_\d{6}_[0-9a-z]+|tail[0-9a-f]{4,10}|"
                  r"(?:i|mi|vol|snap|sg|subnet|vpc|ami|eni|igw|rtb|nat|acl|lt)-[0-9a-f]{8,17})")
# … plus Go pseudo-versions
_KNOWN_ID = re.compile(rf"^(?:{_KNOWN_ID_BODY}|v\d+\.\d+\.\d+-(?:0\.)?\d{{14}}-[0-9a-f]{{12}})$")
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
_NAME_COLON = re.compile(r"^([A-Za-z_][\w.-]*):(\S+)$")  # name:value, judged by the name
# A query parameter name, percent-decoded: word characters, dots, dashes and brackets (user[password], whose parts
# _name_parts splits), with an OData/Graph "$" in front ($select, $top).
_QUERY_NAME = re.compile(r"^\$?[\w.\-\[\]]+$")
_IMAGE_DIGEST = re.compile(r"^[\w.-]+@sha\d+:[0-9a-fA-F]+$")  # image@sha256:… (the long hex is still masked)
# A file that holds a key or credential, named after a secret flag (openssl -key server.key). A secret that looks
# exactly like such a file name (--password notes.txt) is shown, like other secrets shaped as identifiers.
_SECRET_FILE = re.compile(r"^[\w.-]+\.(?:key|pem|crt|cer|der|p12|pfx|jks|keystore|pub|asc|gpg|txt|json|ya?ml|env)$")
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
    upper_cluster = max((len(m) for w in words if w.isupper() and len(w) >= _UPPER_LEN
                         for m in re.findall(r"[^aeiouy]+", w.lower())), default=0)
    clustered = longest_cluster >= _CLUSTER_MIN or (
        upper_cluster >= _CLUSTER_MIN_UPPER and common < _UPPER_CLUSTER_COMMON)
    # one unbroken word of 14+ letters is rare in identifiers and common in random strings: it must read strongly
    floor = _COMMON_MIN_LONG if len(lowered) == 1 and len(letters) >= _LONG_WORD else _COMMON_MIN
    return common < floor or vowels < _VOWEL_MIN or rare > _RARE_MAX or clustered


def _letters_look_random(letters: str) -> bool:
    """Letters (possibly camelCase) that are not words: random case flips, or letters that do not read as words."""
    if len(set(letters.lower())) <= 2:
        return False  # a repeated letter (padding, placeholders) is no secret
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", letters)
    if len(words) > 1 and sum(len(w) for w in words) / len(words) < _MIN_AVG_WORD:
        return True  # "XyQwErTy": a capital every other letter
    if len(letters) >= 10 and sum(len(w) <= _SHORT_WORD for w in words) >= _MAX_SHORT_WORDS:
        return True  # names have at most a couple of short words (is, By, Id); random case has many (Ub…Lz…Jw)
    acronyms = [w for w in words if len(w) <= _SHORT_WORD and w.isupper()]
    # one short acronym (JS, IO) carries no letter statistics; several are random case flips (…JPvGXi…)
    judged = [w for w in words if len(w) >= 3] if len(acronyms) <= 1 else words
    return _letters_random(*(judged if len(judged) > 1 else [letters]))


def _alpha_ok(run: str) -> bool:
    """A letter run that is a word, an acronym or camelCase words. Runs of up to 6 letters carry too little to be a
    secret on their own (iOS, gRPC, eBPF); longer runs are judged."""
    free = _ALPHA_FREE_LOWER if run.islower() else _ALPHA_FREE
    return len(run) <= free or not _letters_look_random(run)


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


def _short_hex(alnum: str) -> bool:
    return len(alnum) <= _SHORT_HEX and bool(re.fullmatch(r"[0-9a-fA-F]+", alnum))


def _is_word_group(group: str) -> bool:
    """A chunk that reads as a word or number (hermes, pytest, 2507), not a generated group (T5UR, OKWV)."""
    if group.isdigit() or _short_hex(group) and not re.search(r"\d", group):
        return True
    consistent = group.islower() or group.isupper() or group.istitle()  # ETMRrk, KvJecc: random case
    return group.isalpha() and consistent and (len(group) <= 2 or not _letters_random(group))


def _key_groups(groups: list[str]) -> bool:
    """Equal-length chunks, mostly not words: a license, recovery or grouped API key (OKWV-IDIA-T5UR-F8U2-…)."""
    if not groups or len({len(g) for g in groups}) != 1:
        return False
    size = len(groups[0])
    low, high = _KEY_GROUP_LEN
    needed = _KEY_GROUPS_LONG if size >= 5 else _KEY_GROUPS_SHORT
    if not (low <= size <= high and len(groups) >= needed) or all(g.isdigit() for g in groups):
        return False
    return sum(not _is_word_group(g) for g in groups) * 2 > len(groups)


def _random_group_letters(chunks: list[str]) -> bool:
    """Short chunks whose letters, pairs taken inside each chunk, do not read as words (ZPTK29.PAJDOL.INCT13)."""
    group_letters = [re.sub(r"[^A-Za-z]", "", chunk) for chunk in chunks if chunk]
    if len(group_letters) < 3 or sum(map(len, group_letters)) < _MIN_JOINED:
        return False
    if any(len(g) >= 5 and g.islower() and not _letters_random(g) for g in group_letters):
        return False  # a real lowercase word among the chunks: a name with parts (us-west-2.compute.amazonaws)
    words = [g for g in group_letters if len(g) >= 2]
    return len(words) >= 3 and all(w.isupper() or w.islower() for w in words) and _letters_random(*words)


def _random_chunk(alnum: str) -> bool:
    """One long chunk (12+ characters) that is generated: letters and digits alternating, or letters that do not
    read as words."""
    if len(alnum) < _MIN_JOINED or _short_hex(alnum):
        return False
    if len(re.findall(r"[A-Za-z]+|\d+", alnum)) >= 6:
        return True
    letters = re.sub(r"[^A-Za-z]", "", alnum)
    return len(letters) >= 8 and _letters_look_random(letters)


def _words_with_short_ids(token: str) -> bool:
    """A name of real words with short id or version parts (hermes-gateway-7d9f8c6b5-x2k4p, v1.2.3-rc4+build5):
    its letter/digit changes come from the short parts, not from one generated string."""
    chunks = [c for c in re.split(r"[-_.+/]", token) if c]
    words = [c for c in chunks
             if ((m := re.fullmatch(r"([a-z]{5,})\d*", c)) and not _letters_random(m.group(1)))
             or re.sub(r"\d+", "", c).lower() in _MODEL_WORDS]
    return bool(words) and all(len(c) <= 10 for c in chunks if c not in words)


def _joined_random(token: str) -> bool:
    """The token is generated, judged with its symbols removed so that symbols cannot cut a secret into short,
    innocent-looking pieces (``uAkqc7c#pilIq7s9``, ``+QBYaSITMRV…``, ``ab12_cd34_ef56_gh78``)."""
    stem = re.sub(r"\.[A-Za-z][A-Za-z0-9]{0,4}$", "", token)  # a file extension must not hide a key (KEY.json)
    for candidate in {token, stem}:
        whole = re.sub(r"[^A-Za-z0-9]", "", candidate)
        if (len(whole) >= _MIN_JOINED and not _short_hex(whole) and _flip_density(whole) >= _FLIP_DENSITY_MAX
                and not _words_with_short_ids(candidate)):
            return True
    for candidate in {token, stem}:
        chunks = [re.sub(r"[^A-Za-z0-9]", "", chunk) for chunk in _WORD_SEPARATORS.split(candidate)]
        if (_key_groups([c for c in chunks if c]) or _random_group_letters(chunks)
                or any(_random_chunk(chunk) for chunk in chunks)):
            return True
    return False


def is_identifier(token: str) -> bool:
    """*token* reads as a name a person would recognise (words, numbers, versions, hosts, ids)."""
    if not token or _UUID.match(token) or _KNOWN_ID.match(token):
        return True
    token = _KNOWN_ID_INSIDE.sub("id", token)  # followup:t_01a1ecf2c3d6367b:notify, hermes-kanban-t_…
    if not _NPM_SPEC.match(token):
        # globs (*.example.com, dir/*, gc*) and printf tokens (%r@%h:%p) are not password symbols
        unglobbed = re.sub(r"(?:(?<=[./:\-*(])|^)\*|\*(?=[./:\-*)]|$)|(?<![A-Za-z0-9])%[A-Za-z](?![A-Za-z0-9])", "",
                           token)
        if not re.search(r"[^\w.*/-]", unglobbed) and re.search(r"\.[A-Za-z]{1,5}$|/", unglobbed):
            unglobbed = unglobbed.replace("*", "")  # foo*bar.txt, src/*util*/x: a file glob
        if re.search(r"[A-Za-z0-9]", unglobbed) and (
                re.search(r"[!^$*]", unglobbed) or len(re.findall(r"[&#%@]", unglobbed)) >= 2):
            return False  # shell-unsafe symbols, or several of &#%@, in a name: a password, not an identifier
    if _joined_random(token):
        return False
    return all(_segment_ok(piece) for piece in _SEPARATORS.split(token))


# ---- names ----------------------------------------------------------------------------------------------------------

def _name_parts(name: str) -> list[str]:
    return [part.lower() for part in re.split(r"[-_.\[\]]", name.lstrip("-")) if part]  # user[password]


def _secret_index(parts: list[str]) -> int:
    """Index of the last part that marks a secret, else -1."""
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] in _KEY_ID_NAMES or parts[index] in _SETTING_SUFFIXES:
            continue  # signingkey, keyid: a key's id, not the key
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
    """--cap-tokens, --tokens-per-delta: a plural secret word with a count part counts secrets; a number after it
    is a count. A plain plural (--github-tokens, --api-keys) lists secrets."""
    parts = _name_parts(name)
    index = _secret_index(parts)
    plural = index >= 0 and parts[index].endswith("s") and not parts[index].endswith("ss")
    return plural and any(part in _COUNT_PARTS for part in parts)


def _auth_only(name: str) -> bool:
    parts = _name_parts(name)
    return any(p.endswith("auth") for p in parts) and not any(
        p in _SECRET_PARTS or (_SECRET_WORD.search(p) and not p.endswith("auth")) for p in parts)


# ---- values -------------------------------------------------------------------------------------------------------

def _base64_piece(part: str) -> bool:
    """A piece base64 could have cut at "/": its alphabet (a file extension aside), with mixed case or digits."""
    part = re.sub(r"\.[A-Za-z][A-Za-z0-9]{0,4}$", "", part)
    return bool(re.fullmatch(r"[A-Za-z0-9+=]+", part)) and bool(
        re.search(r"\d", part) or (re.search(r"[a-z]", part) and re.search(r"[A-Z]", part)))


def _spread_to_base64_neighbours(parts: list[str], seps: list[str], bad: list[bool]) -> None:
    """A base64 secret cut at "/" or "#": two pieces that pass alone but not together are masked, and so are the
    pieces next to a masked piece. One pass over pairs, then a forward and a backward sweep: linear."""
    joinable = [sep in "/#" for sep in seps]
    for index in range(len(parts) - 1):  # two pieces that pass alone but not together (UKSjptB/XAwzL4sS)
        left, right = parts[index], parts[index + 1]
        if (joinable[index] and not bad[index] and not bad[index + 1] and _base64_piece(left)
                and _base64_piece(right) and not is_identifier(left + right)):
            bad[index] = bad[index + 1] = True
    for index in range(1, len(parts)):
        if joinable[index - 1] and bad[index - 1] and len(parts[index]) >= 4 and _base64_piece(parts[index]):
            bad[index] = True
    for index in range(len(parts) - 2, -1, -1):
        if joinable[index] and bad[index + 1] and len(parts[index]) >= 4 and _base64_piece(parts[index]):
            bad[index] = True


def _segments(seps: list[str], count: int) -> list[range]:
    """Index ranges of the pieces in each "/" segment."""
    ranges, first = [], 0
    for index, sep in enumerate(seps):
        if sep == "/":
            ranges.append(range(first, index + 1))
            first = index + 1
    ranges.append(range(first, count))
    return ranges


def _mask_password_segments(parts: list[str], seps: list[str], bad: list[bool], content_bad: list[bool], *,
                            through_question: bool = False) -> None:
    """The path part of a "/" segment (before any "?") with password symbols is one secret cut at # & @ %: when that
    part as a whole is not a name, mask all of its pieces. An image digest (name@sha256:…) is not one."""
    for segment in _segments(seps, len(parts)):
        pieces = []
        for index in segment:
            pieces.append(index)
            if index < len(seps) and seps[index] == "?" and not through_question:
                break
        text = "".join(parts[i] + (seps[i] if i != pieces[-1] else "") for i in pieces)
        if (re.search(r"[&#%@]", text) and re.search(r"[A-Za-z0-9]", text) and not _IMAGE_DIGEST.match(text)
                and (any(content_bad[i] for i in pieces) or not is_identifier(text))):
            for index in pieces:
                bad[index] = True


def _mask_components(value: str, *, depth: int = 0) -> str:
    """A path or URL path (with any query and fragment) keeps its structure: each path component that is not an
    identifier is masked, base64 pieces next to a masked piece too, and a segment that is a symbol password cut into
    pieces (# & @ %) as a whole; the query after the first "?" is judged pair by pair under its names."""
    path, question, query = value.partition("?")
    cut = bool(question) and _password_cut_at_question(path, query)
    if question and not cut:
        return f"{_mask_components(path, depth=depth)}?{_mask_query(query, depth=depth)}"
    parts = _STRUCTURE.split(value)
    seps = _STRUCTURE.findall(value)
    content_bad = [not is_identifier(part) for part in parts]
    bad = list(content_bad)
    _spread_to_base64_neighbours(parts, seps, bad)
    _mask_password_segments(parts, seps, bad, content_bad, through_question=cut)
    out = [MASK if bad[0] else parts[0]]
    for sep, part, masked in zip(seps, parts[1:], bad[1:]):
        out += [sep, MASK if masked else part]
    return "".join(out)


def _password_cut_at_question(path: str, query: str) -> bool:
    """A "?" inside a symbol password, not a query: the last path piece already looks like part of a secret, and it
    and the first query word, joined, are not a name and carry password symbols."""
    last = path.rsplit("/", 1)[-1]
    if not last or (is_identifier(last) and not re.search(r"[!^$*#%@&]", last)):
        return False  # a clean path piece: the "?" starts a real query
    if re.search(r"[!^$*#@&]", last):
        return True  # a path piece with password symbols: the secret goes on past the "?" (Xk9#mP2q?L7vN&…)
    first = re.split(r"[&/]", query, maxsplit=1)[0]
    joined = f"{last}?{first}"
    return bool(re.search(r"[!^$*#%@]", joined)) and not is_identifier(joined.replace("?", "-"))


def _judge_query_value(value: str, *, depth: int) -> str:
    """A query value, percent-decoded when it is encoded ($filter=Price%20gt%2020): shown as written when every
    decoded word passes, else masked whole (the words of one value are one secret as much as one phrase)."""
    if "%" not in value:
        return _judge(value, secret=False, depth=depth)
    decoded = urllib.parse.unquote(value)
    if decoded == value:
        return _judge(value, secret=False, depth=depth)
    words = [word for word in re.split(r"[\s+]+", re.sub(r"[^\x00-\x7f]", " ", decoded)) if word]
    shown = [_judge(word, secret=False, depth=depth) for word in words]
    return value if all(MASK not in word for word in shown) else MASK


def _mask_query(query: str, *, depth: int) -> str:
    """``a=1&token=x``: each name must read as one (a secret used as a name is masked), and each value is judged
    whole under its name (an e-mail with "@" stays one value)."""
    out = []
    masking = False  # pieces after a masked pair that are not well-formed pairs: the rest of a secret cut at "&"
    for pair in query.split("&"):
        name, eq, value = pair.partition("=")
        plain = urllib.parse.unquote(name)
        well_formed = bool(eq) and bool(_QUERY_NAME.match(plain)) and is_identifier(re.sub(r"[$\[\]]", "-", plain))
        if masking and not well_formed:
            out.append(MASK)
            continue
        masking = False
        if not eq:
            masking = not is_identifier(pair)
            out.append(MASK if masking else pair)
        elif not well_formed:
            masking = True
            out.append(f"{MASK}={MASK if value else value}")  # the name is the secret (?Xk9mP2…=1), and its value
        elif _is_secret_name(plain) or plain.lower() in _QUERY_SECRETS:
            masking = bool(value)  # a secret value cut at "&" (pass=sun&shine) goes on into the pieces after it
            out.append(f"{name}={MASK if value else value}")
        else:
            judged = _judge_query_value(value, depth=depth + 1)
            masking = MASK in judged
            out.append(f"{name}={judged}")
    return "&".join(out)


def _mask_url(value: str, *, depth: int = 0) -> str:
    scheme, rest = value.split("://", 1)
    split = re.search(r"[/?#]", rest)
    authority, tail = (rest[: split.start()], rest[split.start():]) if split else (rest, "")
    if "@" in authority:
        authority = f"{MASK}@{authority.rsplit('@', 1)[1]}"  # userinfo is a credential either way
    host = authority.rsplit("@", 1)[-1]
    if not is_identifier(host.split(":")[0]):
        authority = authority[: len(authority) - len(host)] + MASK
    # path, query and fragment together: a "#" or "?" inside a secret must not cut it into innocent pieces
    return f"{scheme}://{authority}{_mask_components(tail, depth=depth) if tail else ''}"


def _mask_userpass(value: str) -> str:
    """``user:pass`` / ``user%pass`` / ``user,pass`` -> the user (if it reads as one) and ``***``; a bare key used as
    the user (``-u KEY:``) is masked too."""
    split = re.match(r"^([^:%,]*)([:%,])(.*)$", value, re.DOTALL)
    if split is None:
        return value if is_identifier(value) else MASK
    user, sep, secret = split.groups()
    if user.isdigit() and secret.isdigit():
        return value  # uid:gid
    return f"{user if user and is_identifier(user) else MASK}{sep}{MASK}"


def _mask_command(value: str, *, depth: int = 0) -> str:
    program, _, rest = value.strip().partition(" ")
    return f"{_judge(program, secret=False, depth=depth + 1)} {MASK}" if rest.strip() else value


def _judge_secret(value: str, *, name: str) -> str:
    """The value of a secret-named flag or name: masked, except an auth mechanism word, a count, or a file that
    holds the secret (an absolute path of 2+ directories made of names, or a key/cert file name)."""
    if _auth_only(name) and value.lower() in _AUTH_MODES:
        return value
    if _counts_secrets(name) and value.isdigit():
        return value
    if _SECRET_FILE.match(value) and is_identifier(value):
        return value  # openssl -key server.key, --cert-key tls.pem
    if (_PATH_START.match(value) and re.fullmatch(r"[\w./\\:~-]+", value) and value.count("/") >= 2
            and _mask_components(value) == value):
        return value
    return MASK


def _judge_assignment(name: str, inner: str, *, depth: int) -> str:
    if _is_command_name(name):
        return f"{name}={_mask_command(inner, depth=depth)}"
    if name.lower().rsplit(".", 1)[-1] in _KEY_ID_NAMES and re.fullmatch(r"[0-9A-Fa-f]{8,40}", inner):
        return f"{name}={inner}"
    if _is_secret_name(name):
        return f"{name}={_judge_secret(inner, name=name)}"
    return f"{name}={_judge(inner, secret=False, depth=depth + 1)}"


def _shell_words(text: str) -> list[tuple[str, str]] | None:
    """Shell text as ``(raw, value)`` words: *raw* as written (quotes kept, for showing), *value* as the shell would
    pass it. None when its quoting does not parse."""
    raws: list[str] = []
    current, quote, escaped = [], "", False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quote != "'":
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "'\"":
            current.append(char)
            quote = char
        elif char.isspace():
            if current:
                raws.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        raws.append("".join(current))
    words = []
    for raw in raws:
        try:
            values = shlex.split(raw, posix=True)
        except ValueError:
            return None
        words.append((raw, values[0] if values else ""))
    return words


def _mask_shell(text: str, *, depth: int) -> str:
    """Shell text: leading ``NAME=value`` env words, then a program and its arguments; a word is shown as written
    unless masking changed it. Text whose quoting does not parse is masked whole: a quoted secret could span any
    number of words."""
    words = _shell_words(text)
    if words is None:
        return MASK
    raws, values = [raw for raw, _ in words], [value for _, value in words]
    shown: list[str] = []
    index = 0
    while index + 1 < len(values) and (assignment := _ASSIGNMENT.match(values[index])):
        shown.append(_judge_assignment(assignment.group(1), values[index][assignment.end():], depth=depth))
        index += 1
    if index < len(values):
        if values[index].startswith("-"):
            shown.extend(mask_argv_secrets(values[index:], _depth=depth + 1))  # flags, not a program (OPTS=…)
        else:
            shown.append(_judge(values[index], secret=False, depth=depth + 1))
            shown.extend(mask_argv_secrets(values[index + 1:], program=values[index], _depth=depth + 1))
    return " ".join(raw if out == value else out for raw, value, out in zip(raws, values, shown))


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
        if _is_secret_name(name):
            return f"{name}={_judge_secret(value[assignment.end():], name=name)}"  # all of it, spaces included
        if not is_identifier(name):
            return MASK  # AB12_CD34_EF56_GH78=…: random segments, not a name
        return _judge_assignment(name, value[assignment.end():], depth=depth)  # OPTS='-Xmx1g -Dapi.token=…' too
    if (named := _NAME_COLON.match(value)) and _is_secret_name(named.group(1)):
        return f"{named.group(1)}:{MASK}"  # api_key:sunshine
    if header := _HEADER.match(value):
        name, rest = header.groups()
        secret_header = name.lower() in _SECRET_HEADERS or _is_secret_name(name)
        return f"{name}: {MASK if secret_header else _judge(rest, secret=False, depth=depth + 1)}"
    if any(c.isspace() for c in value):
        return _mask_shell(value, depth=depth)
    if value.startswith("-") and (flag_name := _long_flag(value)):
        return _flag(value, flag_name, depth + 1)  # --jvm-arg=-Dpassword=…, JAVA_OPTS=-Dtoken=…
    if _URL.match(value):
        return _mask_url(value, depth=depth)
    if (userinfo := _USERINFO.match(value)) and userinfo.group(1).lower() not in _NOT_USERINFO and not re.match(
            r"sha\d+$", userinfo.group(3)):
        return f"{userinfo.group(1)}:{MASK}@{value[userinfo.end(2) + 1:]}"
    if sep := next((c for c in ",|" if c in value), None):
        # a list (--enable-features=A,B,C, A|B): each item on its own, so one odd item does not hide the rest
        return sep.join(_judge(item, secret=False, depth=depth + 1) for item in value.split(sep))
    if _PATH_START.match(value) or "/" in value:
        return _mask_components(value)  # absolute or relative (data/x.json, anthropic/claude-opus-4.7)
    return value if is_identifier(value) else MASK


# ---- argv -----------------------------------------------------------------------------------------------------------

def _program_flags(program: str):
    return _PROGRAM_SECRET_FLAGS.get(os.path.basename(program or "").lower().removesuffix(".exe"))


def _split_flag(token: str) -> tuple[str, str, str]:
    """``(head, separator, value)`` of a flag token; the value follows the first ``=`` or ``:`` (``/p:``-style)."""
    match = re.match(r"^(-{1,2}[^=:]*)([=:])(.*)$", token, re.DOTALL)
    return match.groups() if match else (token, "", "")


def _long_flag(token: str) -> str | None:
    """The name of a long flag, or None. Flag shapes: ``--name``; Go-style ``-name`` (5+ letters, or with a value);
    a Java ``-Dname=…`` property. The name must read as one, so a random token that starts with ``-`` (urlsafe
    base64) is a value, not a flag. Short ``-x`` switches and clusters are handled by the caller."""
    head, sep, _value = _split_flag(token)
    if token.startswith("--"):
        name = head[2:]
        shaped = bool(re.fullmatch(r"[A-Za-z][\w.-]*", name))
    else:  # Go-style -name; -Dname=value (Java properties) is one of these, named "Dname"
        name = head[1:]
        shaped = bool(re.fullmatch(r"[A-Za-z][\w.-]{2,}", name)) and (bool(sep) or len(name) > 4)
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
            # attached value (only for flags listed as attached: openssl -key is a flag, not -k with "ey"):
            # mysql -pPASS, curl -bsession=x, curl -ualice:pw
            return f"{short}{_mask_userpass(token[2:]) if short in userpass else MASK}", None
    # combined short switches ending in a flag that takes a value: curl -su alice:pw, curl -sb session=x
    if re.fullmatch(r"-[A-Za-z]{2,}", token):
        if token[-1] in {f[1] for f in userpass if len(f) == 2}:
            return token, "userpass"
        if token[-1] in {f[1] for f in separate if len(f) == 2}:
            return token, "secret"
    return None


def _flag(token: str, name: str, depth: int) -> str:
    """A long flag as shown: its name, and its ``=value`` (or ``:value``) judged under that name."""
    head, sep, value = _split_flag(token)
    if not sep:
        return token
    if _is_command_name(name):
        return f"{head}{sep}{_mask_command(value, depth=depth)}"
    if _is_secret_name(name):
        return f"{head}{sep}{_judge_secret(value, name=name)}"
    if head in _GLOB_FLAGS and value.startswith("!"):
        return f"{head}{sep}!{_judge(value[1:], secret=False, depth=depth)}"
    return f"{head}{sep}{_judge(value, secret=False, depth=depth)}"


def _pending_value(token: str, kind: str, name: str, *, depth: int) -> str | None:
    """The argument after a flag that takes a secret, or None when it is the next flag instead: a kebab-case
    ``--flag`` (the secret-named flag was a switch). Anything else is its value, also when it starts with ``-``."""
    if re.fullmatch(r"--[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:=.*)?", token, re.DOTALL):
        return None
    if kind == "userpass":
        return _mask_userpass(token)
    if kind == "command":
        return _mask_command(token, depth=depth)
    if name in _COOKIE_FLAGS and "=" not in token and (_PATH_START.match(token) or is_identifier(token)):
        return token  # curl -b cookies.txt: a cookie-jar file, not a cookie
    return _judge_secret(token, name=name)


def mask_argv_secrets(argv: list[str], *, program: str = "", _depth: int = 0) -> list[str]:
    """*argv* (without the program) as it may be shown; *program* (argv[0]) selects its own password flags."""
    flags = _program_flags(program) or _NO_FLAGS
    current = os.path.basename(program or "").lower()  # the program whose flags these are
    wrapper = current in _COMMAND_WRAPPERS
    wrapper_value = False  # the previous argument was a wrapper's own flag that takes a value (sudo -u admin)
    container, before_image = current in _CONTAINER_PROGRAMS, 2  # positionals left before the image is passed
    glob_next = False  # the previous argument was a glob flag (rg -g '!node_modules')
    out: list[str] = []
    pending: tuple[str, str] | None = None  # (what the next argument is, the flag that asked for it)
    for token in argv:
        if token == INLINE_CODE:
            out.append(token)
            pending = None
            continue
        skip_handoff, wrapper_value = wrapper_value, wrapper and token in _WRAPPER_VALUE_FLAGS.get(current, ())
        if glob_next and token.startswith("!"):
            glob_next = False
            out.append("!" + _judge(token[1:], secret=False, depth=_depth))
            continue
        glob_next = token in _GLOB_FLAGS
        if pending is not None:
            (kind, name), pending = pending, None
            if (shown := _pending_value(token, kind, name, depth=_depth)) is not None:
                out.append(shown)
                continue
        if container and not token.startswith("-") and "=" not in token:
            before_image -= 1
            if token == "compose":
                before_image += 1  # docker compose exec <service> …
            if before_image <= 0:  # the image or container: from here on the arguments belong to the program inside
                container, current, wrapper = False, "", True
                out.append(_judge(token, secret=False, depth=_depth))
                continue
        if token in _GENERIC_USER_FLAGS and current not in _USER_FLAG_PROGRAMS:
            out.append(token)
            pending = ("userpass", token)
            continue
        if (handled := _program_flag(token, flags)) is not None:
            shown, kind = handled
            out.append(shown)
            pending = (kind, token if len(token) == 2 else "-" + token[-1]) if kind else None
            continue
        if token.startswith("-") and (name := _long_flag(token)):
            out.append(_flag(token, name, _depth))
            valued = bool(_split_flag(token)[1]) or token.startswith("-D")
            if not valued and _is_command_name(name):
                pending = ("command", name)
            elif not valued and _is_secret_name(name):
                pending = ("secret", name)
            continue
        if re.fullmatch(r"-[A-Za-z][A-Za-z0-9]{0,3}", token):
            out.append(token)  # a short switch or cluster; Go-style -pass/-pw take a secret
            pending = ("secret", token) if _is_secret_name(token) else None
            continue
        if (wrapped := _program_flags(token)) is not None:
            flags, wrapper = wrapped, False  # sudo mysql …, docker exec db mysql …, env X=1 mysql …
            current = os.path.basename(token).lower()
            out.append(token)
            continue
        if wrapper and not skip_handoff and "=" not in token and not _DURATION.fullmatch(token):
            # sshpass -p x ssh -p 2222 …: from the wrapped program on, its own flags (here none) apply
            current = os.path.basename(token).lower()
            flags, wrapper = _NO_FLAGS, current in _COMMAND_WRAPPERS
        out.append(_judge(token, secret=False, depth=_depth))
    return out
