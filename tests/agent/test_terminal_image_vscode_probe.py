"""Real PTY replies during the probe, its drain, and after prompt ownership.

The terminal peer waits for protocol boundaries, not wall-clock guesses: a child
can be descheduled after flushing the query or before starting the drain. The
real select/clock still expire both production waits in the late-reply cases.
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")

_REPO = str(Path(__file__).resolve().parents[2])
_CHILD = textwrap.dedent("""
    import os, select, socket, sys, termios, tty
    from agent import terminal_image
    from hermes_cli.pt_input_extras import install_ignored_terminal_sequences
    from prompt_toolkit.input.vt100_parser import Vt100Parser
    from prompt_toolkit.keys import Keys
    peer = socket.socket(fileno=int(sys.argv[1]))
    peer.settimeout(15)
    def boundary(phase):
        peer.sendall(phase)
        assert peer.recv(1) == b"G"
    real_select = select.select
    waits = []
    def probe_select(readers, writers, errors, timeout):
        # No synthetic readiness or clock: only coordinate which real wait
        # receives the terminal's reply. The first wait must expire for D/P.
        boundary(b"Q" if not waits else b"D")
        ready = real_select(readers, writers, errors, timeout)
        waits.append(bool(ready[0]))
        return ready
    select.select = probe_select
    try:
        result = terminal_image.probe_vscode_images()
    finally:
        select.select = real_select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    boundary(b"P")  # reply/keys are queued before the prompt reader starts
    leaked = b""
    try:
        while select.select([fd], [], [], 0.3)[0]:
            leaked += os.read(fd, 256)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    install_ignored_terminal_sequences()
    presses = []
    parser = Vt100Parser(presses.append)
    parser.feed(leaked.decode())
    parser.flush()
    typed = "".join(p.data for p in presses if p.key is not Keys.Ignore)
    print("WAITS", waits, flush=True)
    print("RESULT", result, terminal_image.supports_iterm_images(),
          terminal_image.resolve_inline_mode("auto", overlays=True), "typed=%r" % typed, flush=True)
""")

_ON = b"\x1b[?1;0;4096S\x1b[?62;4;9;22c"
_OFF = b"\x1b[?1;2c"


def _run(reply: bytes, phase: bytes) -> str:
    import pty

    master, slave = pty.openpty()
    parent, child = socket.socketpair()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SSH_", "KITTY_", "ITERM_", "WEZTERM_", "KONSOLE_", "TMUX"))}
    env.update(TERM_PROGRAM="vscode", TERM="xterm-256color", PYTHONPATH=_REPO)
    proc = subprocess.Popen([sys.executable, "-c", _CHILD, str(child.fileno())],
                            stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                            pass_fds=(child.fileno(),), env=env, cwd=_REPO)
    os.close(slave)
    child.close()
    out = b""
    phases = []
    try:
        while True:
            ready = select.select([master, parent], [], [], 15)[0]
            assert ready, ("child stalled", phases, out)
            if parent in ready:
                event = parent.recv(1)
                if event:
                    phases.append(event)
                    if event == phase:
                        os.write(master, reply)
                    if event == b"P":
                        os.write(master, b"hi")
                    parent.sendall(b"G")
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError:  # EIO: the child closed its end
                    break
                if not data:
                    break
                out += data
        proc.wait(timeout=15)
        assert proc.returncode == 0 and proc.stderr is not None, proc.stderr and proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)
        os.close(master)
        parent.close()
        if proc.stderr is not None:
            proc.stderr.close()
    text = out.decode(errors="replace")
    expected_waits = {b"Q": [True], b"D": [False, True], b"P": [False, False]}
    assert f"WAITS {expected_waits[phase]}" in text, text
    assert phases == ([b"Q", b"P"] if phase == b"Q" else [b"Q", b"D", b"P"])
    return text[text.index("RESULT"):].splitlines()[0].strip() if "RESULT" in text else text


@pytest.mark.parametrize(
    ("reply", "phase", "expected"),
    [
        (_ON, b"Q", "RESULT True True iterm typed='hi'"),
        (_OFF, b"Q", "RESULT False False unicode typed='hi'"),
        (_ON, b"D", "RESULT True True iterm typed='hi'"),
        (_ON, b"P", "RESULT False False unicode typed='hi'"),
        (_OFF, b"P", "RESULT False False unicode typed='hi'"),
    ],
)
def test_vscode_probe_reads_its_reply_and_leaves_nothing_for_the_prompt(reply, phase, expected):
    assert _run(reply, phase) == expected
