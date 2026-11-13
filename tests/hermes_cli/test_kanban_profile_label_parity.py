"""One ``profile:<assignee>`` rule for every pool-label site, mirroring the Board's ``requiredLabels``
(``assignee || "default"``): a NULL/empty assignee is ``profile:default``, never ``profile:None``/``profile:``. [daveey]"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_pickup_stall_repin as rp
from hermes_cli import kanban_repin_notice as notice

CASES = [(None, "profile:default"), ("", "profile:default"), ("x", "profile:x")]


@pytest.mark.parametrize("assignee,label", CASES)
def test_shared_helper_reads_null_and_empty_as_default(assignee, label):
    assert notice.profile_label(assignee) == label


@pytest.mark.parametrize("assignee,label", CASES)
def test_all_three_sites_agree(assignee, label):
    row = {"assignee": assignee, "requires": None, "skills": None}
    assert notice.required_labels(row) == [label]
    assert rp._need(row) == {label}
    # The hub's unreadable-registry branch returns the need set itself.
    runs_on, _ = kps.alarm_placement(kps.OPERATOR_LANE, None, assignee=assignee, requires=())
    assert label in runs_on


def test_every_site_routes_through_the_shared_helper(monkeypatch):
    monkeypatch.setattr(notice, "profile_label", lambda assignee: "profile:SENTINEL")
    row = {"assignee": "x", "requires": None, "skills": None}
    assert notice.required_labels(row) == ["profile:SENTINEL"]
    assert rp._need(row) == {"profile:SENTINEL"}
    runs_on, _ = kps.alarm_placement(kps.OPERATOR_LANE, None, assignee="x", requires=())
    assert "profile:SENTINEL" in runs_on
