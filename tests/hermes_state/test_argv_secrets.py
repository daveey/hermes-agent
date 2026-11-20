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
            "/Users/x/.hermes/desktop-ssh/5b38f00bfab44cbcc8f1a2b3c4d5e6f7/token",
        ],
        ["chat", "--resume", "20261119_163442_8f3a2c"],
        ["kanban", "run", "t_01a1ecf2c3d6367b"],
        ["checkout", "0362a20c0e39651291280105e81f7c42f4f435f7"],
        ["--resume=8ec9ab93-b372-4c03-ac51-7abb31174eb9", "--model=claude-fable-5-2"],
        ["chat", "--model", "anthropic/claude-opus-4.7"],
        ["--password-file", "/home/ubuntu/.hermes/chrome-agent-vnc.pass"],
        ["--auth", "~/.hermes/egress-socks.auth"],
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
            "~/.hermes/cookies.txt",
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
