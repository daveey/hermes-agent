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


@pytest.mark.parametrize("body", [
    "Live on the MacBook: a throwaway desktop chat runs the CLI block on a fixture card.",
    "A hidden scratch Desktop chat will subscribe and complete this card mid-turn.\nThen read the notice.",
    "Open a fresh Desktop window and read the status bar.",
    "Start a new Desktop session and send one prompt.",
    "Needs a Desktop DOM readback of the composer",
])
def test_a_chat_or_window_the_card_drives_in_the_live_app_still_pins(body):
    d = decide({"title": "Prove the notice text", "body": body})
    assert d["where"] == "local" and d["why"].startswith("local marker"), (body, d)
    assert kp.portable_reason({"title": "Prove the notice text", "body": body}, {}) == "local marker"


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
    pinned = json.loads(kt._handle_create({"title": "Desktop app: composer loses focus", "body": "Push the fix."}))
    assert pinned["placement"] == {"where": "local", "why": "local marker: mentions 'desktop app'"}, pinned
