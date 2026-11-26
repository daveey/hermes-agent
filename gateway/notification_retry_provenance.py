"""Finite diagnostic vocabulary for existing notification retry obligations."""
SUBPASSES = ('card_change_pass', 'remote_card_change', 'remote_notices', 'notices', 'delegation',
             'killed_notices', 'guarantee_pass', 'unknown')
REASONS = ('enumeration_failed', 'set_cursor_failed', 'top_gt_cursor', 'board_error', 'remote_error',
           'pending', 'exception', 'unknown')


def origin(subpass='unknown', reason='unknown'):
    return (subpass if subpass in SUBPASSES else 'unknown', reason if reason in REASONS else 'unknown')


def earliest(current, deadline, subpass='unknown', reason='unknown'):
    candidate = (deadline, *origin(subpass, reason))
    return candidate if current is None else min(current, candidate)
