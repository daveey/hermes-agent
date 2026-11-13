"""Which files reach a command's stdin through shell grouping, for the lifecycle guard.

A pipe into a compound command (``cat f | (…)``, ``| { …; }``, ``| if …; fi``, ``| while …;
done``, ``| case … esac``) feeds every command in it, and a group writes on what each of its
commands writes (``{ cat f.py; } | python3``) unless that output is piped elsewhere inside it.
A stdin redirection on a compound command (``{ true; bash; } < f.sh``, ``(bash) < f.sh``,
``while read l; do bash; done < f.sh``) feeds it the same way. A process substitution
(``python3 <(cat f.py)``, ``bash < <(cat f.sh)``) puts what it writes where the ``<(…)`` stands,
and ``cat f.py > >(python3)`` feeds the ``>(…)`` body. A ``cat`` or ``tee`` reading its stdin
passes the feed on (``cat f.py | cat | python3``), and a pipe ending a line continues on the
next. Shell words come from the caller's tokenizer; this module only walks
them, so it never imports ``cron.lifecycle_guard``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Optional

CONTROL_CHARS = frozenset(";&|()")
_CONTROL_OPERATOR = re.compile(r"\|\||&&|\|&|;;|[;&|()]")
# Compound commands a pipe feeds as a whole: the word that opens one -> the word that closes it.
_LEADING_OPENERS = {"{": "}", "if": "fi", "while": "done", "until": "done"}
# Openers the reserved-word peel stops at (they are commands' first words, not prefixes).
_COMMAND_OPENERS = {"for": "done", "select": "done", "case": "esac"}
_CLOSERS = frozenset({"}", "fi", "done", "esac"})
# Stands where a `<(…)` that writes no known file was: NUL keeps it from ever naming a path.
PROCESS_SUBSTITUTION = "\x00<(…)"
# A `<`, or a stdout redirection, right before a `(` opens a process substitution (`>>(…)`).
_SUBSTITUTION_OPENERS = frozenset({"<", ">", ">>", ">|", "1>", "1>>", "1>|", "&>", "&>>"})
# Operators that carry a command on past the end of its line.
_CONTINUING_OPERATORS = frozenset({"|", "|&", "&&", "||"})
# Stands, while tokenizing, for the `&` of `>&`, `<&`, `&>` and `&>>` (a private-use character).
_REDIRECTION_AMPERSAND = "\ue000"
_REDIRECTION_PIPE = "\ue001"  # likewise the `|` of `>|`

Context = tuple[list[str], bool, list[str]]
# Each process substitution of a command: (its position, or None for a `>(…)`, files it writes).
_Subs = list[tuple[Optional[int], list[str]]]


# Bash's `{NAME}>x` / `{NAME}<x` / `{NAME}>&N`: allocate a descriptor into $NAME (a whole word only).
# NAME may be an array element (`{a[1]}>x`, `{m["k k"]}>&2`, `{a[b[1]]}>x`): bash 5.2 accepts those too.
FD_VARIABLE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?:\[.+?\])?\}", re.DOTALL)
# The start of an array-element descriptor word that a space, `$(`/`$((` or a backtick cut off
# (`{a[$((1+1))]}>x`, ``{a[`echo 1`]}>x``); the tail ending in `]}` glued to `<`/`>` is the rest.
_FD_VARIABLE_HEAD = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\[")
# Stands where such a tail was: a whole `{NAME}` word, so the redirection stays one word.
FD_VARIABLE_TAIL = "{_}"


def split_glued_redirections(line: str) -> str:
    """*line* with a space before each unquoted, unescaped ``<``/``>`` glued to a word that is
    not a bare descriptor number or ``{NAME}`` (``python3 -<f.py`` is ``python3 - <f.py``; ``2>x``
    and ``{fd}>x`` stay). A quoted ``<`` is text (``bash -c 'python3<f.py'`` keeps one word), and
    the operator's own second character (``<<``, ``>>``, ``<>``) is never split off. The ``&``/``|``
    of an unquoted ``>&``, ``<&`` or ``>|`` is escaped, so the tokenizer keeps the operator one word
    instead of cutting the command there (``2>&1 bash x.sh`` is not ``2>`` then ``1 bash x.sh``).

    An array-element descriptor whose subscript the tokenizer cuts (``{a[$((1+1))]}>x bash x.sh``,
    ``{a[`echo 1`]}>x bash x.sh``) fails closed: the text from the command's first unterminated
    ``{NAME[`` head (or, without one, the word itself) to the word ending in ``]}`` glued to the
    operator becomes ``{_}`` (still a descriptor-variable redirection, so the command word after it
    is read). That text is not dropped: it becomes a command of its own at the end of the line
    (before a ``#`` comment), or at its start when the line ends in a control operator (``|``,
    ``&&``, ...), so no operand is added and no pipe is cut. A head with no such tail is an
    ordinary word and changes nothing (``bash -s {a[x < f.sh`` keeps its stdin)."""

    out: list[str] = []
    word = ""
    word_out = 0  # where *word* starts in *out*
    head_out: Optional[int] = None  # where this command's first unterminated `{NAME[` head starts
    carried: list[str] = []  # rewritten descriptor text, re-emitted as commands of their own
    comment = ""  # the line's trailing `#` comment, verbatim
    in_single = in_double = escape = False
    operator = False  # the previous character is an unquoted, unescaped `<`/`>`
    for index, ch in enumerate(line):
        after_operator, operator = operator, False
        if escape:
            escape = False
        elif ch == "\\" and not in_single:
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if after_operator and (ch == "&" or (ch == "|" and word[-1] == ">")):
                out.append("\\" + ch)
                word += ch
                continue
            if ch == "&" and word and word[-1] not in "<>" and line[index + 1: index + 2] == ">":
                out.append(" ")
            if ch.isspace() or ch in CONTROL_CHARS:
                if ch in CONTROL_CHARS:
                    head_out = None
                elif (head_out is None and _FD_VARIABLE_HEAD.match(word)
                        and not FD_VARIABLE.match(word)):
                    head_out = word_out
                out.append(ch)
                word = ""
                word_out = len(out)
                continue
            if ch == "#" and not word:
                comment = line[index:]
                break
            if ch in "<>":
                operator = True
                if (word and not word.isdigit() and not FD_VARIABLE.fullmatch(word)
                        and word[-1] not in "<>"):
                    if word.endswith("]}"):
                        start = word_out if head_out is None else head_out
                        carried.append("".join(out[start:]))
                        out[start:] = [FD_VARIABLE_TAIL]
                        word = FD_VARIABLE_TAIL
                        word_out = start
                        head_out = None
                    else:
                        out.append(" ")
                        word = ""
                        word_out = len(out)
        out.append(ch)
        word += ch
    body = "".join(out)
    if carried:
        own = " ; ".join(carried)
        stripped = body.rstrip()
        if stripped and stripped[-1] in CONTROL_CHARS and not stripped.endswith("\\|"):
            body = f"{own} ; {body}"
        else:
            body = f"{stripped} ; {own}" + (" " if comment else "")
    return body + comment


def protect_redirection_ampersands(line: str) -> str:
    """*line* with the ``&`` of each unquoted, unescaped redirection operator (``2>&1``, ``<&0``,
    ``&>f``, ``&>>f``) swapped for a word character, so a tokenizer splitting on ``&`` keeps the
    redirection whole instead of reading a background ``&``. ``&&``, ``|&`` and a lone ``&``
    keep theirs. The ``|`` of ``>|f`` is swapped too: it is no pipe. Undo per token with
    ``restore_redirection_ampersands``."""
    out: list[str] = []
    in_single = in_double = escape = False
    previous = ""  # the last character when it was unquoted and unescaped
    for position, ch in enumerate(line):
        shell_character = False
        if escape:
            escape = False
        elif ch == "\\" and not in_single:
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            shell_character = True
            after = line[position + 1: position + 2]
            if ch == "&" and (previous in ("<", ">")
                              or (after == ">" and previous not in ("&", "|"))):
                ch = _REDIRECTION_AMPERSAND
            elif ch == "|" and previous == ">":  # `>|f` writes f; it pipes nothing
                ch = _REDIRECTION_PIPE
        previous = ch if shell_character else ""
        out.append(ch)
    return "".join(out)


def restore_redirection_ampersands(token: str) -> str:
    """*token* as written (see ``protect_redirection_ampersands``)."""
    return token.replace(_REDIRECTION_AMPERSAND, "&").replace(_REDIRECTION_PIPE, "|")


def join_continued_lines(token_lists: Iterable[list[str]]) -> Iterator[list[str]]:
    """Each line's tokens, a line ending in ``|``, ``|&``, ``&&`` or ``||`` joined with the
    lines after it (``cat f.py |`` newline ``python3`` is one pipeline), blank lines included."""
    carried: list[str] = []
    for tokens in token_lists:
        tokens = carried + tokens
        last = tokens[-1] if tokens else ""
        if last and set(last) <= CONTROL_CHARS \
                and _CONTROL_OPERATOR.findall(last)[-1] in _CONTINUING_OPERATORS:
            carried = tokens
            continue
        carried = []
        yield tokens
    if carried:
        yield carried


@dataclass
class _Group:
    closer: str
    feed: list[str]
    # Open order on the line: the key a redirection read after the closer is recorded under.
    ordinal: int = -1
    output: list[str] = field(default_factory=list)
    # A process substitution suspends the command it stands in: (words, call_argument, subs,
    # feed, its `>(…)` ordinals); the group's own feed is what its body reads.
    outer: Optional[tuple[list[str], bool, _Subs, list[str], list[int]]] = None


def _opened_groups(segment: list[str], peel_reserved_words: Callable[[list[str]], int]) -> list[str]:
    """Closers of the compound commands *segment* opens (``while { if`` opens three)."""
    index = peel_reserved_words(segment)
    closers = [_LEADING_OPENERS[word] for word in segment[:index] if word in _LEADING_OPENERS]
    if index < len(segment) and segment[index] in _COMMAND_OPENERS:
        closers.append(_COMMAND_OPENERS[segment[index]])
    return closers


def _substituted_variants(segment: list[str], subs: _Subs) -> list[list[str]]:
    """One reading of *segment* per file a process substitution writes, each in its place (the
    others left as ``PROCESS_SUBSTITUTION``); *segment* itself when none writes a file. A ``>(…)``
    (position ``None``) writes to the command's stdout instead."""
    variants = [segment[:position] + [name] + segment[position + 1:]
                for position, files in subs if position is not None for name in files]
    return variants or [segment]


class _LineWalk:
    """State of one line's walk; ``contexts`` yields ``(segment, call_argument, piped_files)``."""

    def __init__(self, cat_operands, cat_relays_stdin, opens_call_arguments, peel_reserved_words,
                 stdin_redirections, redirected: Optional[dict[int, list[str]]] = None,
                 writers: Optional[dict[int, list[list[str]]]] = None) -> None:
        self.cat_operands = cat_operands
        self.cat_relays_stdin = cat_relays_stdin
        self.opens_call_arguments = opens_call_arguments
        self.peel_reserved_words = peel_reserved_words
        self.stdin_redirections = stdin_redirections
        # Files each group's own redirection feeds (`{ …; } < f`), by ordinal: an earlier walk of
        # the line read them after the closer, which this walk reaches only after the commands.
        self.redirected = redirected or {}
        self.found_redirections: dict[int, list[str]] = {}
        # The whole command each `>(…)` stands in, by ordinal (`cat > >(python3) f.py`): an
        # earlier walk read the words after the `)`, which this walk reaches only after the body.
        self.writers = writers or {}
        self.found_writers: dict[int, list[list[str]]] = {}
        self.output_subs: list[int] = []  # ordinals of the current command's `>(…)`
        self.groups_open = False  # whether the current segment's own groups are open
        self.opened = 0
        # (ordinal, words to skip) of the group closed right before the current segment: its
        # words past the closer are that group's redirections (`} < f`, `) < f`, `done < f`).
        self.closing: Optional[tuple[int, int]] = None
        self.segment: list[str] = []
        self.subs: _Subs = []
        self.call_argument = False
        self.feed: list[str] = []
        # What the last finished command (or closed group) wrote, until an operator routes it:
        # into the next command's stdin on `|`, else into the enclosing group's output.
        self.pending: list[str] = []
        self.closed: Optional[list[str]] = None
        self.groups: list[_Group] = []

    def open_group(self, closer: str, outer=None, feed: Optional[list[str]] = None) -> _Group:
        """A new group on the current feed, unless its own redirection replaces that. The
        commands after the opener (``{ bash``, ``{ { bash``) run on the group's feed. A process
        substitution (*outer* set) reads *feed* when given (``>(…)``), else the current feed."""
        ordinal, self.opened = self.opened, self.opened + 1
        if outer is not None:
            group = _Group(closer, self.feed if feed is None else feed, ordinal=ordinal,
                           outer=outer)
        else:
            self.feed = self.redirected.get(ordinal) or self.feed
            group = _Group(closer, self.feed, ordinal=ordinal)
        self.groups.append(group)
        return group

    def flush(self) -> Iterator[Context]:
        if self.closing is not None:
            ordinal, skip = self.closing
            files = self.stdin_redirections(self.segment[skip:])
            if files:
                self.found_redirections[ordinal] = files
            self.closing = None
        # Open this segment's groups first: its own command (`{ bash`, `while read l`) runs in them.
        self.open_segment_groups()
        written: list[str] = [name for position, files in self.subs if position is None
                              for name in files]  # `echo >(cat f.py) | python3`
        variants = _substituted_variants(self.segment, self.subs)
        for ordinal in self.output_subs:
            self.found_writers[ordinal] = variants
        for variant in variants:
            yield variant, self.call_argument, self.feed
            written += self.writes(variant)
        self.pending = self.closed if self.closed is not None else written
        self.closed = None
        self.segment, self.subs, self.output_subs = [], [], []
        self.groups_open = False

    def open_segment_groups(self) -> None:
        """Open the compound commands the current segment starts, once (at its first process
        substitution or at its end): ``{ tee >(python3); } < f.py`` feeds the ``>(…)`` too."""
        if not self.groups_open:
            for closer in _opened_groups(self.segment, self.peel_reserved_words):
                self.open_group(closer)
            self.groups_open = True

    def writes(self, segment: list[str]) -> list[str]:
        """Files *segment* writes to its stdout. A bare ``cat`` (or ``cat -``, ``tee``) writes
        what it is fed: ``cat f | cat | bash``, ``{ cat; } < f``, ``cat f | tee log | bash``."""
        written = self.cat_operands(segment)
        # Only asked with a feed: the 8000-line heredoc perf case keeps its timing.
        if self.feed and self.cat_relays_stdin(segment):
            written = written + self.feed
        return written

    def route(self) -> None:
        if self.groups:
            self.groups[-1].output.extend(self.pending)
        self.pending = []

    def enclosing_feed(self) -> list[str]:
        return self.groups[-1].feed if self.groups else []

    def resume(self, group: _Group) -> None:
        """Back in the command a closed ``<(…)`` stands in."""
        if group.outer is None:
            return
        self.segment, self.call_argument, self.subs, self.feed, self.output_subs = group.outer
        self.groups_open = True  # opened before the substitution suspended it
        output = group.ordinal in self.output_subs  # a `>(…)` body writes the command's stdout
        self.subs.append((None if output else len(self.segment), group.output))
        self.segment.append(PROCESS_SUBSTITUTION)

    def word(self, token: str) -> None:
        # A closer right after the closers that started this segment closes the next group too
        # (`{ bash; { true; } } < f`, `… { true; } fi`): bash ends a compound command at its
        # closer, so the run's redirection belongs to the outermost group it closed.
        run = self.closing[1] if self.closing is not None else 0
        if len(self.segment) == run and token in _CLOSERS and self.groups \
                and self.groups[-1].closer == token:
            if run:
                # What the group closed just before wrote is its enclosing group's output.
                self.pending = self.closed or []
            self.route()
            group = self.groups.pop()
            self.closed = group.output
            self.closing = (group.ordinal, run + 1)
            self.feed = self.enclosing_feed()
        self.segment.append(token)

    def operator(self, operator: str) -> Iterator[Context]:
        if not self.segment:
            # A closed `( … )` writes on until an operator routes it; its redirections
            # (`(cat f.py) 2>&1 | python3`) are a segment of their own.
            self.closed = None
        if operator == "(" and self.segment and self.segment[-1] in _SUBSTITUTION_OPENERS:
            # `>(…)` reads what its whole command writes (`cat f.py > >(python3)`, `cat >
            # >(python3) f.py`, `tee >(python3) < f.py`), the words after it from the probe walk.
            self.open_segment_groups()  # `{ tee >(python3); } < f.py`
            feed, ordinal = self.feed, self.opened
            if self.segment.pop() != "<":
                self.output_subs.append(ordinal)
                variants = _substituted_variants(self.segment, self.subs)
                feed = [name for segment in [*variants, *self.writers.get(ordinal, [])]
                        for name in self.writes(segment)]
            self.open_group(")", outer=(self.segment, self.call_argument, self.subs, self.feed,
                                        self.output_subs), feed=feed)
            self.segment, self.subs, self.call_argument, self.feed = [], [], False, feed
            self.output_subs, self.groups_open = [], False
            return
        next_is_call = operator == "(" and bool(self.segment) \
            and self.opens_call_arguments(self.segment)
        if self.segment:
            yield from self.flush()
        self.closing = self.closed = None
        if operator in ("|", "|&"):
            self.feed, self.pending = self.pending, []
        elif operator == "(":
            self.open_group(")")
        else:
            self.route()
            if operator == ")" and self.groups and self.groups[-1].closer == ")":
                group = self.groups.pop()
                if group.outer is not None:
                    self.resume(group)
                    return
                # Its redirections (`) < f`, `) 2>/dev/null`) flush as a segment of their own,
                # which writes what the group wrote.
                self.pending = self.closed = group.output
                self.closing = (group.ordinal, 0)
            self.feed = self.enclosing_feed()
        self.call_argument = next_is_call

    def finish(self) -> Iterator[Context]:
        while True:
            if self.segment:
                yield from self.flush()
            # An unclosed `<(` still runs the command it stands in.
            index = next((i for i in range(len(self.groups) - 1, -1, -1)
                          if self.groups[i].outer is not None), None)
            if index is None:
                return
            group = self.groups[index]
            del self.groups[index:]
            self.resume(group)


def iter_line_segment_contexts(
    tokens: list[str], *,
    cat_operands: Callable[[Optional[list[str]]], list[str]],
    cat_relays_stdin: Callable[[list[str]], bool],
    opens_call_arguments: Callable[[list[str]], bool],
    peel_reserved_words: Callable[[list[str]], int],
    stdin_redirections: Callable[[list[str]], list[str]],
) -> Iterator[Context]:
    """``(segment, call_argument, piped_files)`` for one line's shell *tokens* (see
    ``cron.lifecycle_guard._iter_segment_contexts``). *cat_operands* names the files a segment
    writes to its stdout, *cat_relays_stdin* whether it also writes the feed on its stdin (a
    bare ``cat``, ``tee``); *stdin_redirections* the files some words redirect to stdin.

    A group's own redirection follows its closer, after its commands were read, so the line is
    walked twice: the first walk only collects those redirections, the second feeds each group
    from its own. Both walks open the same groups in the same order: feeds never change that."""
    callables = (cat_operands, cat_relays_stdin, opens_call_arguments, peel_reserved_words,
                 stdin_redirections)
    probe = _LineWalk(*callables)
    for _ in _walk(probe, tokens):
        pass
    yield from _walk(_LineWalk(*callables, redirected=probe.found_redirections,
                               writers=probe.found_writers), tokens)


def _walk(walk: _LineWalk, tokens: list[str]) -> Iterator[Context]:
    for token in tokens:
        if token and set(token) <= CONTROL_CHARS:
            for operator in _CONTROL_OPERATOR.findall(token):
                yield from walk.operator(operator)
        else:
            walk.word(token)
    yield from walk.finish()
