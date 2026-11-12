"""The local-marker remedy names an override that works. [daveey]

11-12: the skill-marker remedy said re-create with placement='hub', which hub admission refuses for portable work
(code placement_unavailable), so a caller following it was left with an archived card and nothing created
(t_01a1cfbd8f74bf87, skill babybear-development).
"""
from __future__ import annotations

import json

from hermes_cli import kanban_placement as kp

pytest_plugins = ["tests.hermes_cli.test_kanban_placement"]  # its ``mac`` fixture (fake hub + board)


def test_a_skill_marker_remedy_names_auto_only_when_auto_would_move_the_card(mac, monkeypatch):
    """A context skill (babybear-*) names 'auto', and following it moves the card; a device skill (computer-use)
    stays local even with 'auto', so its remedy says to drop the skill instead."""
    from hermes_cli import kanban_skill_check
    from tools import kanban_tools as kt
    monkeypatch.setattr(kanban_skill_check, "check_task_skills", lambda *a, **k: None)
    card = {"title": "Fix the parser and push", "body": "Python only.", "skills": ["babybear-development"]}
    out = json.loads(kt._handle_create(card))
    assert out["ok"] and out["placement"]["why"] == "local marker: skill babybear-development", out
    assert out["remedy"] == "if the work doesn't need this machine, archive and re-create with placement='auto'"
    moved = json.loads(kt._handle_create({**card, "placement": "auto"}))
    assert moved["ok"] and moved.get("placed", {}).get("gateway") == "hub", moved  # the remedy works
    device = json.loads(kt._handle_create({**card, "skills": ["computer-use"], "placement": "auto"}))
    assert device["ok"] and device["placement"]["why"] == "local marker: skill computer-use", device
    assert device["remedy"] == kp.DEVICE_SKILL_MARKER_REMEDY, device


def test_a_mixed_skill_card_gets_the_device_remedy_when_auto_would_not_move_it(mac, monkeypatch):
    """[babybear-*, computer-use] with placement omitted hits the context skill first. Naming 'auto' there was a
    dead end: re-created with 'auto' it still pinned local on computer-use. The first remedy is the one that works."""
    from hermes_cli import kanban_skill_check
    from tools import kanban_tools as kt
    monkeypatch.setattr(kanban_skill_check, "check_task_skills", lambda *a, **k: None)
    card = {"title": "Fix the parser and push", "body": "Python only.",
            "skills": ["babybear-development", "computer-use"]}
    out = json.loads(kt._handle_create(card))
    assert out["ok"] and out["placement"]["why"] == "local marker: skill babybear-development", out
    assert out["remedy"] == kp.DEVICE_SKILL_MARKER_REMEDY, out
    following_auto = json.loads(kt._handle_create({**card, "placement": "auto"}))
    assert following_auto["placement"]["why"] == "local marker: skill computer-use", following_auto  # still local
    # Without the device skill, 'auto' is still the advice (and it moves the card).
    assert kp.marker_remedy("local marker: skill babybear-development", {},
                            {**card, "skills": ["babybear-development"]}) == kp.LOCAL_MARKER_REMEDY
