"""Worker skill loads are selective; explicit reads and desktop loads remain complete."""
import json

import pytest

from tools import skills_tool as skills


@pytest.fixture
def skill(tmp_path, monkeypatch):
    root = tmp_path / 'skills'
    folder = root / 'sample'
    folder.mkdir(parents=True)
    content = ('---\nname: sample\ndescription: sample summary\n---\n'
               '# Sample\n\nOpening summary.\n\n## Procedure\nDo the safe thing.\n'
               '### Rules\nNever disclose secrets.\n\n## Examples\n' + 'example detail\n' * 1000
               + '\n## Troubleshooting\nConsult logs.\n')
    (folder / 'SKILL.md').write_text(content)
    monkeypatch.setattr(skills, 'skill_search_dirs', lambda: ([('local', root)], root))
    monkeypatch.setattr(skills, '_is_skill_disabled', lambda *a: False)
    monkeypatch.setattr(skills, '_skill_readiness', lambda *a: ({}, {}))
    monkeypatch.setattr(skills, '_preprocess_skill', lambda content, *a: content)
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    return content


def test_worker_gets_summary_procedure_and_toc(skill, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    view = json.loads(skills.skill_view('sample'))
    assert view['compact'] is True
    assert 'Opening summary.' in view['content']
    assert 'Do the safe thing.' in view['content']
    assert 'Never disclose secrets.' in view['content']
    assert 'example detail' not in view['content']
    assert len(view['content']) < len(skill) / 2
    assert 'Examples' in view['sections']
    assert 'Troubleshooting' in view['sections']
    full = json.loads(skills.skill_view('sample', file_path='SKILL.md'))
    assert full['content'] == skill


def test_desktop_keeps_full_load(skill):
    assert json.loads(skills.skill_view('sample'))['content'] == skill


def test_section_load_and_missing_section(skill, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    result = json.loads(skills.skill_view('sample', section='Examples'))
    assert 'example detail' in result['content']
    assert 'Consult logs.' not in result['content']
    assert json.loads(skills.skill_view('sample', section='nonexistent'))['success'] is False


def test_delegate_child_is_compact(skill):
    from agent.delegation_context import delegated_child_context
    with delegated_child_context():
        assert json.loads(skills.skill_view('sample'))['compact'] is True


def test_fenced_code_headings_are_not_sections():
    from tools.skills_tool_worker import select_content
    content = '# Guide\nSummary\n## Procedure\n```sh\n## fake\n```\n## Extra\nlarge\n'
    shown, sections = select_content(content)
    assert '## fake' in shown
    assert sections == ['Procedure', 'Extra']


def test_internal_preload_and_background_reviewer_keep_source(skill, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    assert json.loads(skills.skill_view('sample', preprocess=False))['content'] == skill
    monkeypatch.setattr(skills, 'is_background_review', lambda: True)
    assert json.loads(skills.skill_view('sample'))['content'] == skill


def test_early_subtopics_do_not_become_an_unbounded_summary():
    from tools.skills_tool_worker import select_content
    source = '# Guide\nSummary\n### Examples\n' + 'detail\n' * 1000 + '## Rules\nNever weaken acceptance.\n'
    shown, titles = select_content(source)
    assert 'detail' not in shown and 'Never weaken acceptance.' in shown
    assert titles == ['Examples', 'Rules']
