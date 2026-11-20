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
