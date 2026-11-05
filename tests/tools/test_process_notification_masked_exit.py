"""Background completion notices must not call a masked failure "completed normally".

Incident agent-1e03ea6be2: `hermes chat ... > log 2>&1; echo EXIT=$?` exited 0 (echo's
status) while printing EXIT=1, and the notice said "completed normally (exit code 0)".
"""

from tools.process_registry_notifications import (
    format_process_notification,
    process_completion_display_text,
)

INCIDENT_CMD = ('hermes chat --resume 20261103_abc -q "review PR 818 at exact head" '
                '> /tmp/review-818.log 2>&1; echo EXIT=$?')


def _evt(command=INCIDENT_CMD, output="EXIT=1\n", **extra):
    return {"type": "completion", "session_id": "proc_e81d0d005f45", "command": command,
            "exit_code": 0, "output": output, **extra}


def _fmt(evt) -> str:
    text = format_process_notification(evt)
    assert text is not None
    return text


def test_incident_notice_warns_instead_of_completed_normally():
    text = _fmt(_evt())
    assert "completed normally" not in text
    assert "exited 0 but output shows failure" in text
    assert "status 1" in text
    assert "EXIT=1" in text  # the output itself is still delivered


def test_incident_display_title_not_finished():
    assert "Finished" not in process_completion_display_text([_evt()])


def test_status_zero_still_completed_normally():
    text = _fmt(_evt(output="EXIT=0\n"))
    assert "completed normally" in text
    assert "output shows failure" not in text
    assert "Finished" in process_completion_display_text([_evt(output="EXIT=0\n")])


def test_readonly_grep_not_flagged():
    text = _fmt(_evt(command="grep EXIT= /tmp/review-818.log",
                                            output="EXIT=1\n"))
    assert "completed normally" in text


def test_long_subagent_output_checked_before_trim():
    # The failure shape sits only in the head that the subagent trim cuts away.
    text = _fmt(_evt(command="cargo build 2>&1 | tail -400",
                                            output="error: could not compile `x`\n" + "y" * 5000,
                                            owner_task_id="sa-1-abc"))
    assert "output trimmed" in text
    assert "output shows failure" in text


def test_long_output_status_line_at_end():
    out = "y" * 9000 + "\nEXIT=2\n"
    text = _fmt(_evt(command="make; echo EXIT=$?", output=out))
    assert "status 2" in text
    assert "completed normally" not in text


def test_nonzero_exit_unchanged():
    text = _fmt(_evt(exit_code=1, output="EXIT=1\n"))
    assert "exited (exit code 1)" in text


def test_killed_reason_unchanged():
    text = _fmt(_evt(completion_reason="killed", exit_code=0))
    assert "terminated by" in text
    assert "Warning:" not in text


def test_lost_reason_not_reinterpreted():
    text = _fmt(_evt(completion_reason="lost", exit_code=0))
    assert "marked lost" in text
    assert "Warning:" not in text
