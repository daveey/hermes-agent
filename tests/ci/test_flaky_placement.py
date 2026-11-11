"""Deflake guidance must not imply that the work needs the creator's host."""
from hermes_cli import kanban_placement as placement
from scripts.ci import flaky_kanban


def test_deflake_template_is_portable():
    body = flaky_kanban._card_body({"test_id": "tests/test_example.py::test_wait"},
                                   "https://github.com/example/project/actions/runs/1", "main", None)
    assert "Usual cause on the CI runners" in body
    assert placement.portable_reason({"title": "Deflake test_wait", "body": body}, {}) is None
