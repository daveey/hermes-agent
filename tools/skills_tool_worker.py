"""Selective instructional reads for workers; never rewrites conversation history."""
from __future__ import annotations

import json
import os
import re


def worker_view() -> bool:
    from agent.delegation_context import is_delegated_child_process_context, is_dispatcher_owned_worker_context
    return (is_delegated_child_process_context() or
            bool(os.environ.get('HERMES_KANBAN_TASK') and is_dispatcher_owned_worker_context()))


def _sections(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Split at peer sections, ignoring Markdown headings inside fenced examples."""
    lines = content.splitlines(keepends=True)
    starts = []
    fence = ''
    frontmatter = bool(lines and lines[0].strip() == '---')
    headings = []
    for index, line in enumerate(lines):
        if frontmatter:
            if index and line.strip() == '---':
                frontmatter = False
            continue
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if match:
            marker = match[1]
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = ''
            continue
        if not fence and (match := re.match(r'^(#{1,6})\s+(.+?)\s*#*\s*$', line)):
            headings.append((index, len(match[1]), match[2]))
    # H1 is normally a title. Some skills start with H3 topics before their first H2;
    # those are sections too, not an unbounded introductory summary.
    level = min((depth for _, depth, _ in headings if depth > 1), default=1)
    first_peer = next((index for index, depth, _ in headings if depth == level), len(lines))
    starts = [(index, title) for index, depth, title in headings
              if depth == level or (depth > level and index < first_peer)]
    if not starts:
        return content, []
    sections = [(title, ''.join(lines[index:(starts[pos + 1][0] if pos + 1 < len(starts) else len(lines))]))
                for pos, (index, title) in enumerate(starts)]
    return ''.join(lines[:starts[0][0]]), sections


def select_content(content: str, section: str | None = None) -> tuple[str, list[str]]:
    intro, parts = _sections(content)
    titles = [title for title, _ in parts]
    if section:
        if section == '*':
            return content, titles
        selected = [text for title, text in parts if title.casefold() == section.casefold()]
        if not selected:
            raise ValueError(f'Unknown section {section!r}; available sections: {titles}')
        return '\n'.join(selected), titles
    # Keep procedural/rule sections in full, including their subordinate headings.
    selected = [text for title, text in parts
                if re.search(r'procedure|workflow|rules|invariants|safety|constraints|never|do not|done means', title, re.IGNORECASE)]
    return intro + '\n'.join(selected), titles


def present(result: str, *, section: str | None, file_path: str | None) -> str:
    """Project successful SKILL.md responses, including namespaced plugin skills."""
    if file_path or (not section and not worker_view()):
        return result
    payload = json.loads(result)
    if not payload.get('success') or not isinstance(payload.get('content'), str):
        return result
    try:
        content, titles = select_content(payload['content'], section)
    except ValueError as exc:
        return json.dumps({'success': False, 'error': str(exc)})
    payload.update(content=content, sections=titles, compact=not bool(section))
    payload['usage_hint'] = ('Read omitted sections with skill_view(name, section="Heading"); '
                             'section="*" or file_path="SKILL.md" reads the full skill. '
                             'Linked references use file_path as before.')
    if not section and titles:
        payload['content'] += '\n\n## Other sections (load on demand)\n' + '\n'.join(f'- {t}' for t in titles)
    return json.dumps(payload, ensure_ascii=False)
