"""Secrets in a process's argv are masked before ``hermes doctor`` shows the process (holder lists are
pasted into issues); the identifiers a person needs to tell processes apart are kept."""

import pytest

from hermes_state_argv_secrets import MASK, mask_argv_secrets

KEY = "q8ZpL2vXk9RtYwB4mN7cQ1sD5fH3jK6g"


@pytest.mark.parametrize(
    "argv, expected",
    [
        # bare random tokens, any case
        (["worker.py", KEY], ["worker.py", MASK]),
        (["worker.py", "q8zpl2vxk9rtywb4mn7cq1sd5fh3jk6g"], ["worker.py", MASK]),
        # secret-named flags the shared redactor does not list, both spellings, short values too
        (
            ["tool.py", "--webhook-secret", "Zx9Lk2PqRt7V"],
            ["tool.py", "--webhook-secret", MASK],
        ),
        (["tool.py", "--signing-key=hunter2"], ["tool.py", f"--signing-key={MASK}"]),
        (["tool.py", "--auth", "abc"], ["tool.py", "--auth", MASK]),
        (["tool.py", "--DB-PASSWORD", "pw"], ["tool.py", "--DB-PASSWORD", MASK]),
        # base64 secrets contain "/": still masked after a secret-named flag (only a path's START keeps it)
        (
            ["--aws-secret-access-key", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"],
            ["--aws-secret-access-key", MASK],
        ),
        (["--webhook-secret=Zm9vYmFyYmF6/cXV4cXV1eA=="], [f"--webhook-secret={MASK}"]),
        # a suffix after the secret word keeps the flag secret (fail closed); short, "/" and hex values too
        (
            [
                "--client-secret-value",
                "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "--token-b64",
                "Zm9vYmFy/YmF6cXV4",
            ],
            ["--client-secret-value", MASK, "--token-b64", MASK],
        ),
        (
            [
                "--api-key-value",
                "hunter2hunter2",
                "--secret-key-base",
                "0a1b2c3d4e5f60718293a4b5c6d7e8f9",
            ],
            ["--api-key-value", MASK, "--secret-key-base", MASK],
        ),
        (
            [
                "--auth-header",
                "Bearer.Xy9kq2LmZp4Rt7",
                "--private-key-pem=abc",
                "--apikey",
                "x",
            ],
            ["--auth-header", MASK, f"--private-key-pem={MASK}", "--apikey", MASK],
        ),
        # base64 padding is not an assignment: a padded random token is masked whole
        (
            ["Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==", "Xy9kq2LmZp4Rt7Wb3NcQ8abc="],
            [MASK, MASK],
        ),
        # NAME=value assignments: the value is judged under its name
        (
            ["API_TOKEN=abc123", "Config=" + KEY],
            [f"API_TOKEN={MASK}", f"Config={MASK}"],
        ),
        # an UPPER_SNAKE env name stays readable; a secret-named one still hides its value
        (
            ["HERMES_RELAY_ID_2024=abc", "OPENROUTER_API_KEY_2024=sk12"],
            ["HERMES_RELAY_ID_2024=abc", f"OPENROUTER_API_KEY_2024={MASK}"],
        ),
        # plural secret lists are secrets too
        (
            ["--github-tokens", "Qx7Lm2Zp/w", "--bot-tokens=abc"],
            ["--github-tokens", MASK, f"--bot-tokens={MASK}"],
        ),
        # a numeric value after a secret-named flag is the secret
        (
            ["--webhook-secret", "12345678901234567890", "--signing-key=4242424242"],
            ["--webhook-secret", MASK, f"--signing-key={MASK}"],
        ),
        # a lowercase prefixed random token is not a Hermes id
        (["abc-9f8e7d6c5b4a39281706f5e4z"], [MASK]),
        # a bare token as a --flag=value value
        (["tool.py", f"--config={KEY}"], ["tool.py", f"--config={MASK}"]),
    ],
)
def test_secrets_are_masked(argv, expected):
    assert mask_argv_secrets(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["-m", "hermes_cli.main", "gateway", "run", "--external-supervisor"],
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
        ["chat", "--resume", "20261119_163442_8f3a2c"],
        ["kanban", "run", "t_01a1ecf2c3d6367b"],
        [
            "checkout",
            "0362a20c0e39",
        ],  # a short commit; a full 40-hex digest is masked (hex keys look the same)
        ["--resume=8ec9ab93-b372-4c03-ac51-7abb31174eb9", "--model=claude-fable-5-2"],
        ["chat", "--model", "anthropic/claude-opus-4.7"],
        ["--password-file", "/opt/vnc/chrome-agent-vnc.pass"],
        ["--auth", "/opt/egress/egress-socks.auth"],
        [
            "--variations-seed-version=20261117-170111.083000-production",
            "--num-raster-threads=4",
            "--launch-time-ticks=1676849682180",
            "--trace-process-track-uuid=3190710511816001666",
        ],
        ["/home/ubuntu/.local/share/hermes-mail/hermes_hub.py", "_runtime-serve"],
        ["--author", "daveey", "--keyboard", "us"],
        [
            "--max-tokens",
            "4096",
            "--min-tokens=16",
            "--cache-read-tokens",
            "2048",
            "--token-env",
            "OPENAI_API_KEY",
        ],
        [
            "--idempotency-key",
            "followup:t_01a1ecf2c3d6367b:notify",
            "--step-key",
            "build",
            "--passkey-browser",
            "chrome",
        ],
        ["--model", "Qwen3-Coder-480B-A35B-Instruct"],
        ["--api-key-timeout", "30"],
        ["profile", "create", "--isolated-auth", "work", "--no-credentials", "scratch"],
        [
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "NumberOfPasswordPrompts=0",
            "HERMES_PROFILE=coder",
        ],
        # settings about a secret, not the secret: deliberate (an id, a name, an env var, a store, a jar path)
        [
            "--access-key-id",
            "deploy-bot",
            "--secret-name",
            "prod/db",
            "--api-key-env",
            "OPENAI_API_KEY",
        ],
        [
            "--keyring-backend",
            "macos",
            "--auth-mode",
            "oauth",
            "--cookie-jar",
            "/opt/jar/cookies.txt",
        ],
        ["--key-file", "C:\\Users\\x\\key.pem"],
        ["--token-file", "./token.txt"],
    ],
)
def test_identifiers_are_kept(argv):
    assert mask_argv_secrets(argv) == argv


def test_a_flag_value_that_is_itself_a_flag_is_not_swallowed():
    assert mask_argv_secrets(["--token", "--verbose", "x"]) == [
        "--token",
        "--verbose",
        "x",
    ]


# ---- the remaining shapes: short tokens, letter-only tokens, program short flags, userinfo, "-"-leading values,
# command-valued secret flags, UPPER_SNAKE look-alikes --------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, program, expected",
    [
        # 16-19 character random tokens
        (["worker.py", "ab12cd34ef56gh78"], "python3", ["worker.py", MASK]),
        # letters-only random tokens: case flipping or vowel-poor
        (["worker.py", "XyQwErTyUiOpAsDfGhJk"], "python3", ["worker.py", MASK]),
        (["worker.py", "qzxkvbnmplkjhgfdrtwz"], "python3", ["worker.py", MASK]),
        # programs' own short password flags
        (
            ["-u", "root", "-phunter2", "db"],
            "/usr/bin/mysql",
            ["-u", "root", f"-p{MASK}", "db"],
        ),
        (["-p", "hunter2", "ssh", "host"], "sshpass", ["-p", MASK, "ssh", "host"]),
        (["-a", "hunter2", "ping"], "redis-cli", ["-a", MASK, "ping"]),
        (
            ["enc", "-k", "hunter2", "-pass", "pass:hunter2"],
            "openssl",
            ["enc", "-k", MASK, "-pass", MASK],
        ),
        (
            ["-u", "alice:hunter2", "https://x"],
            "curl",
            ["-u", f"alice:{MASK}", "https://x"],
        ),
        (["-ualice:hunter2"], "curl", [f"-ualice:{MASK}"]),
        (["-U", "alice%hunter2"], "smbclient", ["-U", f"alice%{MASK}"]),
        # user:pass@host without a scheme
        (
            ["push", "alice:hunter2@git.example.com"],
            "git",
            ["push", f"alice:{MASK}@git.example.com"],
        ),
        # a secret that starts with "-" after a secret-named flag
        (["--password", "-Xy9kq2LmZ"], "tool", ["--password", MASK]),
        # a command that prints the secret: only its program is shown
        (
            ["--password-cmd", "echo hunter2"],
            "tool",
            ["--password-cmd", f"echo {MASK}"],
        ),
        (
            ["--api-key-helper=pass show openai"],
            "tool",
            [f"--api-key-helper=pass {MASK}"],
        ),
        # random segments posing as an UPPER_SNAKE name
        (["AB12_CD34_EF56_GH78_IJ90_KL12=x"], "env", [MASK]),
    ],
)
def test_remaining_secret_shapes_are_masked(argv, program, expected):
    assert mask_argv_secrets(argv, program=program) == expected


@pytest.mark.parametrize(
    "argv, program",
    [
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
        (
            ["-p", "coder", "gateway", "run"],
            "hermes",
        ),  # -p is the profile for most programs: never masked
        (
            ["-u", "alice", "https://x"],
            "curl",
        ),  # a bare user: curl prompts for the password
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
                "internationalization",
                "ServerAliveCountMax",
                "NumberOfPasswordPrompts",
                "followup:t_01a1:notify",
            ],
            "x",
        ),
        (
            [
                "--annotation=platform=macosarm64",
                "--annotation=_version=5.11492.5",
                "--password-cmd",
                "pass",
            ],
            "x",
        ),
        (
            [
                "HERMES_RELAY_ID_2024=abc",
                "OPENROUTER_BASE_URL=https://openrouter.ai/api/v1",
            ],
            "env",
        ),
        # repeated letters are padding or placeholders, not random secrets
        (["--profile", "p" * 120, "zzzzzzzzzzzzzzzzzzzzzzzz"], "x"),
    ],
)
def test_identifiers_survive_the_stricter_rules(argv, program):
    assert mask_argv_secrets(argv, program=program) == argv


# ---- fail closed: generated secrets of every common shape are masked; identifiers people read are kept -----------

import base64
import random
import secrets as _secrets
import string

_RNG = random.Random(20261119)


def _shape(kind: str, length: int) -> str:
    pick = lambda alphabet: "".join(_RNG.choice(alphabet) for _ in range(length))
    return {
        "alnum": lambda: pick(string.ascii_letters + string.digits),
        "lower_digits": lambda: pick(string.ascii_lowercase + string.digits),
        "upper_digits": lambda: pick(string.ascii_uppercase + string.digits),
        "base32": lambda: pick(string.ascii_uppercase + "234567"),
        "hex": lambda: pick("0123456789abcdef"),
        "mixed_letters": lambda: pick(string.ascii_letters),
        "symbols": lambda: pick(string.ascii_letters + string.digits + "!@#$%^&*"),
        "base64": lambda: base64.b64encode(_RNG.randbytes(length)).decode()[
            : length + 4
        ],
        "urlsafe": lambda: (
            base64.urlsafe_b64encode(_RNG.randbytes(length)).decode().rstrip("=")
        ),
        "lower": lambda: pick(string.ascii_lowercase),
        "upper": lambda: pick(string.ascii_uppercase),
    }[kind]()


# Leak rate allowed per secret length (a random string sometimes reads as a name; the shorter, the likelier).
_LEAK_CEILING = {16: 0.05, 20: 0.03, 24: 0.02, 32: 0.006, 40: 0.006}


@pytest.mark.parametrize(
    "kind",
    [
        "alnum",
        "lower_digits",
        "upper_digits",
        "base32",
        "hex",
        "mixed_letters",
        "symbols",
        "base64",
        "urlsafe",
        "lower",
        "upper",
    ],
)
@pytest.mark.parametrize("length", sorted(_LEAK_CEILING))
def test_generated_secrets_are_masked_at_the_documented_rate(kind, length):
    leaked = 0
    for _ in range(1000):
        secret = _shape(kind, length)
        shown = " ".join(mask_argv_secrets(["worker.py", secret], program="python3"))
        leaked += secret in shown or secret[-8:] in shown
    assert leaked / 1000 <= _LEAK_CEILING[length], (
        f"{kind}/{length}: {leaked / 10:.1f}% shown"
    )


@pytest.mark.parametrize(
    "make, ceiling",
    [
        (lambda: _secrets.token_urlsafe(16), _LEAK_CEILING[20]),  # 22 characters
        (lambda: _secrets.token_hex(16), 0.0),  # 32 hex: always masked
        (lambda: _secrets.token_urlsafe(32), _LEAK_CEILING[40]),  # 43 characters
    ],
)
def test_python_secrets_module_tokens_are_masked_at_the_documented_rate(make, ceiling):
    tokens = [make() for _ in range(1000)]
    leaked = sum(
        token in " ".join(mask_argv_secrets(["--opt", "x", token], program="tool"))
        for token in tokens
    )
    assert leaked / len(tokens) <= ceiling


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
        # ids people match on
        "proc_467899b1fa1b",
        "i-0abc123def4567890",
        "t_01a1ecf2c3d6367b",
        "20261119_163442_8f3a2c",
        "8ec9ab93-b372-4c03-ac51-7abb31174eb9",
        "0362a20c0e39",
        "GIT_CONFIG_KEY_17",
        "HERMES_RELAY_ID_2024",
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
        # words and camelCase
        "internationalization",
        "isHermesHubPickerUrl",
        "WebKitWebContentProcess",
        "ServerAliveCountMax",
        "testSessionOwnerByRuntimeId",
        "production",
        "gateway",
        "serve",
        "dashboard",
        "_runtime-serve",
    ],
)
def test_identifiers_people_read_are_kept(token):
    assert mask_argv_secrets([token], program="tool") == [token]


@pytest.mark.parametrize(
    "argv, program, expected",
    [
        # wrappers keep the inner program's own password flags
        (["mysql", "-uroot", "-phunter2"], "sudo", ["mysql", "-uroot", f"-p{MASK}"]),
        (
            ["exec", "-it", "db", "mysql", "-phunter2"],
            "docker",
            ["exec", "-it", "db", "mysql", f"-p{MASK}"],
        ),
        (["-c", "mysql -phunter2 shop"], "bash", ["-c", f"mysql -p{MASK} shop"]),
        # sshpass hides its password, not the wrapped ssh's port
        (
            ["-p", "hunter2", "ssh", "-p", "2222", "host"],
            "sshpass",
            ["-p", MASK, "ssh", "-p", "2222", "host"],
        ),
        # curl user:pass in every spelling, a key as the user, a random token as the user
        (
            ["--user=alice:hunter2", "--proxy-user=bob:pw123"],
            "curl",
            [f"--user=alice:{MASK}", f"--proxy-user=bob:{MASK}"],
        ),
        (["-su", "alice:hunter2"], "curl", ["-su", f"alice:{MASK}"]),
        (["-u", "sk9Lm2QxPz7Rt4Wb:"], "curl", ["-u", f"{MASK}:{MASK}"]),
        (["-u", "Xk9mP2qL7vN4aB3cZ8wRtY5u"], "curl", ["-u", MASK]),
        (["--user=alice%hunter2"], "smbclient", [f"--user=alice%{MASK}"]),
        (
            ["-u", "alice,hunter2", "sftp://h"],
            "lftp",
            ["-u", f"alice,{MASK}", "sftp://h"],
        ),
        # more programs' password flags
        (["-p", "hunter2"], "mongosh", ["-p", MASK]),
        (["-w", "hunter2"], "ldapsearch", ["-w", MASK]),
        (["-P", "hunter2"], "sqlcmd", ["-P", MASK]),
        (["-storepass", "hunter2"], "keytool", ["-storepass", MASK]),
        (["-phunter2", "x.7z"], "7z", [f"-p{MASK}", "x.7z"]),
        (["-P", "hunter2"], "zip", ["-P", MASK]),
        (["-N", "hunter2"], "ssh-keygen", ["-N", MASK]),
        # secrets that start with "-" after a secret flag
        (["--password", "-abcdefgh"], "tool", ["--password", MASK]),
        (["--token", "-12345678"], "tool", ["--token", MASK]),
        (["--password", "--Xy9kq2LmZ"], "tool", ["--password", MASK]),
        # a secret header, a webhook URL, a secret inside a command
        (["-H", "X-Api-Key: hunter2"], "curl", ["-H", f"X-Api-Key: {MASK}"]),
        (["-H", "Authorization: Bearer abc"], "curl", ["-H", f"Authorization: {MASK}"]),
        (
            ["--webhook-url", "https://hooks.slack.com/services/T0/B0/x"],
            "tool",
            ["--webhook-url", MASK],
        ),
        (["-c", "credential.helper=!f() { echo password=hunter2; }; f"], "git", None),
        # symbol passwords and a path-shaped secret after a secret flag
        (["P@ssw0rd!2024xyz"], "tool", [MASK]),
        (["--token=/K7MDENG+bPxRfiCYEXAMPLEKEY=="], "tool", [f"--token={MASK}"]),
        # nested values are judged under their own names
        (
            ["--env=API_TOKEN=abc", "--annotation=password=x"],
            "tool",
            [f"--env=API_TOKEN={MASK}", f"--annotation=password={MASK}"],
        ),
    ],
)
def test_named_and_program_secrets_are_masked(argv, program, expected):
    shown = mask_argv_secrets(argv, program=program)
    if expected is None:
        assert "hunter2" not in " ".join(shown), shown
    else:
        assert shown == expected


@pytest.mark.parametrize(
    "argv, program",
    [
        (["-p", "coder", "gateway", "run"], "hermes"),
        (
            [
                "chat",
                "--model",
                "gpt-5.6-sol-900k",
                "--session-key",
                "20261119_163442_8f3a2c",
            ],
            "hermes",
        ),
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
        (["auth", "login", "--with-token", "github.com"], "gh"),
        (["--auth-server-whitelist=*.corp.example.com"], "Electron"),
        (["-c", "user.signingkey=ABCDEF0123456789"], "git"),
        (["-k", "testSessionOwnerByRuntimeId"], "pytest"),
        (["start-session", "--target", "i-0abc123def4567890"], "aws"),
        (["process", "kill", "proc_467899b1fa1b"], "hermes"),
    ],
)
def test_hermes_and_tool_arguments_are_kept(argv, program):
    assert mask_argv_secrets(argv, program=program) == argv


def test_deep_nesting_is_masked_not_a_crash():
    assert (
        mask_argv_secrets(["a=" * 1000 + "x"], program="x") == [f"a={MASK}"]
        or MASK in mask_argv_secrets(["a=" * 1000 + "x"], program="x")[0]
    )
