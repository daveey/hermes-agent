"""The owner's per-turn profile read for a following local session's in-process build. [daveey]

A desktop Bot Chat is not managed (``managed_policy`` is None), so its agent is built in process by
the TurnRunner, under ``policy_scope``. Before the turn runs, the owner commits the turn's effective
selection (``session_profile_selection.commit_selection``) and then holds that SAME profile read for
the turn (``_TURN_PROFILE``): the TurnRunner's ``policy_scope`` and every resolution inside it reuse it,
so the committed switch intent and the agent's route come from one read, and a profile edit landing in
between reaches the next turn instead of half of this one. The managed path does the same in
``session_managed_worker._bootstrap`` with the frame it sends the child.
"""
from contextlib import contextmanager


def live_policy(authority, ref):
    """The launch policy the owner currently publishes for *ref*."""
    from gateway.session_policy import policy_for_source
    return policy_for_source(authority.runner, authority.sessions[ref.session_id].source)


@contextmanager
def turn_selection(authority, ref, policy, generation):
    """Commit the following *policy*'s selection for the admission at *generation*, then hold the
    turn's one profile read for everything the turn resolves. A frozen or bypass policy is untouched."""
    if policy is None or not policy.follows_profile or policy.ignore_user_config:
        yield
        return
    from gateway.session_policy import _TURN_PROFILE, profile_config
    from gateway.session_profile_selection import commit_selection
    live = profile_config(authority)
    token = _TURN_PROFILE.set((policy, live))
    try:
        config, model, ended = policy.effective_selection(authority, secrets=False)
        current = commit_selection(authority, ref, policy, config, model, generation, ended=ended)
        if current is not policy:
            # The released receipt is what the TurnRunner's policy_scope will look up: same read.
            _TURN_PROFILE.set((current, live))
        yield
    finally:
        _TURN_PROFILE.reset(token)
