"""Keep executable expansions inside shell words without losing their command bodies.

This is lexical separation, not expansion evaluation: the placeholder cannot name a
file, and each command body is independently walked by the existing script scanner.
"""
from __future__ import annotations

import re
import shlex


# No whitespace or shell punctuation; NUL prevents a guessed expansion naming a file.
SUBSTITUTION = '\x00subst'
# Distinguish potentially absent words until their lexical context is checked.
_EMPTY_SUBSTITUTION = '\x00empty-subst'


def normalize_ansi_c_quotes(text: str) -> str:
    """Make ANSI-C quote spans safe for the POSIX lexical helpers.

    Only quote/backslash escapes are needed for delimiter handling; retain other
    escapes literally rather than guessing their expansion into shell syntax.
    """
    out: list[str] = []
    index = 0
    quote = ''
    while index < len(text):
        ch = text[index]
        if ch == '\\' and quote != "'":
            out.append(text[index:index + 2])
            index += 2
            continue
        if not quote and text.startswith("$'", index):
            index += 2
            value: list[str] = []
            while index < len(text) and text[index] != "'":
                if text[index] == '\\' and index + 1 < len(text):
                    following = text[index + 1]
                    value.append(following if following in "'\\" else '\\' + following)
                    index += 2
                else:
                    value.append(text[index])
                    index += 1
            out.append(shlex.quote(''.join(value)))
            index += index < len(text)
            continue
        if quote:
            if ch == ("'" if quote == "$'" else quote):
                quote = ''
        elif ch in "'\"":
            quote = ch
        elif ch == '#' and (index == 0 or text[index - 1].isspace()
                            or text[index - 1] in ';&|()'):
            end = text.find('\n', index)
            end = len(text) if end < 0 else end
            out.append(text[index:end])
            index = end
            continue
        out.append(ch)
        index += 1
    return ''.join(out)


def _empty_word_variant(text: str) -> str:
    """Drop only complete unquoted expansion words, never redirection operands."""
    out: list[str] = []
    index = 0
    operand = False
    while index < len(text):
        ch = text[index]
        if ch.isspace():
            out.append(ch)
            index += 1
            continue
        if ch in '<>':
            operator = text[index:index + 2]
            if len(operator) < 2 or operator[1] not in '<>&|':
                operator = ch
            out.append(operator)
            index += len(operator)
            operand = True
            continue
        if ch in ';&|()':
            out.append(ch)
            index += 1
            operand = False
            continue
        start = index
        quote = ''
        while index < len(text):
            ch = text[index]
            if ch == '\\' and quote != "'":
                index += 2
                continue
            if quote:
                if ch == ("'" if quote == "$'" else quote):
                    quote = ''
            elif text.startswith("$'", index):
                quote = "$'"
                index += 2
                continue
            elif ch in "'\"":
                quote = ch
            elif ch.isspace() or ch in ';&|()<>':
                break
            index += 1
        word = text[start:index]
        if operand or word.replace(_EMPTY_SUBSTITUTION, ''):
            out.append(word)
        operand = False
    return ''.join(out).replace(_EMPTY_SUBSTITUTION, SUBSTITUTION)


# Bound both delimiter matching and the later body walk, before Python's stack limit.
MAX_SUBSTITUTION_DEPTH = 32


class SubstitutionNestingLimit(ValueError):
    """Executable expansion nesting exceeded the lexical scan's safe bound."""


def _backtick_end(text: str, start: int) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == '\\':
            index += 2
        elif text[index] == '`':
            return index + 1
        else:
            index += 1
    return len(text)


_HEREDOC_OPERATOR = re.compile(r"<<(-?)[ \t]*((?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|()<>])+)")
_COMMAND_KEYWORDS = frozenset({'then', 'do', 'else', 'elif', 'if', 'while', 'until', '!'})


class _ParenScan:
    """Lexical state for one ``$(`` span; each ``step_*`` method returns True if it consumed input."""

    def __init__(self, text: str, start: int, nesting: int) -> None:
        self.text, self.start, self.nesting = text, start, nesting
        self.index, self.depth = start + 2, 1
        self.quote = ''
        self.command_position = True
        # (phase, parenthesis depth); phases: subject, in, pattern-start, pattern, body.
        self.cases: list[tuple[str, int]] = []
        self.heredocs: list[tuple[str, bool]] = []

    def phase(self) -> str:
        return self.cases[-1][0] if self.cases else ''

    def step_heredoc_operator(self) -> bool:
        text, index = self.text, self.index
        if (self.quote or text.startswith('$((', self.start)
                or not text.startswith('<<', index) or text.startswith('<<<', index)):
            return False
        # A delimiter is a shell word, not executable expansion syntax. Queue
        # bodies until the command's newline, including multiple heredocs.
        match = _HEREDOC_OPERATOR.match(text, index)
        if not match:
            return False
        try:
            delimiter = shlex.split(match[2])[0]
        except (ValueError, IndexError):
            delimiter = match[2]
        self.heredocs.append((delimiter, bool(match[1])))
        self.index += len(match[0])
        return True

    def step_heredoc_bodies(self) -> bool:
        if self.quote or self.text[self.index] != '\n' or not self.heredocs:
            return False
        text, index = self.text, self.index + 1
        for delimiter, strip_tabs in self.heredocs:
            while index < len(text):
                end = text.find('\n', index)
                end = len(text) if end < 0 else end
                line = text[index:end]
                index = min(end + 1, len(text))
                if (line.lstrip('\t') if strip_tabs else line) == delimiter:
                    break
        self.heredocs.clear()
        self.index = index
        self.command_position = True
        return True

    def step_bracket_pattern(self) -> bool:
        text, cases = self.text, self.cases
        if self.quote or text[self.index] != '[' or not cases or not cases[-1][0].startswith('pattern'):
            return False
        # Parentheses inside a bracket expression are pattern characters.
        end = self.index + 1
        if end < len(text) and text[end] in '!^':
            end += 1
        if end < len(text) and text[end] == ']':
            end += 1
        while end < len(text) and text[end] != ']':
            end += 2 if text[end] == '\\' else 1
        if end >= len(text):
            return False
        self.index = end + 1
        cases[-1] = ('pattern', cases[-1][1])
        return True

    def step_escape_or_nested(self) -> bool:
        text, index, ch = self.text, self.index, self.text[self.index]
        if ch == '\\' and self.quote != "'":
            self.command_position = False
            self.index += 2
            return True
        if not self.quote and text.startswith("$'", index):
            self.quote = "$'"
            self.index += 2
            self.command_position = False
            return True
        if self.quote not in {"'", "$'"} and text.startswith('$(', index):
            self.index = _paren_end(text, index, self.nesting + 1)
            self.command_position = False
            return True
        if self.quote not in {"'", "$'"} and ch == '`':
            self.index = _backtick_end(text, index)
            self.command_position = False
            return True
        return False

    def step_quote_or_comment(self) -> bool:
        text, index, ch = self.text, self.index, self.text[self.index]
        if self.quote:
            if ch == ("'" if self.quote == "$'" else self.quote):
                self.quote = ''
            self.index += 1
            return True
        if ch in "'\"":
            self.quote = ch
            self.command_position = False
            self.index += 1
            return True
        if ch == '#' and (index == 0 or text[index - 1].isspace() or text[index - 1] in ';&|()'):
            newline = text.find('\n', index)
            self.index = len(text) if newline < 0 else newline
            return True
        return False

    def open_paren(self) -> None:
        # A case pattern's optional leading '(' has no ')' of its own (the pattern
        # terminator closes it), so it does not deepen the expansion's nesting.
        if not (self.cases and self.cases[-1][0] == 'pattern-start'):
            self.depth += 1
        self.command_position = True

    def close_paren(self) -> bool:
        """Handle ``)``; True when it closes the expansion."""
        if self.cases and self.cases[-1][0].startswith('pattern'):
            case_depth = self.cases[-1][1]
            # Optional leading '(' and extglob groups are balanced normally;
            # the bare pattern terminator is not an expansion terminator.
            if self.depth > case_depth:
                self.depth -= 1
            else:
                self.cases[-1] = ('body', case_depth)
                self.command_position = True
            return False
        self.depth -= 1
        return self.depth == 0

    def step_word(self) -> None:
        match = re.match(r'[^\s;&|()\'"`\\$]+', self.text[self.index:])
        word = match[0] if match else self.text[self.index]
        phase, cases = self.phase(), self.cases
        if word == 'case' and self.command_position and not phase.startswith('pattern'):
            cases.append(('subject', self.depth))
        elif word == 'in' and phase == 'in':
            cases[-1] = ('pattern-start', cases[-1][1])
        elif word == 'esac' and cases and (self.command_position or phase.startswith('pattern')):
            cases.pop()
        elif phase == 'pattern-start':
            cases[-1] = ('pattern', cases[-1][1])
        # These keywords introduce a command, not ordinary argument positions.
        self.command_position = word in _COMMAND_KEYWORDS
        self.index += len(word)

    def step_operator(self) -> bool:
        """Handle ``(``, ``)``, case terminators and separators; True when the span closed."""
        text, ch = self.text, self.text[self.index]
        if ch == '(':
            self.open_paren()
        elif ch == ')':
            if self.close_paren():
                self.index += 1
                return True
        elif self.phase() == 'body' and text.startswith((';;', ';&'), self.index):
            self.index += 2
            # For ;;& the remaining '&' is a normal command separator.
            self.cases[-1] = ('pattern-start', self.cases[-1][1])
            self.command_position = True
            return False
        elif ch in ';&|\n':
            self.command_position = True
        elif not ch.isspace():
            self.step_word()
            return False
        self.index += 1
        return False

    def run(self) -> int:
        text = self.text
        while self.index < len(text):
            if (self.step_heredoc_operator() or self.step_heredoc_bodies()
                    or self.step_bracket_pattern()):
                continue
            if self.cases and self.cases[-1][0] == 'subject' and not text[self.index].isspace():
                self.cases[-1] = ('in', self.cases[-1][1])
                self.command_position = False
            if self.step_escape_or_nested():
                continue
            if self.step_quote_or_comment():
                continue
            if self.step_operator():
                return self.index
        return len(text)


def _paren_end(text: str, start: int, nesting: int = 0) -> int:
    """Match expansion delimiters, including the unmatched ``)`` of case patterns.

    This is not a shell evaluator. Track only command-position case/in/esac and
    pattern/body transitions; ordinary arguments named case must remain words.
    """
    if nesting >= MAX_SUBSTITUTION_DEPTH:
        raise SubstitutionNestingLimit('substitution nesting limit reached')
    return _ParenScan(text, start, nesting).run()


def separate_substitutions(text: str, *, _nesting: int = 0,
                           empty_unquoted: bool = False) -> tuple[str, list[str]]:
    """Return the outer text and executed bodies, before redirection/line splitting.

    Single quotes and comments are inert. Double quotes still execute expansions.
    Arithmetic is not a shell command, but its nested command expansions execute.
    Unclosed spans retain their remainder as a body (conservative partial input).
    """
    if _nesting >= MAX_SUBSTITUTION_DEPTH:
        raise SubstitutionNestingLimit('substitution nesting limit reached')
    out: list[str] = []
    bodies: list[str] = []
    index = 0
    quote = ''
    while index < len(text):
        ch = text[index]
        if ch == '\\' and quote != "'":
            out.append(text[index:index + 2])
            index += 2
            continue
        if not quote and text.startswith("$'", index):
            quote = "$'"
            out.append("$'")
            index += 2
            continue
        if quote not in {"'", "$'"} and (text.startswith('$(', index) or ch == '`'):
            backtick = ch == '`'
            end = _backtick_end(text, index) if backtick else _paren_end(text, index)
            arithmetic = text.startswith('$((', index) and text[index:end].endswith('))')
            lead = 1 if backtick else (3 if arithmetic else 2)
            tail = 1 if backtick else (2 if arithmetic else 1)
            closed = text[index:end].endswith('`' if backtick else ')' * tail)
            body = text[index + lead:end - tail if closed else end]
            if backtick:
                # Legacy substitution removes backslashes before $, ` and backslash.
                escapes = r'\\([$`\\"])' if quote == '"' else r'\\([$`\\])'
                body = re.sub(escapes, r'\1', body)
            if arithmetic:
                _, nested = separate_substitutions(body, _nesting=_nesting + 1)
                bodies.extend(nested)
            else:
                bodies.append(body)
            out.append(_EMPTY_SUBSTITUTION if empty_unquoted and not quote and not arithmetic
                       else SUBSTITUTION)
            index = end
            continue
        if quote:
            if ch == ("'" if quote == "$'" else quote):
                quote = ''
        elif ch in "'\"":
            quote = ch
        elif ch == '#' and (index == 0 or text[index - 1].isspace()
                            or text[index - 1] in ';&|()'):
            end = text.find('\n', index)
            end = len(text) if end < 0 else end
            out.append(text[index:end])
            index = end
            continue
        out.append(ch)
        index += 1
    outer = ''.join(out)
    return (_empty_word_variant(outer) if empty_unquoted else outer), bodies
