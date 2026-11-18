"""Additive literal candidates for computed executable words; never run a shell.

Keep expansion spans opaque while locating executable positions. Resolving a
literal default is an additional reading, not a proof of shell variable state.
Other parameter operators and nonliteral substitution output fail closed only
when used to construct an executable (not when passed as ordinary data).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from cron.lifecycle_guard_substitutions import _backtick_end, _paren_end

_MARKER = '\x00computed'
_DEFAULT = re.compile(r'([A-Za-z_][A-Za-z0-9_]*):?[-=](.*)', re.DOTALL)


@dataclass
class Expansion:
    start: int
    end: int
    quoted: bool
    values: tuple[str, ...] | None
    variable: str | None = None


def _literal(text):
    # Deliberately small static subset. Unknown output is never evaluated.
    if any(ch in text for ch in '$`\\*?[]{}\x00'):
        return None
    try:
        return shlex.split(text)
    except ValueError:
        return None


def _printed(body):
    words = _literal(body)
    if not words or any(ch in body for ch in ';|&<>\n()'):
        return None
    if words[0] in {'true', ':'} and len(words) == 1:
        return ('',)
    if words[0] == 'echo' and not any(word.startswith('-') for word in words[1:]):
        return (' '.join(words[1:]),)
    if len(words) >= 2 and words[:2] == ['printf', '%s']:
        return (''.join(words[2:]),)
    return None


def _parameter_end(text, start):
    """Balance braces without interpreting the parameter operator or its value."""
    index, depth, quote = start + 2, 1, ''
    while index < len(text):
        ch = text[index]
        if ch == '\\':
            index += 2
            continue
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '\"\'':
            quote = ch
        elif text.startswith('$(', index):
            index = _paren_end(text, index)
            continue
        elif ch == '`':
            index = _backtick_end(text, index)
            continue
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if not depth:
                return index + 1
        index += 1
    return len(text)


def _expansion(text, index, quoted, bindings):
    variable = None
    if text.startswith('${', index):
        end = _parameter_end(text, index)
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', text[index + 2:end - 1]):
            return None  # Existing additive scalar-variable discovery owns this form.
        match = _DEFAULT.fullmatch(text[index + 2:end - 1]) if text[end - 1:end] == '}' else None
        values = None
        if match and (words := _literal(match[2])) is not None:
            variable = match[1]
            default = ' '.join(words) if any(c in match[2] for c in "\"'") else match[2]
            values = tuple(dict.fromkeys((default, *bindings.get(variable, ()))))
        identity = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)//([a-zA-Z0-9_]+)/\2',
                                text[index + 2:end - 1])
        if identity:
            variable = identity[1]
            values = tuple(bindings.get(variable, ())) or None
    elif text.startswith('$(', index) and not text.startswith('$((', index):
        end = _paren_end(text, index)
        values = _printed(text[index + 2:end - 1]) if text[end - 1:end] == ')' else None
    elif text[index] == '`':
        end = _backtick_end(text, index)
        values = _printed(text[index + 1:end - 1]) if text[end - 1:end] == '`' else None
    else:
        return None
    return Expansion(index, end, quoted, values, variable)


def _opaque_expansions(text, bindings):
    out, expansions = [], {}
    index, quote = 0, ''
    while index < len(text):
        ch = text[index]
        if ch == '\\' and quote != "'":
            out.append(text[index:index + 2])
            index += 2
            continue
        if quote != "'" and text.startswith('$((', index):
            end = _paren_end(text, index)
            if text[index:end].endswith('))'):
                out.append('0')  # Arithmetic output cannot name a lifecycle executable.
                index = end
                continue
        if not quote and text.startswith('((', index):
            end = index + _paren_end('$' + text[index:], 0) - 1
            if text[index:end].endswith('))'):
                out.append(':')  # Only a closed arithmetic command is opaque.
                index = end
                continue
        if quote != "'" and (span := _expansion(text, index, bool(quote), bindings)):
            marker = f'{_MARKER}{len(expansions)}z'
            expansions[marker] = span
            out.append(marker)
            index = span.end
            continue
        if not quote and ch == '#' and (not index or text[index - 1].isspace()):
            end = text.find('\n', index)
            end = len(text) if end < 0 else end
            out.append(text[index:end])
            index = end
            continue
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '\"\'':
            quote = ch
        out.append(ch)
        index += 1
    return ''.join(out), expansions


def _structural_segments(opaque):
    from cron import lifecycle_guard as guard
    from cron.lifecycle_guard_lines import _split_logical_lines

    for line in _split_logical_lines(opaque):
        try:
            tokens = guard._shlex_tokens(line)
        except ValueError:
            continue  # Evidence-recovery fragments cannot prove command position.
        yield from guard._split_segments(tokens)


def scan_computed_commands(command, *, cwd, executed, variable_source, budget, recurse, source):
    from cron import lifecycle_guard as guard

    if not executed or not variable_source:
        return False
    normalized = re.sub(r"[\"'\\\\]", '', command.replace('\\\n', ''))
    if re.search(r'\b(?:function\s+(?:echo|printf|true)\b|(?:echo|printf|true)\s*\(\s*\))', normalized):
        budget.variable_candidates.computed_producer_shadow = True
    if not any(s in command for s in ('${', '$(', '`')):
        return False
    from cron.lifecycle_guard_substitutions import separate_substitutions

    # Bodies execute even when their output is an assignment or data argument.
    # Keep their original syntax/provenance; flattened token recovery loses it.
    _, bodies = separate_substitutions(command)
    for body in bodies:
        if recurse(body, cwd, True, source):
            return True
    opaque, expansions = _opaque_expansions(command, budget.variable_candidates.values)
    for segment in _structural_segments(opaque):
        words = segment[guard._peel_reserved_words(segment):]
        words = guard._split_redirections(words)[0]
        index = guard._executed_command_index(words)
        if index is None:
            continue
        for marker, span in expansions.items():
            if marker not in words[index]:
                continue
            producer_unknown = (command[span.start:span.start + 2] != '${' and (
                budget.variable_candidates.computed_producer_shadow or budget.alias_names is None
                or bool({'echo', 'printf', 'true'} & budget.alias_names)))
            if command[span.start:span.start + 2] != '${':
                budget.variable_candidates.computed_producer_used = True
            if span.values is None or producer_unknown:
                budget.variable_candidates.unresolved_computed = True
                continue
            if span.variable:
                # Register the set-variable reading with the existing sink graph:
                # a later sourced file may supply its literal binding.
                payload = command[:span.start] + '${' + span.variable + '}' + command[span.end:]
                if recurse(payload, cwd, True, source):
                    return True
            for value in span.values:
                # Expansion output is data, not shell grammar. Never turn an
                # output semicolon into an operator. Keep both quoted and split
                # readings, as the literal-variable candidate walker does.
                replacements = (shlex.quote(value), shlex.join(value.split()))
                if span.quoted:
                    replacements = (value.replace('\\', '\\\\').replace('"', '\\"')
                                    .replace('$', '\\$').replace('`', '\\`'),)
                for replacement in dict.fromkeys(replacements):
                    payload = command[:span.start] + replacement + command[span.end:]
                    if recurse(payload, cwd, True, source):
                        return True
    return False
