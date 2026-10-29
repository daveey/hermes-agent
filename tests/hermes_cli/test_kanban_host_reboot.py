"""A host reboot is not a worker crash, and the card that rebooted the host is blocked, not retried. [daveey]

2026-10-29, hermes-hub: a worker's test subprocess ran ``sudo -n /sbin/shutdown -r now``. The sweep booked every
running card ``crashed`` ("pid N not alive") and retried the culprit, which rebooted the hub twice more.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_reboot as hr

OLD_BOOT = "1db9ba69-7f38-412b-b960-a4c38ce49d08"
NOW_BOOT = "3a7a1e43-81f7-4df4-846e-ed5f0a469284"

# Lines as journalctl -o short-iso prints them on hermes-hub (paths shortened to the test home below).
JOURNAL = """\
2026-10-29T12:30:01+00:00 hermes-hub sudo[1200]:   ubuntu : PWD=/srv ; USER=root ; COMMAND=/usr/bin/systemctl restart ssh
2026-10-29T12:37:00+00:00 hermes-hub sudo[1201]:   ubuntu : command not allowed ; PWD={ws_other} ; USER=root ; COMMAND=/usr/sbin/shutdown -k now
2026-10-29T12:38:06+00:00 hermes-hub sudo[1202]:   ubuntu : PWD={pwd} ; USER=root ; COMMAND=/sbin/shutdown -r now
2026-10-29T12:38:06+00:00 hermes-hub sudo[1202]: pam_unix(sudo:session): session opened for user root(uid=0) by (uid=1000)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: "")
    monkeypatch.setattr(hr, "current_boot", lambda: NOW_BOOT)
    monkeypatch.setattr(hr, "hermes_root", lambda: tmp_path)
    hr._journal_cache.clear()
    return tmp_path


def _journal(monkeypatch, text):
    seen = []

    def reader(boot):
        seen.append(boot)
        return text
    monkeypatch.setattr(hr, "_read_journal", reader)
    return seen


def _running(conn, title, *, assignee="default", boot=OLD_BOOT, workspace=None, pid=71001):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=?, worker_started_at=?, started_at=?, workspace_path=? WHERE id=?",
                     (pid, f"{boot}:22|66427451", int(time.time()) - 600, workspace, tid))
    return tid


def _run(conn, tid):
    return conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()


def test_parse_power_commands_keeps_only_executed_power_commands():
    found = hr.parse_power_commands(JOURNAL.format(ws_other="/w/b", pwd="/w/a/rbs/b"))
    assert [(f["pwd"], f["command"]) for f in found] == [("/w/a/rbs/b", "/sbin/shutdown -r now")]
    more = hr.parse_power_commands(
        "x sudo[1]:   ubuntu : TTY=pts/1 ; PWD=/p ; USER=root ; COMMAND=/usr/bin/systemctl --no-wall reboot\n"
        "x sudo[2]:   ubuntu : PWD=/p ; USER=root ; COMMAND=/usr/bin/systemctl status reboot.target\n")
    assert [f["command"] for f in more] == ["/usr/bin/systemctl --no-wall reboot"]


def test_boot_compare_needs_both_ids():
    assert hr.boot_of(f"{OLD_BOOT}:22|5") == OLD_BOOT
    assert hr.boot_of(12345) == "" and hr.boot_of(None) == "" and hr.boot_of("unverified") == ""
    assert hr.rebooted_since(f"{OLD_BOOT}:22|5", NOW_BOOT)
    assert not hr.rebooted_since(f"{NOW_BOOT}:22|5", NOW_BOOT)
    assert not hr.rebooted_since(f"{OLD_BOOT}:22|5", "")  # unreadable /proc: never guess a reboot


def test_culprit_in_its_workspace_is_blocked_and_bystander_requeued_uncounted(home, monkeypatch):
    ws_a, ws_b = home / "kanban/workspaces/t_a", home / "kanban/workspaces/t_b"
    seen = _journal(monkeypatch, JOURNAL.format(ws_other=ws_b, pwd=f"{ws_a}/rbs/b"))
    with kbc.connect() as conn:
        culprit = _running(conn, "re-verify mac-fixer", workspace=str(ws_a), pid=71001)
        bystander = _running(conn, "upkeep", workspace=str(ws_b), pid=71002)
        parent = kb.create_task(conn, title="downstream card", assignee="default")
        kb.link_tasks(conn, culprit, parent)  # a linked (child) card hears about it too
        kbd.detect_crashed_workers(conn)

        c, b = kb.get_task(conn, culprit), kb.get_task(conn, bystander)
        assert c.status == "triage" and c.block_kind == "capability"
        assert "THIS card rebooted the host" in c.last_failure_error
        assert "COMMAND=/sbin/shutdown -r now" in c.last_failure_error
        assert _run(conn, culprit)["outcome"] == "host_reboot"

        assert b.status == "ready" and b.consecutive_failures == 0
        assert _run(conn, bystander)["outcome"] == "host_reboot"
        assert "requeued without counting a failure" in b.last_failure_error

        comments = {t: [x.body for x in kb.list_comments(conn, t)] for t in (culprit, parent)}
        assert any("rebooted the host" in x for x in comments[culprit])
        assert any(culprit in x for x in comments[parent])
        kinds = [e.kind for e in kb.list_events(conn, bystander)]
        assert "host_reboot" in kinds and "crashed" not in kinds
    assert seen == [OLD_BOOT.replace("-", "")]  # one journal read for the spawn boot, cached


def test_profile_scratch_matches_only_the_profiles_sole_victim(home, monkeypatch):
    scratch = home / "profiles/bb-tuner/cache/scratch/mf/pre/st-822415ff"
    _journal(monkeypatch, JOURNAL.format(ws_other="/nowhere", pwd=scratch))
    with kbc.connect() as conn:
        sole = _running(conn, "bb recovery", assignee="bb-tuner", workspace=str(home / "ws/x"), pid=71011)
        other = _running(conn, "default card", assignee="default", workspace=str(home / "ws/y"), pid=71012)
        kbd.detect_crashed_workers(conn)
        assert kb.get_task(conn, sole).block_kind == "capability"
        assert kb.get_task(conn, other).status == "ready"


def test_profile_scratch_with_two_victims_of_that_profile_blocks_neither(home, monkeypatch):
    scratch = home / "profiles/bb-tuner/cache/scratch/mf"
    _journal(monkeypatch, JOURNAL.format(ws_other="/nowhere", pwd=scratch))
    with kbc.connect() as conn:
        a = _running(conn, "a", assignee="bb-tuner", pid=71021)
        b = _running(conn, "b", assignee="bb-tuner", pid=71022)
        kbd.detect_crashed_workers(conn)
        for t in (a, b):
            task = kb.get_task(conn, t)
            assert task.status == "ready" and task.consecutive_failures == 0


def test_plain_reboot_requeues_everything_uncounted(home, monkeypatch):
    _journal(monkeypatch, "")  # e.g. a console/EC2 reboot: no sudo power command in the journal
    with kbc.connect() as conn:
        tid = _running(conn, "job", workspace=str(home / "ws/z"))
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.consecutive_failures == 0
        assert _run(conn, tid)["outcome"] == "host_reboot"
        assert kbd.detect_crashed_workers._last_host_rebooted == [tid]  # type: ignore[attr-defined]


def test_same_boot_death_is_still_a_crash(home, monkeypatch):
    seen = _journal(monkeypatch, JOURNAL.format(ws_other="/x", pwd="/y"))
    with kbc.connect() as conn:
        tid = _running(conn, "job", boot=NOW_BOOT)
        kbd.detect_crashed_workers(conn)
        assert _run(conn, tid)["outcome"] == "crashed"
        assert kb.get_task(conn, tid).consecutive_failures == 1
    assert seen == []  # no journal read without a reboot
