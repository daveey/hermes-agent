"""Generated sweep/review work must not manufacture a top-priority default."""
from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA


def test_create_schema_explains_generated_priority_policy():
    description = KANBAN_CREATE_SCHEMA['parameters']['properties']['priority']['description']
    assert "Generated review, sweep and test-gap cards" in description
    assert "omit this field" in description
    assert "explicit user urgency" in description
