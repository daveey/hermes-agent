"""Safety contract for retry-wait update parking (WIP: must fail until wired).

These tests isolate marker projection and continuation kickoff, not a real
provider/process recovery proof. The complete sleep/resume proof is also required.
"""
import threading
import time

import pytest

from tui_gateway import server, turn_marker


@pytest.mark.parametrize("seam", ["projection", "continuation_kickoff"])
def test_retry_deadline_survives_pause_and_continuation(tmp_path, monkeypatch, seam):
    key = "retry-deadline-session"
    prompt = "Finish the original job"
    deadline = time.time() + 600.0
    assert turn_marker.record_turn_start(tmp_path, key, prompt)

    def seed(entries):
        entries[key]["provider_retry_not_before"] = deadline
        return entries

    # Arrange future producer output using the real flocked store, then exercise
    # the unchanged production readers. No provider pool independently benches it.
    assert turn_marker._update(tmp_path, key, seed, "fixture retry deadline")
    assert turn_marker.mark_turn_paused(tmp_path, key, "core_update_pause")
    if seam == "projection":
        marker = turn_marker.read_turn_marker(tmp_path, key)
        assert marker.get("provider_retry_not_before") == deadline
        return

    # Isolate kickoff from projection so the second failure is not masked by
    # the first. The scheduler must eventually transfer this field on both
    # cold and in-place resume; a new user turn must not receive it.
    session = {
        "session_key": key, "profile_home": str(tmp_path),
        "history_lock": threading.Lock(), "_auto_continue_prompt": prompt,
        "_auto_continue_attempt": 0, "_auto_continue_pauses": 1,
        "_auto_continue_provider_retry_not_before": deadline,
    }
    monkeypatch.setattr(server, "_session_home", lambda _: tmp_path)
    assert server._record_turn_marker(session, "Continue") == key
    # Inspect raw storage to avoid conflating kickoff with reader projection.
    marker = turn_marker._load(turn_marker._marker_path(tmp_path))[key]
    assert marker.get("provider_retry_not_before") == deadline
    assert "_auto_continue_provider_retry_not_before" not in session
