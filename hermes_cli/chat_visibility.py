"""CLI proof-chat visibility; authorship does not grant terminal or owner authority."""
import os


def creation_visibility(*, visible=False):
    """Start-time ancestry only: main's own advertised HERMES_AGENT is not evidence."""
    from hermes_cli.kanban_comment_writer import Inconclusive, agent_ancestor
    try:
        agent = agent_ancestor() is not None
    except Inconclusive:
        agent = False
    # health: allow HX002 -- explicit CLI opt-out required for proof-chat visibility, not a credential
    return {'agent_cli': agent, 'hidden': agent and not (visible or os.getenv('HERMES_CHAT_VISIBLE') == '1')}


def hide_classic_session(db, session_id):
    """Classic in-process creates retain provenance for the ungated retirement sweep."""
    from hermes_cli.gateway_session_store import GatewaySessionStore
    if isinstance(db, GatewaySessionStore):
        return  # gateway create already committed the visibility with its immutable receipt
    flags = creation_visibility()
    if flags['agent_cli']:
        db.seed_session_model_config(session_id, {'_agent_cli': True})
    if flags['hidden']:
        db.set_session_hidden(session_id, True)

