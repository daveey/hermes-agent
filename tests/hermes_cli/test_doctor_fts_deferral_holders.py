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

    proc = subprocess.Popen(["sh", "-c", "read x", "sh"], stdin=subprocess.PIPE)
    try:
        assert "read x" in describe_holder_pid(proc.pid)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
