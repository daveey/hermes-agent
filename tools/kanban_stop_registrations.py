"""Topical stop-only (and release-offer) tool registration entries; keeps the Kanban facade bounded."""
from tools import kanban_containment_tools as containment
from tools import kanban_offer_release_tools as offer_release
from tools import kanban_retain_tools as retain
from tools import kanban_protect_stopped_tools as protection
from tools import kanban_stop_ended_tools as stop_ended


def entries(wrap):
    return tuple((name, schema, wrap(name)(handler), icon) for name, schema, handler, icon in (
        ('kanban_retain', retain.SCHEMA, retain.handle, '⏸'),
        ('kanban_protect_stopped', protection.SCHEMA, protection.handle, '⏸'),
        ('kanban_stop_ended', stop_ended.SCHEMA, stop_ended.handle, '⏸'),
        ('kanban_offer_release', offer_release.SCHEMA, offer_release.handle, '▶'),
        ('kanban_contain', containment.CONTAIN_SCHEMA, containment.handle_contain, '⏸'),
        ('kanban_resume_contained', containment.RESUME_SCHEMA, containment.handle_resume, '▶')))
