"""``hermes doctor`` advice for a blocked FTS rebuild names the processes holding state.db now.

The stored deferral record's ``holder_pids`` is its last writer's view, which leaves the writer out. When a CLI
process wrote it, the list was the gateway, and doctor said to stop "the listed process(es)": the gateway, whose
own retry is what rebuilds.
"""

import sqlite3
import subprocess
import sys

import pytest

from hermes_cli.doctor_state import _live_state_db_holders, _render_state_db_stats

GATEWAY = (100, "PID 100 (hermes gateway run)", True)
WATCHER = (200, "PID 200 (python3 hermes_hub.py watch)", False)


def _deferral_row(deferral, live):
    rows = [
        r
        for r in _render_state_db_stats(
            {"fts_rebuild_deferral": deferral}, live_holders=live
        )
        if r[0] == "warn"
    ]
    assert len(rows) == 1, rows
    return rows[0][1] + " " + rows[0][2]


@pytest.mark.parametrize("futile", [True, False])
def test_advice_stops_only_the_non_gateway_holders(futile):
    # The record was written by the watcher's CLI child, so it names only the gateway.
    text = _deferral_row(
        {
            "futile": futile,
            "holder_pids": [100],
            "holders_attempts": 12,
            "attempts": 40,
        },
        [GATEWAY, WATCHER],
    )
    assert "stop ONLY PID 200 (python3 hermes_hub.py watch)" in text
    assert "keep the host gateway PID 100 (hermes gateway run) running" in text
    assert "stop ONLY PID 100" not in text


def test_gateway_alone_is_never_named_as_the_process_to_stop():
    text = _deferral_row(
        {"futile": True, "holder_pids": [100], "holders_attempts": 12}, [GATEWAY]
    )
    assert "only the host gateway PID 100 (hermes gateway run) holds it now" in text
    assert "stop ONLY" not in text


@pytest.mark.platforms("linux", "macos")
def test_live_holders_lists_another_process_holding_the_db(tmp_path):
    db_path = tmp_path / "state.db"
    sqlite3.connect(str(db_path)).execute("CREATE TABLE t (x)").connection.close()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('select * from t');"
            " print('ready', flush=True); sys.stdin.read()",
            str(db_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        live = _live_state_db_holders(db_path)
        assert live is not None
        assert [(pid, is_gateway) for pid, _desc, is_gateway in live] == [
            (holder.pid, False)
        ]
        assert live[0][1].startswith(f"PID {holder.pid} (")
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


@pytest.mark.platforms("linux", "macos")
def test_holder_description_skips_inline_bootstrap_code(tmp_path):
    from hermes_state_holders import describe_holder_pid

    code = "import sys; print('ready', flush=True); sys.stdin.read()  # " + "x" * 200
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "gateway", "run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        desc = describe_holder_pid(proc.pid)
        assert desc.endswith("-c <inline code> gateway run)"), desc
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_other_holders_without_a_gateway_get_coherent_advice():
    text = _deferral_row(
        {"futile": False, "holder_pids": [200], "attempts": 4}, [WATCHER]
    )
    assert (
        "stop PID 200 (python3 hermes_hub.py watch); the next Hermes process to open state.db"
        in text
    )
    assert "gateway's its" not in text and "stop ONLY" not in text


def test_two_gateways_are_told_they_block_each_other():
    other_gateway = (101, "PID 101 (hermes gateway run)", True)
    text = _deferral_row(
        {"futile": True, "holder_pids": [101], "holders_attempts": 12},
        [GATEWAY, other_gateway],
    )
    assert "several gateways hold it" in text and "keep one and stop the rest" in text
    assert "its next retry rebuilds" not in text


@pytest.mark.platforms("linux", "macos")
def test_holder_description_keeps_a_non_python_dash_c():
    from hermes_state_holders import describe_holder_pid

    # Popen's exec-error pipe can close before the child's argv is observable.
    # Wait for the shell itself, as the Python holder fixtures above do.
    proc = subprocess.Popen(
        ["sh", "-c", "printf 'ready\\n'; read x", "sh"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        assert "read x" in describe_holder_pid(proc.pid)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()


@pytest.mark.platforms("linux", "macos")
def test_non_python_holder_waits_for_child_readiness(monkeypatch):
    """Model argv being unavailable until the parent observes the child's ready message."""
    import hermes_state_holders as holders

    popen = subprocess.Popen
    read_argv = holders._read_proc_argv
    cmdline = holders.psutil.Process.cmdline
    children = []
    ready = False

    def spawn(*args, **kwargs):
        proc = popen(*args, **kwargs)
        children.append(proc)
        if proc.stdout is not None:
            readline = proc.stdout.readline

            def observe_ready():
                nonlocal ready
                line = readline()
                ready = line.strip() == "ready"
                return line

            monkeypatch.setattr(proc.stdout, "readline", observe_ready)
        return proc

    def argv_when_ready(pid):
        if children and pid == children[0].pid and not ready:
            return None
        return read_argv(pid)

    def cmdline_when_ready(proc):
        if children and proc.pid == children[0].pid and not ready:
            return []
        return cmdline(proc)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(holders, "_read_proc_argv", argv_when_ready)
    monkeypatch.setattr(holders.psutil.Process, "cmdline", cmdline_when_ready)
    try:
        test_holder_description_keeps_a_non_python_dash_c()
        assert ready
    finally:
        # The invoked test must retain cleanup even when its semantic assertion fails.
        assert len(children) == 1
        assert children[0].poll() is not None


@pytest.mark.platforms("linux", "macos")
def test_doctor_lists_holders_with_their_whole_command_line(tmp_path):
    """Doctor's holder list is what a person reads to decide what to stop: no 80-character cut there,
    while the default description (log and error lines) keeps it."""
    from hermes_state_holders import describe_holder_pid

    db_path = tmp_path / "state.db"
    sqlite3.connect(str(db_path)).execute("CREATE TABLE t (x)").connection.close()
    tail = "--profile " + "p" * 120 + " --the-end"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('select * from t');"
            " print('ready', flush=True); sys.stdin.read()",
            str(db_path),
            *tail.split(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "ready"
        ((pid, desc, _is_gateway),) = _live_state_db_holders(db_path)
        assert pid == holder.pid and desc.endswith(f"state.db {tail})"), desc
        short = describe_holder_pid(holder.pid)
        assert len(short) < len(desc) and "--the-end" not in short
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


@pytest.mark.platforms("linux", "macos")
def test_holder_descriptions_never_show_secrets_from_argv(tmp_path):
    """Doctor output is pasted into issues: API keys, URL credentials, secret-flag values and bare tokens in argv are redacted,
    whole or cut."""
    from hermes_state_holders import describe_holder_pid

    key = "sk-ant-api03-" + "A1b2C3d4" * 6
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('ready', flush=True); sys.stdin.read()",
            "--base-url",
            "http://alice:hunter2pw@10.0.0.1:8000/v1",
            "--api-key",
            key,
            "--webhook-secret",
            "Zx9Lk2PqRt7VbNm4Wc8Ys1Df",
            "q8ZpL2vXk9RtYwB4mN7cQ1sD5fH3jK6g",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        for desc in (
            describe_holder_pid(proc.pid, limit=None),
            describe_holder_pid(proc.pid),
        ):
            assert (
                key[:16] not in desc and "hunter2pw" not in desc and "alice" not in desc
            ), desc
            # unknown secret-named flag and a bare random token: masked whole, not head/tail
            assert "Zx9Lk2" not in desc and "q8ZpL2" not in desc, desc
        assert "10.0.0.1:8000/v1" in describe_holder_pid(proc.pid, limit=None)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
