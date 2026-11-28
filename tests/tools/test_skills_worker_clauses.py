"""Direct clause-level kills for tools/skills_tool_worker.py and the skill_view gate."""
import json

import pytest

from tools import skills_tool as skills
from tools import skills_tool_worker as worker

GUIDE = '# Guide\nIntro\n## Procedure\nDo it.\n## Examples\nexample body\n'


@pytest.fixture
def skill(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    folder = root / 'sample'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: sample\ndescription: d\n---\n' + GUIDE)
    monkeypatch.setattr(skills, 'skill_search_dirs', lambda: ([('local', root)], root))
    monkeypatch.setattr(skills, '_is_skill_disabled', lambda *a: False)
    monkeypatch.setattr(skills, '_skill_readiness', lambda *a: ({}, {}))
    monkeypatch.setattr(skills, '_preprocess_skill', lambda content, *a: content)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    return GUIDE


def _titles(content):
    return [t for t, _ in worker._sections(content)[1]]


def _payload(**kw):
    return json.dumps({'success': True, 'content': GUIDE, **kw})


# --- worker_view (C269) ---------------------------------------------------
def test_worker_view_requires_dispatcher_ownership_for_kanban_env(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_x')
    assert worker.worker_view() is True
    with non_dispatcher_owned_context():
        assert worker.worker_view() is False  # C269: env alone is not a worker


# --- _sections ------------------------------------------------------------
def test_sections_without_headings_and_empty_content():  # C221, C249
    assert worker._sections('plain text\nno headings\n') == ('plain text\nno headings\n', [])
    assert worker._sections('') == ('', [])


def test_frontmatter_headings_are_ignored():  # C233, C251, C271, C272, C288
    content = '---\nname: x\n## hidden\n---\n# Guide\n## Real\nbody\n'
    assert _titles(content) == ['Real']
    intro, parts = worker._sections(content)
    assert intro.startswith('---\nname: x\n## hidden\n---\n# Guide\n')
    assert parts == [('Real', '## Real\nbody\n')]


def test_fence_closes_only_with_same_char_and_enough_length():  # C274, C289, C290
    other_char = '# G\n## Real\n```\n~~~\n## fake\n```\n## After\n'
    assert _titles(other_char) == ['Real', 'After']
    shorter = '# G\n## Real\n````\n```\n## fake\n````\n## After\n'
    assert _titles(shorter) == ['Real', 'After']


def test_deeper_headings_after_first_peer_are_not_sections():  # C280, C295
    content = '# G\n## A\n### sub\n## B\n### late\n'
    assert _titles(content) == ['A', 'B']
    parts = worker._sections(content)[1]
    assert parts[0][1] == '## A\n### sub\n' and parts[1][1] == '## B\n### late\n'


# --- select_content (C240) --------------------------------------------------
def test_star_section_returns_everything():
    shown, titles = worker.select_content(GUIDE, '*')
    assert shown == GUIDE and titles == ['Procedure', 'Examples']


# --- present ----------------------------------------------------------------
def test_present_passes_through_unsuccessful_or_non_string_payloads(monkeypatch):  # C227, C245, C246
    monkeypatch.setattr(worker, 'worker_view', lambda: True)
    failed = json.dumps({'success': False, 'content': GUIDE})
    assert worker.present(failed, section=None, file_path=None) == failed
    no_content = json.dumps({'success': True, 'content': None})
    assert worker.present(no_content, section=None, file_path=None) == no_content
    missing = json.dumps({'success': True})
    assert worker.present(missing, section=None, file_path=None) == missing


def test_present_toc_only_for_compact_view_with_titles(monkeypatch):  # C228, C229, C247, C248, C267
    monkeypatch.setattr(worker, 'worker_view', lambda: True)
    compact = json.loads(worker.present(_payload(), section=None, file_path=None))
    assert compact['content'].endswith('\n\n## Other sections (load on demand)\n- Procedure\n- Examples')
    chosen = json.loads(worker.present(_payload(), section='Examples', file_path=None))
    assert 'Other sections' not in chosen['content']
    assert chosen['compact'] is False and chosen['content'] == '## Examples\nexample body\n'
    flat = json.loads(worker.present(_payload(content='no headings\n'), section=None, file_path=None))
    assert flat['content'] == 'no headings\n' and flat['sections'] == []


def test_explicit_section_projects_outside_worker_view(monkeypatch):  # C263
    monkeypatch.setattr(worker, 'worker_view', lambda: False)
    out = json.loads(worker.present(_payload(), section='Examples', file_path=None))
    assert out['content'] == '## Examples\nexample body\n'
    # no section, non-worker: untouched
    raw = _payload()
    assert worker.present(raw, section=None, file_path=None) == raw


# --- skill_view gate (C216) -------------------------------------------------
def test_section_request_is_honored_even_without_preprocess(skill, monkeypatch):
    monkeypatch.setattr(skills, 'is_background_review', lambda: False)
    out = json.loads(skills.skill_view('sample', section='Examples', preprocess=False))
    assert out['content'] == '## Examples\nexample body\n'
    assert out['compact'] is False
