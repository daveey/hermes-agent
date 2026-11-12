"""The Desktop app named in a card body is a symptom, not a need for the machine with the app. [daveey]

A core fix whose body began "Desktop chat turns are held at their start and end by plugin hooks" was kept on
a full hub as ``local marker: mentions 'desktop chat'`` although it was a repo change landed by push.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_placement as kp
from tests.hermes_cli import test_kanban_placement as helpers

mac = helpers.mac
decide = helpers.decide

_TITLE = "Core: replica task reads fetch 12.5k rows one sqlite step at a time and stall turns 20-90 s"
_BODY = ("Desktop chat turns are held at their start and end by plugin hooks that wait on a lock.\n"
         "The serve/dashboard process that hosts Desktop chats never sets the switch interval.\n"
         "Read the rows in a bounded number of sqlite steps and push the branch.")

_SYMPTOM_BODIES = [
    _BODY,
    "The Desktop backend pid 2884099 held deleted -wal files for all 5 boards. Fix the reopen path.",
    "David reported that Hermes Desktop loads as if getting started again. Fix the default-model write.",
    "Its completion notice is delivered to the owning session (Desktop chat or Kanban run) exactly once.",
    "Rows created as source=cli show in the Desktop sidebar. Create them as source=tool.",
    "The desktop app for b2bcf494ad was copied from a peer in 205 s while admission was held.",
    "The worker was cut off mid-turn. The Desktop chat showed no notice afterwards.",
    "The fix must start from the session row; Desktop chats are otherwise unchanged.",
]


@pytest.mark.parametrize("body", _SYMPTOM_BODIES)
def test_desktop_surface_in_the_body_does_not_keep_the_card_local(body):
    d = decide({"title": "Fix the session-activity projection", "body": body})
    assert d["where"] == "hub", d


@pytest.mark.parametrize("body", _SYMPTOM_BODIES)
def test_desktop_surface_in_the_body_is_portable_to_the_worker_pool(body):
    assert kp.portable_reason({"title": "Fix the session-activity projection", "body": body}, {}) is None


@pytest.mark.parametrize("title", [
    "Hermes Desktop sidebar shows red on idle sessions",
    "Desktop app: composer loses focus after a model switch",
    "Fix Desktop chats discarded as headless owners",
    "Desktop renderer crashes on a long diff",
])
def test_desktop_surface_in_the_title_still_pins(title):
    d = decide({"title": title, "body": "Push the fix."})
    assert d["where"] == "local" and d["why"].startswith("local marker"), d
    assert kp.portable_reason({"title": title, "body": "Push the fix."}, {}) == "local marker"


# Bodies that tell the worker to operate the running app: an operate verb and a Desktop surface in one sentence,
# in either order. Each was pinned on main; review t_01a1ce5ad188bd9c found most of them leaving on f0d80b24.
_OPERATE_BODIES = [
    "Live on the MacBook: a throwaway desktop chat runs the CLI block on a fixture card.",
    "A hidden scratch Desktop chat will subscribe and complete this card mid-turn.\nThen read the notice.",
    "Open a fresh Desktop window and read the status bar.",
    "Needs a Desktop DOM readback of the composer",
    "Open a Desktop chat and send one prompt.",
    "Focus the existing Desktop window and read the status bar.",
    "Open a fresh Hermes Desktop chat and send one prompt.",
    "Quit and relaunch the Desktop app, then read the version.",
    # Finding A
    "Open Hermes Desktop and send one prompt.",
    "Restart Hermes Desktop and confirm the version in Settings.",
    "Quit Hermes Desktop, then relaunch it and read the version.",
    "In Hermes Desktop, open Settings and toggle the Kanban panel.",
    "In the Desktop app, open Settings and toggle the Kanban panel.",
    "Send one prompt in the Desktop chat and read the notice.",
    "Reload the Desktop window and read the status bar.",
    "Drive the Desktop chat composer and read back the rendered notice.",
    "Use the existing Desktop chat to send one prompt.",
    "Relaunching the Desktop app must keep the session; check it by hand.",
    "Restart the Desktop backend and read its log for the new line.",
    # Finding A-weak
    "Verify in the Desktop sidebar that the red badge clears after the card completes.",
    "Reproduce the stuck spinner in the Desktop app and record what the composer shows.",
    "Watch the Desktop sidebar while this card completes.",
    # Matched only by LIVE_DESKTOP_MARKER's ``(hermes )?``: no operate verb in the sentence.
    "A throwaway Hermes Desktop chat subscribes to the card.",
]


@pytest.mark.parametrize("body", _OPERATE_BODIES)
def test_a_body_that_operates_the_live_app_still_pins(body):
    d = decide({"title": "Prove the notice text", "body": body})
    assert d["where"] == "local" and d["why"].startswith("local marker"), (body, d)
    assert kp.portable_reason({"title": "Prove the notice text", "body": body}, {}) == "local marker"


# Every operate verb form, before the surface and after it.
@pytest.mark.parametrize("verb", [
    "open", "opens", "opening", "opened", "launch", "launches", "launching", "launched", "relaunch",
    "relaunches", "relaunching", "relaunched", "restart", "restarts", "restarting", "restarted", "quit",
    "quits", "quitting", "focus", "focuses", "focusing", "focused", "click", "clicks", "clicking", "clicked",
    "send", "sends", "sending", "reload", "reloads", "reloading", "reloaded", "drive", "drives", "driving",
    "toggle", "toggles", "toggling", "toggled", "verify", "verifies", "verifying", "verified", "reproduce",
    "reproduces", "reproducing", "reproduced", "watch", "watches", "watching", "watched", "confirm", "confirms",
    "confirming", "confirmed", "use", "uses", "using", "used",
])
@pytest.mark.parametrize("shape", ["{v} it in the Desktop app", "In the Desktop app, {v} it"])
def test_every_operate_verb_pins_in_either_order(verb, shape):
    body = shape.format(v=verb)
    assert decide({"title": "Prove the notice text", "body": body})["where"] == "local", body
    assert kp.portable_reason({"title": "Prove the notice text", "body": body}, {}) == "local marker", body


@pytest.mark.parametrize("surface", [
    "Hermes Desktop", "the Desktop app", "the Desktop backend", "the Desktop backends", "the Desktop renderer",
    "the Desktop window", "the Desktop chat", "the Desktop chats", "the Desktop sidebar", "the Desktop UI",
])
def test_every_desktop_surface_pins_beside_an_operate_verb(surface):
    for body in (f"Restart {surface} and read the log.", f"In {surface}, restart it."):
        assert decide({"title": "Prove the notice text", "body": body})["where"] == "local", body
        assert kp.portable_reason({"title": "Prove the notice text", "body": body}, {}) == "local marker", body


# Symptom text: the app named beside a common word that is deliberately not an operate verb (start, type,
# check, read, run, show), or an operate verb in another sentence (split by ".", ";" or a newline) or more
# than 80 characters away, or a "Desktop session" (main never matched it).
_NOT_OPERATE_BODIES = [
    "Desktop chat turns are held at their start and end by plugin hooks.",
    "Fix the type errors in the Desktop app's TypeScript so CI is green.",
    "Check and read the run log while the Desktop sidebar shows red.",
    "Run the suite until the Desktop chat turns start.",
    "The Desktop sidebar does not show the badge.",
    "Open the log file. The Desktop chat showed no notice.",
    "Open the log file; the Desktop chat showed no notice.",
    "Open the log file\nthe Desktop chat showed no notice.",
    "The Desktop chat showed no notice. Open the log file.",
    "The Desktop chat showed no notice; open the log file.",
    "The Desktop chat showed no notice\nopen the log file.",
    "Open the replica reader that returns twelve thousand rows one sqlite step at a time for every board in "
    "the Desktop chat.",
    "The Desktop chat stalls while the replica reader returns twelve thousand rows one sqlite step at a time "
    "for every board, then open it.",
    "Each new Desktop session re-reads config.yaml. Cache it in the loader.",
]


@pytest.mark.parametrize("body", _NOT_OPERATE_BODIES)
def test_symptom_text_without_an_operate_sentence_leaves(body):
    d = decide({"title": "Fix the session-activity projection", "body": body})
    assert d["where"] == "hub", (body, d)
    assert kp.portable_reason({"title": "Fix the session-activity projection", "body": body}, {}) is None, body


def test_a_replaced_marker_list_scans_the_body_for_the_desktop_surface():
    """A caller who replaces ``local_markers`` owns the list: every pattern scans the whole text."""
    cfg = {**helpers.ROUTE, "local_markers": [kp.DESKTOP_SURFACE_MARKER]}
    d = decide({"title": "Fix the projection", "body": _BODY}, cfg=cfg)
    assert d["where"] == "local" and "desktop chat" in d["why"], d


def test_create_tool_sends_a_card_that_describes_desktop_chats_to_the_hub(mac):
    """Through ``kanban_create``: the verbatim card that stayed on the hub leaves the origin."""
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": _TITLE, "body": _BODY}))
    assert out.get("placed", {}).get("gateway") == "hub" and "placement" not in out, out
    assert helpers._local_count() == 0
    for title, body in (("Prove the notice text", "Open a Desktop chat and send one prompt."),
                        ("Confirm the build version", "Open Hermes Desktop and send one prompt."),
                        ("Toggle the Kanban panel", "In the Desktop app, open Settings and toggle the Kanban panel."),
                        ("Read the block actor line", "A throwaway Desktop chat runs the CLI block."),
                        ("Subscribe a scratch owner", "A throwaway Hermes Desktop chat subscribes to the card.")):
        live = json.loads(kt._handle_create({"title": title, "body": body}))
        assert live["placement"]["where"] == "local" and "desktop" in live["placement"]["why"], live
    assert helpers._local_count() == 5
    for title, body in (
            ("Unhold plugin hook turns", "Desktop chat turns are held at their start and end by plugin hooks."),
            ("Cache the config loader", "Each new Desktop session re-reads config.yaml. Cache it in the loader.")):
        out = json.loads(kt._handle_create({"title": title, "body": body}))
        assert out.get("placed", {}).get("gateway") == "hub" and "placement" not in out, out
    assert helpers._local_count() == 5
    pinned = json.loads(kt._handle_create({"title": "Desktop app: composer loses focus", "body": "Push the fix."}))
    assert pinned["placement"] == {"where": "local", "why": "local marker: mentions 'desktop app'"}, pinned
