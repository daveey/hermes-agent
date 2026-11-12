"""kanban_move never steers a worker to move a routine hub job back to the hub (t_01a1d0e68acaba94).

t_01a1ccba2bf93bf1 and t_01a1cad1b3da962a were moved to machine:hub for hub reads and routine hub writes that a
capability:hub-write worker does with `ssh hermes-hub` and `hub-admin`; the tool description only said to move
when the work needs "a specific host's files or logs".
"""
from tools.kanban_tools_schemas import KANBAN_MOVE_SCHEMA


def test_move_description_routes_hub_work_through_hub_admin():
    description = KANBAN_MOVE_SCHEMA["description"]
    assert "Never move a card to the hub for hub files, logs, databases, cron" in description
    assert "`hub-admin '<command>'` (declare hub:write)" in description
    assert "`ssh hermes-hub`" in description
    assert "judged by hub admission" in description
