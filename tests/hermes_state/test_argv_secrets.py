"""Secrets in a process's argv are masked before ``hermes doctor`` shows the process (holder lists are pasted into
issues); the identifiers a person needs to tell processes apart are kept.

The mask fails closed: an argument is shown only when it reads as an identifier. Names (secret-named flags,
``NAME=value``, headers, programs' own password flags) mask their values whatever they look like.
"""

import base64
import random
import secrets as _secrets
import string

import pytest

from hermes_state_argv_secrets import MASK, mask_argv_secrets

KEY = "q8ZpL2vXk9RtYwB4mN7cQ1sD5fH3jK6g"


def shown(argv, program="tool"):
    return mask_argv_secrets(argv, program=program)


# ---- values a name marks as secret ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, expected",
    [
        # secret-named flags, both spellings; short, numeric, "/"-containing and hex values alike
        (["--webhook-secret", "Zx9Lk2PqRt7V"], ["--webhook-secret", MASK]),
        (["--signing-key=hunter2"], [f"--signing-key={MASK}"]),
        (["--auth", "abc"], ["--auth", MASK]),
        (["--DB-PASSWORD", "pw"], ["--DB-PASSWORD", MASK]),
        (
            ["--aws-secret-access-key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"],
            ["--aws-secret-access-key", MASK],
        ),
        (["--webhook-secret=Zm9vYmFyYmF6/cXV4cXV1eA=="], [f"--webhook-secret={MASK}"]),
        (
            ["--webhook-secret", "12345678901234567890", "--signing-key=4242424242"],
            ["--webhook-secret", MASK, f"--signing-key={MASK}"],
        ),
        # fail closed: a part after the secret word keeps the flag secret unless it names a setting
        (
            [
                "--client-secret-value",
                "x/y",
                "--token-b64",
                "Zm9v",
                "--api-key-value",
                "hunter2",
            ],
            [
                "--client-secret-value",
                MASK,
                "--token-b64",
                MASK,
                "--api-key-value",
                MASK,
            ],
        ),
        (
            [
                "--secret-key-base",
                "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
                "--auth-header",
                "Bearer.x",
            ],
            ["--secret-key-base", MASK, "--auth-header", MASK],
        ),
        (
            ["--private-key-pem=abc", "--apikey", "x", "--github-tokens", "Qx7L/w"],
            [f"--private-key-pem={MASK}", "--apikey", MASK, "--github-tokens", MASK],
        ),
        # secret words the shared redactor does not list
        (
            [
                "--passphrase",
                "correct-horse",
                "--pass",
                "x",
                "--pw",
                "x",
                "--otp",
                "123456",
                "--jwt",
                "a.b.c",
            ],
            [
                "--passphrase",
                MASK,
                "--pass",
                MASK,
                "--pw",
                MASK,
                "--otp",
                MASK,
                "--jwt",
                MASK,
            ],
        ),
        (
            ["--authorization=Bearer abc.def.ghi", "--hmac", "x", "--sftp-pass", "x"],
            [f"--authorization={MASK}", "--hmac", MASK, "--sftp-pass", MASK],
        ),
        # Go-style single-dash and Java -D property flags
        (
            [
                "-password",
                "sunshine",
                "-Dpassword=sunshine",
                "-Dapi.key=sunshine",
                "-Dtoken=x",
            ],
            [
                "-password",
                MASK,
                f"-Dpassword={MASK}",
                f"-Dapi.key={MASK}",
                f"-Dtoken={MASK}",
            ],
        ),
        # the argument after a secret flag is its value even when it starts with "-"
        (["--password", "-abcdefgh"], ["--password", MASK]),
        (["--token", "-12345678"], ["--token", MASK]),
        (["--password", "--Xy9kq2LmZ"], ["--password", MASK]),
        (["--password", "-Xy9"], ["--password", MASK]),
        # a mechanism word stays only after an auth-only flag; a path after a secret flag must look like a file
        (
            ["--password", "agent", "--token", "prompt"],
            ["--password", MASK, "--token", MASK],
        ),
        (
            ["--password", "~/hunter2", "--token", "./x"],
            ["--password", MASK, "--token", MASK],
        ),
        (["--token=/K7MDENG+bPxRfiCYEXAMPLEKEY=="], [f"--token={MASK}"]),
        # NAME=value, nested values, headers, webhooks
        (
            ["API_TOKEN=abc123", "Config=" + KEY],
            [f"API_TOKEN={MASK}", f"Config={MASK}"],
        ),
        (
            ["--env=API_TOKEN=abc", "--annotation=password=x"],
            [f"--env=API_TOKEN={MASK}", f"--annotation=password={MASK}"],
        ),
        (
            ["CREDS=x", "PASSPHRASE=x", "OPENROUTER_API_KEY_2024=sk12"],
            [f"CREDS={MASK}", f"PASSPHRASE={MASK}", f"OPENROUTER_API_KEY_2024={MASK}"],
        ),
        (
            ["-H", "X-Api-Key: hunter2", "-H", "Authorization: Bearer abc"],
            ["-H", f"X-Api-Key: {MASK}", "-H", f"Authorization: {MASK}"],
        ),
        (
            ["--webhook-url", "https://hooks.slack.com/services/T0/B0/x"],
            ["--webhook-url", MASK],
        ),
        # user:pass@host, with or without a dotted host
        (
            [
                "alice:hunter2@git.example.com",
                "admin:sunshine@db",
                "admin:sunshine@db-primary:5432/app",
            ],
            [
                f"alice:{MASK}@git.example.com",
                f"admin:{MASK}@db",
                f"admin:{MASK}@db-primary:5432/app",
            ],
        ),
        # commands that print a secret show only their program
        (
            ["--password-cmd", "echo hunter2", "--api-key-helper=pass show openai"],
            ["--password-cmd", f"echo {MASK}", f"--api-key-helper=pass {MASK}"],
        ),
        # random segments posing as a name
        (["AB12_CD34_EF56_GH78_IJ90_KL12=x"], [MASK]),
        # base64 padding is not an assignment
        (
            ["Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==", "Xy9kq2LmZp4Rt7Wb3NcQ8abc="],
            [MASK, MASK],
        ),
        # symbol passwords
        (["P@ssw0rd!2024xyz"], [MASK]),
    ],
)
def test_named_secrets_are_masked(argv, expected):
    assert shown(argv) == expected


@pytest.mark.parametrize(
    "argv, program, expected",
    [
        # programs' own password flags
        (
            ["-u", "root", "-phunter2", "db"],
            "/usr/bin/mysql",
            ["-u", "root", f"-p{MASK}", "db"],
        ),
        (["-p", "hunter2", "ssh", "host"], "sshpass", ["-p", MASK, "ssh", "host"]),
        (["-a", "hunter2", "ping"], "redis-cli", ["-a", MASK, "ping"]),
        (["--pass", "hunter2"], "redis-cli", ["--pass", MASK]),
        (
            ["enc", "-k", "hunter2", "-pass", "pass:hunter2"],
            "openssl",
            ["enc", "-k", MASK, "-pass", MASK],
        ),
        (["-p", "hunter2"], "mongosh", ["-p", MASK]),
        (["-w", "hunter2"], "ldapsearch", ["-w", MASK]),
        (["-P", "hunter2"], "sqlcmd", ["-P", MASK]),
        (["-storepass", "hunter2"], "keytool", ["-storepass", MASK]),
        (["-phunter2", "x.7z"], "7z", [f"-p{MASK}", "x.7z"]),
        (["-P", "hunter2"], "zip", ["-P", MASK]),
        (["-N", "hunter2"], "ssh-keygen", ["-N", MASK]),
        # curl/smbclient/lftp user:pass in every spelling, a key as the user, a random token as the user, cookies
        (
            ["-u", "alice:hunter2", "https://x"],
            "curl",
            ["-u", f"alice:{MASK}", "https://x"],
        ),
        (["-ualice:hunter2"], "curl", [f"-ualice:{MASK}"]),
        (
            ["--user=alice:hunter2", "--proxy-user=bob:pw123"],
            "curl",
            [f"--user=alice:{MASK}", f"--proxy-user=bob:{MASK}"],
        ),
        (["-su", "alice:hunter2"], "curl", ["-su", f"alice:{MASK}"]),
        (["-u", "sk9Lm2QxPz7Rt4Wb:"], "curl", ["-u", f"{MASK}:{MASK}"]),
        (["-u", "Xk9mP2qL7vN4aB3cZ8wRtY5u"], "curl", ["-u", MASK]),
        (["-b", "session=sunshine"], "curl", ["-b", MASK]),
        (["-U", "alice%hunter2"], "smbclient", ["-U", f"alice%{MASK}"]),
        (["--user=alice%hunter2"], "smbclient", [f"--user=alice%{MASK}"]),
        (
            ["-u", "alice,hunter2", "sftp://h"],
            "lftp",
            ["-u", f"alice,{MASK}", "sftp://h"],
        ),
        # behind wrappers and inside shell strings
        (["mysql", "-uroot", "-phunter2"], "sudo", ["mysql", "-uroot", f"-p{MASK}"]),
        (
            ["exec", "-it", "db", "mysql", "-phunter2"],
            "docker",
            ["exec", "-it", "db", "mysql", f"-p{MASK}"],
        ),
        (["X=1", "mysql", "-phunter2"], "env", ["X=1", "mysql", f"-p{MASK}"]),
        (["-c", "mysql -phunter2 shop"], "bash", ["-c", f"mysql -p{MASK} shop"]),
        (
            ["-c", "SLACK_TOKEN=sunshine ./run"],
            "sh",
            ["-c", f"SLACK_TOKEN={MASK}"],
        ),
        (["-c", "TOKEN=sunshine exec ./run"], "sh", ["-c", f"TOKEN={MASK}"]),
        (
            ["-o", "ProxyCommand=sshpass -p sunshine ssh h"],
            "ssh",
            ["-o", f"ProxyCommand=sshpass -p {MASK} ssh h"],
        ),
        # sshpass hides its own password, not the wrapped ssh's port
        (
            ["-p", "hunter2", "ssh", "-p", "2222", "host"],
            "sshpass",
            ["-p", MASK, "ssh", "-p", "2222", "host"],
        ),
    ],
)
def test_program_secrets_are_masked(argv, program, expected):
    assert shown(argv, program) == expected


def test_a_secret_inside_a_credential_helper_command_is_masked():
    out = " ".join(
        shown(["-c", "credential.helper=!f() { echo password=hunter2; }; f"], "git")
    )
    assert "hunter2" not in out


# ---- identifiers are kept ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        # models, versions, platforms
        "gpt-5.6-sol-900k",
        "grok-code-fast-1",
        "qwen3-14b-ctx16k",
        "Qwen3.8-27B-Q4_K_M",
        "claude-opus-4-5-20250929",
        "anthropic/claude-opus-4.7",
        "1.2.3-beta.4+build.567",
        "manylinux2014_x86_64",
        "python3.14-nodejs22",
        "macosarm64",
        "x86_64",
        "utf8",
        "v1beta1",
        "k8s",
        "-XX:+UseG1GC",
        # ids people match on
        "proc_467899b1fa1b",
        "i-0abc123def4567890",
        "t_01a1ecf2c3d6367b",
        "20261119_163442_8f3a2c",
        "8ec9ab93-b372-4c03-ac51-7abb31174eb9",
        "0362a20c0e39",
        "GIT_CONFIG_KEY_17",
        "HERMES_RELAY_ID_2024",
        "followup:t_01a1ecf2c3d6367b:notify",
        # files, programs, hosts, refs
        "Wide310x150Logo.png",
        "VCRUNTIME140.dll",
        "libpython3.14.dylib",
        "hermes_hub.py",
        "origin/main",
        "feature/argv-secret-gaps",
        "hermes-hub",
        "127.0.0.1:8000",
        "api.openrouter.ai",
        "alice@example.com",
        "git@github.com:org/repo.git",
        "python:3.12-slim",
        "npm:react@18.3.1",
        "github:org/repo@v1",
        "https://github.com/org/repo@main",
        "git+https://github.com/org/tool@v1.2.0",
        "s3.amazonaws.com",
        "ec2-user@ec2-54-12-34-56.us-west-2.compute.amazonaws.com",
        "@playwright/mcp@latest",
        "/tmp/hermes-kanban-t_01a1ecf2c3d6367b",
        "/x/workspaces/t_9c1f00ab2d3e4f51/out",
        # words and camelCase, short mixed case, other scripts
        "internationalization",
        "isHermesHubPickerUrl",
        "WebKitWebContentProcess",
        "ServerAliveCountMax",
        "testSessionOwnerByRuntimeId",
        "FullscreenBubbleShowOrigin",
        "ExperimentalJSProfiler",
        "workflow",
        "launchctl",
        "mdworker",
        "iOS",
        "iPad",
        "tvOS",
        "eBPF",
        "gRPC",
        "DBus",
        "getX",
        "naïve-café",
        "Привет",
        "production",
        "gateway",
        "serve",
        "dashboard",
        "_runtime-serve",
        "release-workflow.yml",
    ],
)
def test_identifiers_people_read_are_kept(token):
    assert shown([token]) == [token]


@pytest.mark.parametrize(
    "argv, program",
    [
        (
            ["-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"],
            "python3",
        ),
        (
            [
                "serve",
                "--isolated",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--ssh-session-token-file",
                "/Users/x/.hermes/desktop-ssh/session-a/token",
            ],
            "hermes",
        ),
        (
            [
                "chat",
                "--resume",
                "20261119_163442_8f3a2c",
                "--model",
                "gpt-5.6-sol-900k",
                "--session-key",
                "20261119_163442_8f3a2c",
            ],
            "hermes",
        ),
        (["kanban", "run", "t_01a1ecf2c3d6367b"], "hermes"),
        (["process", "kill", "proc_467899b1fa1b"], "hermes"),
        (["-p", "coder", "gateway", "run"], "hermes"),
        (
            [
                "--show-tokens",
                "default",
                "--rotate-tokens",
                "work",
                "--migrate-secrets",
                "x",
            ],
            "hermes",
        ),
        (["mcp", "add", "--auth", "oauth", "srv"], "hermes"),
        (
            [
                "--cap-tokens",
                "4096",
                "--tokens-per-delta",
                "512",
                "--turn1-max-tokens",
                "800",
            ],
            "python3",
        ),
        (
            [
                "--token-endpoint",
                "https://auth.example.com/token",
                "--use-partner-key-as-auth",
                "flux",
            ],
            "python3",
        ),
        (
            [
                "--max-tokens",
                "4096",
                "--min-tokens=16",
                "--cache-read-tokens",
                "2048",
                "--token-env",
                "OPENAI_API_KEY",
            ],
            "x",
        ),
        (
            [
                "--idempotency-key",
                "followup:t_01a1ecf2c3d6367b:notify",
                "--step-key",
                "build",
                "--passkey-browser",
                "chrome",
            ],
            "x",
        ),
        (["--model", "Qwen3-Coder-480B-A35B-Instruct", "--api-key-timeout", "30"], "x"),
        (
            [
                "--password-file",
                "/home/ubuntu/.hermes/chrome-agent-vnc.pass",
                "--auth",
                "~/.hermes/egress-socks.auth",
            ],
            "x",
        ),
        (
            [
                "--key-file",
                "C:\\Users\\x\\key.pem",
                "--token-file",
                "./token.txt",
                "--cookie-jar",
                "~/.hermes/c.txt",
            ],
            "x",
        ),
        (
            [
                "--access-key-id",
                "deploy-bot",
                "--secret-name",
                "prod/db",
                "--api-key-env",
                "OPENAI_API_KEY",
            ],
            "x",
        ),
        (["--keyring-backend", "macos", "--auth-mode", "oauth"], "x"),
        (
            [
                "profile",
                "create",
                "--isolated-auth",
                "work",
                "--no-credentials",
                "scratch",
            ],
            "hermes",
        ),
        (["auth", "login", "--with-token", "github.com"], "gh"),
        (["workflow", "run", "release.yml"], "gh"),
        (["--auth-server-whitelist=*.corp.example.com"], "Electron"),
        (
            [
                "--enable-features=AutofillActorMode,BackForwardCache,FullscreenBubbleShowOrigin"
            ],
            "Electron",
        ),
        (
            [
                "--origin-trial-disabled-features=CanvasTextNg|WebAssemblyCustomDescriptors"
            ],
            "Electron",
        ),
        (["-c", "user.signingkey=ABCDEF0123456789"], "git"),
        (["-k", "testSessionOwnerByRuntimeId"], "pytest"),
        (["start-session", "--target", "i-0abc123def4567890"], "aws"),
        (
            [
                "-o",
                "ServerAliveCountMax=3",
                "-o",
                "NumberOfPasswordPrompts=0",
                "-p",
                "2222",
                "host",
            ],
            "ssh",
        ),
        (["-u", "alice", "https://x"], "curl"),
        (
            [
                "run",
                "python:3.12-slim",
                "127.0.0.1:8000",
                "alice@example.com",
                "git@github.com:org/repo.git",
            ],
            "docker",
        ),
        (
            [
                "HERMES_RELAY_ID_2024=abc",
                "OPENROUTER_BASE_URL=https://openrouter.ai/api/v1",
            ],
            "env",
        ),
        (
            ["--annotation=platform=macosarm64", "--annotation=_version=5.11492.5"],
            "crashpad",
        ),
        (["-c", "launchctl kickstart -k gui/501/com.example.agent"], "bash"),
        (["--profile", "p" * 120, "zzzzzzzzzzzzzzzzzzzzzzzz"], "x"),
        (["--author", "daveey", "--keyboard", "us"], "x"),
    ],
)
def test_hermes_and_tool_arguments_are_kept(argv, program):
    assert shown(argv, program) == argv


def test_lists_are_judged_item_by_item():
    assert shown([f"--enable-features=Alpha,{KEY},Gamma"]) == [
        f"--enable-features=Alpha,{MASK},Gamma"
    ]
    assert shown([f"--x=a|{KEY}|b"]) == [f"--x=a|{MASK}|b"]


def test_paths_and_urls_keep_their_structure_and_mask_only_random_parts():
    assert shown([f"/srv/cache/{KEY}/data.json"]) == [f"/srv/cache/{MASK}/data.json"]
    assert shown([f"https://api.example.com/v1/items?token={KEY}&page=2"]) == [
        f"https://api.example.com/v1/items?token={MASK}&page=2"
    ]
    assert shown([f"/tmp={KEY}"]) == [f"/tmp={MASK}"]
    assert shown([f"~/{KEY}"]) == [f"~/{MASK}"]


def test_the_first_word_of_shell_text_is_judged_too():
    assert shown(["-c", f"{KEY} --help"], "sh") == ["-c", f"{MASK} --help"]
    assert shown(["-c", f"/opt/{KEY}/run --help"], "sh") == [
        "-c",
        f"/opt/{MASK}/run --help",
    ]


@pytest.mark.parametrize(
    "token",
    [
        "0362a20c0e39651291280105e81f7c42f4f435f7",  # a full commit
        "python@sha256:9f2b8c1e4d7a6053b2e1f0c9d8a7b6e5f4c3d2e1a0b9c8d7e6f5a4b3c2d1e0f9",  # an image digest
        "/Users/x/.hermes/desktop-ssh/5b38f00bfab44cbcc8f1a2b3c4d5e6f7/token",  # a 32-hex session folder
    ],
)
def test_long_hex_is_masked_like_the_keys_it_resembles(token):
    """Deliberate: 16+ hex characters are masked whether a digest or a key; short commits stay readable."""
    assert MASK in shown([token])[0]


def test_deep_nesting_is_masked_not_a_crash():
    out = shown(["a=" * 1000 + "x"])[0]
    assert out.startswith("a=") and out.endswith(MASK)


# ---- fail closed: generated secrets are masked at the documented rate, in every position ------------------------

# Leak rate allowed per secret length (a random string sometimes reads as a name; the shorter, the likelier): the
# worst shape and position measured over several seeds, plus about two standard deviations at 400 samples.
_LEAK_CEILING = {16: 0.10, 24: 0.05, 32: 0.03, 40: 0.02}
_SHAPES = [
    "grouped_dot_mixed",
    "alnum",
    "lower_digits",
    "upper_digits",
    "base32",
    "lower_base32",
    "hex",
    "mixed_letters",
    "symbols",
    "base64",
    "urlsafe",
    "lower",
    "upper",
    "grouped_dash",
    "grouped_underscore",
    "grouped_dot",
]
_POSITIONS = {
    "bare": lambda s: ["worker.py", s],
    "spaced_env": lambda s: [f"OPTS=--verbose {s} --x"],
    "flag_value": lambda s: [f"--jvm-arg=-Dregion={s}"],
    "list": lambda s: [f"--ids=alpha,{s},beta"],
    "query_name": lambda s: [f"https://api.example.com/v1/items?{s}=1&page=2"],
    "query_value": lambda s: [f"https://api.example.com/v1/items?q={s}&page=2"],
    "relative_path": lambda s: [f"data/{s}.json"],
    "path": lambda s: [f"/srv/data/{s}/file.json"],
    "url": lambda s: [f"https://api.example.com/v1/{s}?page=2"],
    "assignment": lambda s: [f"--config=region={s}"],
}


def _shape(rng: random.Random, kind: str, length: int) -> str:
    def pick(alphabet: str, n: int = length) -> str:
        return "".join(rng.choice(alphabet) for _ in range(n))

    def grouped(sep: str, size: int) -> str:
        groups = [
            pick(string.ascii_uppercase + string.digits, size)
            for _ in range(max(3, length // size))
        ]
        return sep.join(groups)

    return {
        "alnum": lambda: pick(string.ascii_letters + string.digits),
        "lower_digits": lambda: pick(string.ascii_lowercase + string.digits),
        "upper_digits": lambda: pick(string.ascii_uppercase + string.digits),
        "base32": lambda: pick(string.ascii_uppercase + "234567"),
        "lower_base32": lambda: pick(string.ascii_lowercase + "234567"),
        "hex": lambda: pick("0123456789abcdef"),
        "mixed_letters": lambda: pick(string.ascii_letters),
        "symbols": lambda: pick(string.ascii_letters + string.digits + "!@#$%^&*?"),
        "base64": lambda: base64.b64encode(rng.randbytes(length)).decode()[:length],
        "urlsafe": lambda: base64.urlsafe_b64encode(rng.randbytes(length)).decode()[
            :length
        ],
        "lower": lambda: pick(string.ascii_lowercase),
        "upper": lambda: pick(string.ascii_uppercase),
        "grouped_dash": lambda: grouped("-", 4),
        "grouped_underscore": lambda: grouped("_", 4),
        "grouped_dot": lambda: grouped(".", 6),
        "grouped_dot_mixed": lambda: ".".join(
            pick(string.ascii_letters + string.digits, 6)
            for _ in range(max(3, length // 6))
        ),
    }[kind]()


# Every position runs the shortest and longest lengths; the middle ones run where the code differs most (bare
# arguments, paths, URLs, env values), so the suite stays fast. Query positions share the URL code.
_FULL_POSITIONS = frozenset({"bare", "path", "url", "spaced_env"})
_RATE_CASES = [
    (position, kind, length)
    for position in sorted(_POSITIONS)
    for kind in _SHAPES
    for length in sorted(_LEAK_CEILING)
    if length in (16, 40) or (length == 24 and not position.startswith("query_")) or position in _FULL_POSITIONS
]


@pytest.mark.parametrize("position, kind, length", _RATE_CASES)
def test_generated_secrets_are_masked_at_the_documented_rate(position, kind, length):
    rng = random.Random(f"{position}/{kind}/{length}")
    leaked = 0
    for _ in range(400):
        secret = _shape(rng, kind, length)
        out = " ".join(shown(_POSITIONS[position](secret), "python3"))
        leaked += secret in out or secret[-8:] in out
    assert leaked / 400 <= _LEAK_CEILING[length], (
        f"{position}/{kind}/{length}: {leaked / 4:.1f}% shown"
    )


@pytest.mark.parametrize(
    "make, ceiling",
    [
        (lambda: _secrets.token_urlsafe(16), _LEAK_CEILING[24]),  # 22 characters
        (lambda: _secrets.token_hex(16), 0.0),  # 32 hex: always masked
        (lambda: _secrets.token_urlsafe(32), _LEAK_CEILING[40]),  # 43 characters
    ],
)
def test_python_secrets_module_tokens_are_masked_at_the_documented_rate(make, ceiling):
    tokens = [make() for _ in range(1000)]
    leaked = sum(token in " ".join(shown(["--opt", "x", token])) for token in tokens)
    assert leaked / len(tokens) <= ceiling


# ---- the third review: shell quoting, path and URL pieces, names, flags ----------------------------------------------


@pytest.mark.parametrize(
    "argv, program, secret",
    [
        (["-c", "PASSPHRASE='correct horse battery staple' ./run"], "bash", "horse"),
        (["-c", "./run --passphrase 'correct horse battery staple'"], "bash", "horse"),
        (["-c", './run --password="two words"'], "bash", "words"),
        (["PASSPHRASE=correct horse battery"], "docker", "horse"),
        (["-c", "./run --token 'unbalanced quote"], "bash", "unbalanced"),
    ],
)
def test_quoted_secrets_in_shell_text_are_masked_whole(argv, program, secret):
    assert secret not in " ".join(shown(argv, program))


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://h/cb?pass=sunshine&page=2", f"https://h/cb?pass={MASK}&page=2"),
        (
            "https://h/cb?pwd=x&otp=123456&sid=abc&hash=def",
            f"https://h/cb?pwd={MASK}&otp={MASK}&sid={MASK}&hash={MASK}",
        ),
        ("https://h/cb?api_key=x", f"https://h/cb?api_key={MASK}"),
    ],
)
def test_query_values_are_judged_by_their_name(value, expected):
    assert shown([value]) == [expected]


def test_base64_pieces_next_to_a_masked_piece_are_masked():
    assert shown(["/srv/data/UKSjptB/XAwzL4sS/file.json"]) == [
        f"/srv/data/{MASK}/{MASK}/file.json"
    ]
    assert "XAwzL4sS" not in shown(["/dsccDeT/S2G5OsA"])[0]


def test_symbols_in_a_path_component_are_masked():
    assert shown(["/srv/a!b9/file"]) == [f"/srv/{MASK}/file"]


def test_relative_paths_with_grouped_keys_are_masked():
    assert MASK in shown(["data/ABCD-EF12-GH34-IJ56.json"])[0]
    assert MASK in shown(["data/ZPTK29.PAJDOL.INCT13.json"])[0]


@pytest.mark.parametrize(
    "argv, expected",
    [
        (
            [
                "--tokens",
                "123456789012",
                "--github-tokens",
                "12345678",
                "--api-keys",
                "98765",
            ],
            ["--tokens", MASK, "--github-tokens", MASK, "--api-keys", MASK],
        ),
        (
            ["--password:hunter2", "api_key:sunshine"],
            [f"--password:{MASK}", f"api_key:{MASK}"],
        ),
        (["-pass", "sunshine", "-pw", "sunshine"], ["-pass", MASK, "-pw", MASK]),
    ],
)
def test_more_name_shapes_mask_their_values(argv, expected):
    assert shown(argv) == expected


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["-bsession=sunshine"], [f"-b{MASK}"]),
        (["-sb", "session=sunshine"], ["-sb", MASK]),
        (["-b", "cookies.txt"], ["-b", "cookies.txt"]),
        (["--cookie", "/tmp/jar.txt"], ["--cookie", "/tmp/jar.txt"]),
    ],
)
def test_curl_cookies_in_every_spelling(argv, expected):
    assert shown(argv, "curl") == expected


@pytest.mark.parametrize(
    "argv, program",
    [
        (["HERMES_KANBAN_BRANCH=main"], "env"),
        (
            [
                "HERMES_KANBAN_SOURCE_COMMIT=0362a20c0e39",
                "HERMES_UPDATE_PAUSED=1",
                "hermes",
                "update",
            ],
            "env",
        ),
        (
            [
                "HERMES_DESKTOP_CWD=/srv/x",
                "HERMES_RPC_DIR=/run/rpc",
                "SETUP_READY_EVENT=1",
                "DEFAULT_COMMAND_TIMEOUT=30",
            ],
            "env",
        ),
        (["run", "qwen3:30b-a3b-instruct-2507-q4_K_M"], "ollama"),
        (
            ["--model", "/models/Qwen3-Coder-30B-A3B-Instruct-UD-Q4_K_XL.gguf"],
            "llama-server",
        ),
        (["logs", "hermes-gateway-7d9f8c6b5-x2k4p"], "kubectl"),
        (
            [
                "ms-playwright/mcp-chrome-a1b2c3d4",
                "hermes-pytest-3kx9a2",
                "v1.2.3-rc4+build5",
            ],
            "x",
        ),
        (["cp312-cp312-manylinux_2_17_x86_64"], "pip"),
        (["https://hermes-mini.tail1a2b3c.ts.net:9147"], "curl"),
        (
            [
                "--secret-arn",
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-db",
            ],
            "x",
        ),
        (["--passphrase-fd", "0", "--target", "mi-0123456789abcdef0"], "x"),
        (["-Dhttp.proxyHost=proxy.example.com", "-Dfile.encoding=UTF-8"], "java"),
    ],
)
def test_names_the_third_review_found_masked_are_kept(argv, program):
    assert shown(argv, program) == argv


def test_java_properties_and_url_hosts_are_judged():
    assert shown(["-Dapi.key=sunshine"], "java") == [f"-Dapi.key={MASK}"]
    assert shown([f"https://{KEY}.example.com/x"]) == [f"https://{MASK}/x"]


def test_image_digests_are_not_user_and_password():
    assert shown(["python:3.12-slim@sha256:0362a20c0e39"]) == [
        "python:3.12-slim@sha256:0362a20c0e39"
    ]


# ---- the fourth review: values that are argv, switches, openssl, digests, quoting, speed -----------------------------


@pytest.mark.parametrize(
    "argv, program, secret",
    [
        (["JAVA_TOOL_OPTIONS=-Xmx1g -Dapi.token=sunshine"], "env", "sunshine"),
        (["-e", "JAVA_TOOL_OPTIONS=-Xmx1g -Dapi.token=sunshine"], "docker", "sunshine"),
        (["OPTS=--verbose --token sunshine", "run"], "env", "sunshine"),
        (
            ["GIT_SSH_COMMAND=sshpass -p sunshine ssh", "git", "fetch"],
            "env",
            "sunshine",
        ),
        (["CURL_ARGS=-u admin:sunshine", "./x"], "env", "sunshine"),
        (
            ["JAVA_OPTS=-Dpassword=hunter2", "--jvm-arg=-Dpassword=hunter2"],
            "x",
            "hunter2",
        ),
        (["--args", "--token sunshine"], "x", "sunshine"),
        (["--ids=a,3kjw7/gd4EQ6tyKW"], "x", "gd4EQ6tyKW"),
        (["https://h/x?q=7%0Nv5^zU^aX2ZRi@FLc0tYI"], "x", "FLc0tYI"),
    ],
)
def test_values_that_are_argv_are_judged_as_argv(argv, program, secret):
    assert secret not in " ".join(shown(argv, program))


def test_a_flag_value_that_is_itself_a_flag_is_not_swallowed():
    """main's rule, kept: a kebab-case --flag after a secret-named flag is the next flag (that flag was a switch)."""
    assert shown(["--token", "--verbose", "x"]) == ["--token", "--verbose", "x"]


@pytest.mark.parametrize(
    "argv, program",
    [
        (["--ask-become-pass", "-i", "inv", "site.yml"], "ansible-playbook"),
        (["--ask-vault-pass", "site.yml"], "ansible-playbook"),
        (["login", "--password-stdin", "-u", "me", "ghcr.io"], "docker"),
        (
            [
                "--token-stdin",
                "-v",
                "--keep-secrets",
                "build",
                "--ask-password",
                "--host",
                "db",
            ],
            "x",
        ),
        (
            [
                "req",
                "-key",
                "server.key",
                "-keyout",
                "k.pem",
                "-kdf",
                "x",
                "-keyform",
                "PEM",
                "-newkey",
                "rsa:2048",
            ],
            "openssl",
        ),
        (
            [
                "mlx-community/Qwen3-30B-A3B-4bit-DWQ",
                "v0.0.0-20241119163442-8f3a2c1d9e0b",
            ],
            "x",
        ),
        (["-Xlog:gc*:file=gc.log"], "java"),
        (["-o", "ControlPath=/tmp/cm-%r@%h:%p"], "ssh"),
        (
            [
                "Edit(//**/ccd-*h.501)",
                "openxml",
                "execvpe",
                "https://h/x?email=a@b.com",
            ],
            "x",
        ),
    ],
)
def test_switches_and_tool_arguments_the_fourth_review_found_are_kept(argv, program):
    assert shown(argv, program) == argv


def test_a_digest_pinned_image_keeps_its_name():
    assert shown(["ghcr.io/org/image@sha256:" + "9f2b8c1e" * 8], "docker") == [
        f"ghcr.io/org/image@{MASK}"
    ]


def test_shell_text_is_shown_as_written():
    assert shown(["-c", "require('node:http').createServer()"], "node") == [
        "-c",
        "require('node:http').createServer()",
    ]
    assert shown(["-c", "./run --name 'two words' && echo ok"], "bash") == [
        "-c",
        "./run --name 'two words' && echo ok",
    ]


def test_long_paths_are_judged_in_linear_time():
    import time

    value = "/".join(["Ab1x"] * 25000) + "/" + KEY
    start = time.monotonic()
    shown([value])
    assert time.monotonic() - start < 5


# ---- the fifth review: queries on plain paths, "#" inside URL secrets, globs, the generic -u rule -------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/cb?pass=sunshine", f"/cb?pass={MASK}"),
        ("/cb?sid=sunshine", f"/cb?sid={MASK}"),
        ("//h/x?pwd=sunshine", f"//h/x?pwd={MASK}"),
        ("h.example.com/x?sid=sunshine", f"h.example.com/x?sid={MASK}"),
        ("https://h/?next=/a?pass=sunshine", f"https://h/?next=/a?pass={MASK}"),
    ],
)
def test_queries_are_judged_by_name_on_plain_paths_and_nested(value, expected):
    assert shown([value]) == [expected]


@pytest.mark.parametrize(
    "value, secret",
    [
        ("https://api.example.com/v1/hook/0*%0N5Mq#4r-uGb2", "4r-uGb2"),
        ("https://api.example.com/v1/hook/=RUVZN7R&#vdsGC+", "vdsGC"),
    ],
)
def test_a_hash_inside_a_url_secret_does_not_cut_it(value, secret):
    assert secret not in shown([value])[0]


@pytest.mark.parametrize(
    "value",
    [
        "https://h/x?email=a@b.com&page=2#top",
        "https://github.com/org/repo/blob/main/x.py#L10",
    ],
)
def test_ordinary_queries_and_fragments_are_kept(value):
    assert shown([value]) == [value]


def test_a_star_between_password_symbols_is_not_a_glob():
    assert shown(["0*%0N5Mq"]) == [MASK]


@pytest.mark.parametrize(
    "argv, program",
    [
        (["-url=http://127.0.0.1:8080/x", "-upstream=host:8080"], "tool"),
        (["run", "--user", "app:app", "img"], "docker"),
        (["-u", "1000:1000", "img"], "docker"),
        (["-u", "root", "id"], "sudo"),
    ],
)
def test_the_generic_user_rule_leaves_users_groups_and_other_flags(argv, program):
    assert shown(argv, program) == argv


def test_the_generic_user_rule_masks_a_password():
    assert shown(["CURL_ARGS=-u admin:sunshine", "./x"], "env") == [
        f"CURL_ARGS=-u admin:{MASK}",
        "./x",
    ]


# ---- the sixth review: query names, "?" inside a path secret, the generic -u rule, globs -----------------------------


@pytest.mark.parametrize(
    "value",
    [
        f"https://h/x?{KEY}=1",
        f"/x?{KEY}=1",
        "https://h/x?lhqHg!a#^PU!9FP^$DCXWVy$q=1",
    ],
)
def test_a_secret_used_as_a_query_name_is_masked(value):
    assert KEY not in shown([value])[0] and "DCXWVy" not in shown([value])[0]


def test_a_question_mark_inside_a_path_secret_does_not_start_a_query():
    """_password_cut_at_question: a path piece with password symbols goes on past the "?"."""
    out = shown(["/srv/Xk9#mP2q?L7vN&4aB3cZ8w/x"])[0]
    assert "4aB3cZ8w" not in out and "L7vN" not in out and out.endswith("/x")


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["-u", "admin:admin", "https://x"], ["-u", f"admin:{MASK}", "https://x"]),
        (["--user", "postgres:postgres"], ["--user", f"postgres:{MASK}"]),
    ],
)
def test_a_password_equal_to_the_user_is_masked(argv, expected):
    assert shown(argv, "curl") == expected


@pytest.mark.parametrize(
    "argv, program",
    [
        (["-u", "daveey:staff"], "ps"),
        (["foo*bar.txt", "--glob=!node_modules"], "rg"),
    ],
)
def test_known_programs_keep_their_own_u_and_word_globs_are_kept(argv, program):
    assert shown(argv, program) == argv


# ---- the seventh review: -u for named programs, bracketed and OData query names, values cut at "&", "!" globs -------


@pytest.mark.parametrize(
    "argv, program",
    [
        (["-u", "admin:sunshine"], "mytool"),
        (["fetch.sh", "-u", "admin:sunshine"], "bash"),
        (["x.py", "-u", "admin:sunshine"], "python3"),
        (["cli.js", "--user", "admin:sunshine"], "node"),
        (["-c", "./fetch.sh -u admin:sunshine"], "bash"),
        (["exec", "db", "mysql", "-u", "root:sunshine"], "docker"),
    ],
)
def test_the_generic_user_rule_masks_a_password_for_any_program(argv, program):
    out = " ".join(shown(argv, program))
    assert "sunshine" not in out and f":{MASK}" in out


@pytest.mark.parametrize(
    "argv, program",
    [
        (["-u", "x.py"], "python3"),
        (["push", "-u", "origin", "main"], "git"),
        (["-u", "daveey:staff"], "pgrep"),
        (["exec", "-u", "app:app", "db", "sh"], "podman"),
    ],
)
def test_programs_whose_u_names_a_user_or_is_a_switch_are_kept(argv, program):
    assert shown(argv, program) == argv


@pytest.mark.parametrize(
    "value, expected",
    [
        ("https://h/?token[]=sunshine", f"https://h/?token[]={MASK}"),
        ("https://h/?auth[token]=sunshine", f"https://h/?auth[token]={MASK}"),
        ("https://h/?user[password]=sunshine&x=1", f"https://h/?user[password]={MASK}&x=1"),
        ("https://h/?%70assword=sunshine", f"https://h/?%70assword={MASK}"),
    ],
)
def test_bracketed_and_encoded_secret_query_names_mask_their_value(value, expected):
    assert shown([value]) == [expected]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("/cb?pass=sun&shine&x=1", f"/cb?pass={MASK}&{MASK}&x=1"),
        ("/cb?token=ab&correcthorsebattery", f"/cb?token={MASK}&{MASK}"),
        (f"https://h/x?q={KEY}&sunshine&page=2", f"https://h/x?q={MASK}&{MASK}&page=2"),
        (f"https://h/x?{KEY}&sunshine&page=2", f"https://h/x?{MASK}&{MASK}&page=2"),
        (f"https://h/x?{KEY}=1&sunshine&page=2", f"https://h/x?{MASK}={MASK}&{MASK}&page=2"),
    ],
)
def test_pieces_after_a_masked_query_piece_are_masked_until_a_well_formed_pair(value, expected):
    """The masking carry in _mask_query: a secret cut at "&" does not show its tail, and the next pair is judged."""
    assert shown([value]) == [expected]


@pytest.mark.parametrize(
    "value",
    [
        "https://graph.microsoft.com/v1.0/me/messages?$select=subject&$top=10",
        "https://h/Products?$filter=Price%20gt%2020",
        "https://h/?%24filter=a",
        "https://h/?q%5B%5D=1",
        "https://h/?q=hello%20world&next=%2Fa%2Fb",
    ],
)
def test_odata_and_percent_encoded_queries_are_kept(value):
    assert shown([value]) == [value]


def test_an_encoded_secret_query_value_is_masked():
    assert shown([f"https://h/?q=%21{KEY}"]) == [f"https://h/?q={MASK}"]


def test_a_leading_bang_is_a_glob_only_after_a_glob_flag():
    assert shown(["-g", "!*.lock", "--exclude=!vendor", "!sunshine"], "rg") == [
        "-g",
        "!*.lock",
        "--exclude=!vendor",
        MASK,
    ]


# ---- the eighth review: programs run inside containers, wrapper flag values, the wrapper handoff -------------------


@pytest.mark.parametrize(
    "argv, program",
    [
        (["run", "--rm", "img", "mytool", "-u", "admin:sunshine"], "docker"),
        (["run", "--rm", "myimg", "-u", "admin:sunshine"], "docker"),
        (["run", "img", "./t", "--user", "admin:sunshine"], "podman"),
        (["exec", "pod", "--", "./tool", "-u", "admin:sunshine"], "kubectl"),
        (["exec", "db", "mysql", "-u", "root:sunshine"], "docker"),
        (["mytool", "-u", "admin:sunshine"], "sudo"),
        (["-u", "NAME", "mytool", "-u", "admin:sunshine"], "env"),
        (["5m", "mytool", "-u", "admin:sunshine"], "timeout"),
    ],
)
def test_a_program_inside_a_container_or_wrapper_gets_the_generic_user_rule(argv, program):
    out = " ".join(shown(argv, program))
    assert "sunshine" not in out and f":{MASK}" in out


@pytest.mark.parametrize(
    "argv, program",
    [
        (["run", "-u", "1000:1000", "img"], "docker"),
        (["exec", "-u", "app:app", "db", "sh"], "docker"),
        (["compose", "exec", "-u", "app:app", "svc", "sh"], "docker"),
        (["-u", "admin", "ps", "-u", "daveey:staff"], "sudo"),
        (["X=1", "ps", "-u", "daveey:staff"], "env"),
        (["-n", "5", "ps", "-u", "daveey:staff"], "nice"),
    ],
)
def test_the_wrapped_program_keeps_its_own_u_after_wrapper_flag_values(argv, program):
    """The handoff to the wrapped program (env X=1 ps: ps's -u), past a wrapper's own flag values (sudo -u admin)."""
    assert shown(argv, program) == argv


def test_a_docker_login_password_is_masked():
    assert shown(["login", "--password", "sunshine", "reg"], "docker") == ["login", "--password", MASK, "reg"]


def test_encoded_non_ascii_query_values_are_kept():
    value = "https://h/?q=%E2%9C%93&x=caf%C3%A9"
    assert shown([value]) == [value]


# ---- the ninth review: containers behind a wrapper, wrapper long flags, durations, non-ASCII in a secret ----------


@pytest.mark.parametrize(
    "argv, program",
    [
        (["docker", "run", "--rm", "img", "tool", "-u", "admin:sunshine"], "sudo"),
        (["-u", "root", "docker", "run", "img", "tool", "-u", "admin:sunshine"], "sudo"),
        (["DOCKER_HOST=x", "docker", "run", "img", "tool", "-u", "admin:sunshine"], "env"),
        (["60", "docker", "exec", "ctr", "tool", "--user", "admin:sunshine"], "timeout"),
        (["podman", "run", "img", "./t", "-u", "admin:sunshine"], "nohup"),
        (["-c", "sudo docker run img tool -u admin:sunshine"], "bash"),
    ],
)
def test_a_container_started_through_a_wrapper_masks_the_inner_programs_password(argv, program):
    out = " ".join(shown(argv, program))
    assert "sunshine" not in out and f":{MASK}" in out


@pytest.mark.parametrize(
    "argv, program",
    [
        (["docker", "run", "-u", "app:app", "img"], "sudo"),
        (["--user", "admin", "ps", "-u", "daveey:staff"], "sudo"),
        (["5m", "ps", "-u", "daveey:staff"], "timeout"),
    ],
)
def test_wrapper_long_flag_values_and_durations_are_not_the_wrapped_program(argv, program):
    assert shown(argv, program) == argv


def test_non_ascii_inside_an_encoded_secret_does_not_cut_it_into_short_pieces():
    assert shown(["https://h/?q=Xk9%C3%A9mP2q%C3%BCL7vN4%C3%9FaB3c"]) == [f"https://h/?q={MASK}"]
