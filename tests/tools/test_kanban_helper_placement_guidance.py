"""Placement hints distinguish reachable evidence from host-only operations."""
from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA


def test_helper_placement_guidance_checks_reach_without_granting_write_authority():
    description = KANBAN_CREATE_SCHEMA["parameters"]["properties"]["placement"]["description"]
    assert "workers' attested capabilities" in description
    assert "hub-local write, unavailable runtime access or unpublished input" in description
    assert "A hub name in evidence is not a locality requirement" in description
    assert "Read access does not grant foreign-card or registry write authority" in description
