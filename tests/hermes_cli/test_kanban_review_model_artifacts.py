"""Artifact custody drops authors only with positive commit-range disjointness."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_review_implementer_set as authors
from tests.hermes_cli.test_kanban_review_retry_custody import ARTIFACT, handoff

board = authors.board


def receipt(head, commits):
    return {'base': '0' * 40, 'head': head, 'commits': commits}


def test_unhanded_work_after_artifact_stays_excluded(board):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, ARTIFACT)
        authors._run(conn, tid, receipt=authors.OPUS)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}


@pytest.mark.parametrize('overlap', [True, False])
def test_complete_ranges_control_same_pr_author_custody(board, overlap):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        first = {**ARTIFACT, 'review_commit_range': receipt('b' * 40, ['b' * 40])}
        handoff(conn, tid, authors.ASTRA, first)
        rid = authors._run(conn, tid, receipt=authors.GROK, lane='review')
        conn.execute("UPDATE task_runs SET outcome='changes_requested' WHERE id=?", (rid,))
        commits = ['d' * 40, 'b' * 40] if overlap else ['d' * 40]
        second = {**ARTIFACT, 'merge_commit': 'c' * 40, 'reviewed_sha': 'd' * 40,
                  'review_commit_range': receipt('d' * 40, commits)}
        handoff(conn, tid, authors.OPUS, second)
        excluded = review.implementer_models(conn, kb.get_task(conn, tid))
        assert excluded == ({authors.ASTRA, authors.OPUS} if overlap else {authors.OPUS})


@pytest.mark.parametrize('replacement', [False, True])
def test_unproven_range_keeps_full_history(board, replacement):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, {**ARTIFACT,
                'review_commit_range': receipt('b' * 40, ['b' * 40])})
        second = {**ARTIFACT, 'reviewed_sha': 'd' * 40, 'merge_commit': None,
                  'review_commit_range': receipt('d' * 40, ['d' * 40, 'e' * 40])}
        if replacement:
            second['published_pr'] = 'https://github.com/example/project/pull/13'
            second['review_commit_range'] = receipt('d' * 40, ['d' * 40])
        handoff(conn, tid, authors.OPUS, second)
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}


def test_prior_delivery_range_accounts_for_non_head_commit(board):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.GROK, {**ARTIFACT, 'reviewed_sha': 'f' * 40, 'merge_commit': None,
                'review_commit_range': receipt('f' * 40, ['f' * 40])})
        handoff(conn, tid, authors.ASTRA, {**ARTIFACT, 'merge_commit': None,
                'review_commit_range': receipt('b' * 40, ['b' * 40, 'e' * 40])})
        handoff(conn, tid, authors.OPUS, {**ARTIFACT, 'reviewed_sha': 'd' * 40, 'merge_commit': None,
                'review_commit_range': receipt('d' * 40, ['d' * 40, 'e' * 40])})
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}


@pytest.mark.parametrize('bad', [None, {}, [], {'base': 'bad'},
    receipt('e' * 40, ['e' * 40]), receipt('d' * 40, []),
    receipt('d' * 40, ['d' * 40, 'bad']), receipt('d' * 40, ['d' * 40, 'd' * 40]),
    {**receipt('d' * 40, ['d' * 40]), 'base': 'bad'},
    receipt('d' * 40, ['d' * 40, '0' * 40]), receipt('d' * 40, ['e' * 40])])
def test_invalid_range_never_releases_same_pr_author(board, bad):
    with authors.kbc.connect() as conn:
        tid = authors._card(conn)
        handoff(conn, tid, authors.ASTRA, {**ARTIFACT,
                'review_commit_range': receipt('b' * 40, ['b' * 40])})
        handoff(conn, tid, authors.OPUS, {**ARTIFACT, 'merge_commit': 'c' * 40,
                                        'reviewed_sha': 'd' * 40, 'review_commit_range': bad})
        assert review.implementer_models(conn, kb.get_task(conn, tid)) == {authors.ASTRA, authors.OPUS}
