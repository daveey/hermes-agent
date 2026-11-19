"""Fail closed when an unevaluated command output supplies a shell's code/file.

Never execute substitutions or trust builtin-looking producer names: functions can
replace them. Arithmetic output is separately marked, since it is numeric rather
than arbitrary shell code. Existing walks still inspect the substitution bodies.
"""
from __future__ import annotations

import re

from cron.lifecycle_guard_substitutions import separate_substitutions
from cron.lifecycle_guard_variables import _ASSIGNMENT, _DECLARATIONS

_OUTPUT = '\x00command-output'
_VARIABLE_REFERENCE = re.compile(r'\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})')


def _input_words(
    words: list[str], opened: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Reuse final-fd binding semantics, treating here-string bytes as an input."""
    from cron import lifecycle_guard as guard
    from cron.lifecycle_guard_words import redirection_parts

    inputs = []
    for word in words:
        match = redirection_parts(word, guard._REDIRECTION)
        if match is not None and match[1] == '<<<':
            fd, _, target = match
            word = fd + '<' + target
        inputs.append(word)
    return guard._split_redirections(inputs, opened=opened)


def _unanchored_splice(pattern: str) -> bool:
    """True when *pattern* is only ``.*`` separators, so it matches every name."""
    return pattern.replace(".*", "") == ""


def _shell_input_is_computed(
    view: list[str], piped: list[str], *, opened: dict[str, str] | None = None,
    depth: int = 0, anchored_only: bool = False,
) -> bool:
    from cron import lifecycle_guard as guard

    # An empty output can join a shell name or a transparent wrapper's name.
    # Keep the original words for input uncertainty and scan the empty-output
    # interpretation additively; this is not a claim about the actual output.
    bindings = dict(opened or {})
    if piped:
        bindings['0'] = '\n'.join(piped)
    # Redirections are syntax, never executable words (including before or
    # between wrappers). Apply them in order before locating the consumer.
    view, _ = _input_words(view, bindings)
    empty = [word.replace(_OUTPUT, '') for word in view]
    index = guard._executed_command_index(empty)
    if index is None:
        return False
    while not empty[index] and _OUTPUT in view[index]:
        # Unknown programs with shell invocation syntax are possible shells.
        arguments, stdin = _input_words(view[index + 1:], dict(bindings))
        _, code, reads_stdin = guard._shell_options_end(arguments, 'bash')
        if code or reads_stdin or stdin or piped:
            return True
        # Also consider an absent word before a literal shell, without
        # treating an empty prefix before Python as a shell consumer.
        index += 1
        if index >= len(view):
            return False
    name = guard._executable_name(empty[index])
    arguments, stdin = _input_words(view[index + 1:], bindings)
    if name in {'.', 'source', 'eval'}:
        selected = arguments if name == 'eval' else arguments[:1]
        return _OUTPUT in view[index] or any(_OUTPUT in word for word in selected)
    if _OUTPUT in view[index]:
        # The output may fill a *nonempty* executable splice. Only classify
        # names compatible with a shell/wrapper, not py$(...)thon or data.
        pattern = '.*'.join(re.escape(part) for part in guard._executable_name(view[index]).split(_OUTPUT))
        shells = guard._SHELL_EXECUTABLES | {'.', 'source', 'eval'}
        # A placeholder-only basename (`s/<out>/<out>/` → Path.name is the
        # placeholder → pattern `.*`) matches every shell. That is evidence on
        # a real command word, and not evidence inside a raw quote the parent
        # does not feed to a shell. Anchored splices (`b<out>ash`) still match.
        narrowed = anchored_only and _unanchored_splice(pattern)
        if not narrowed and any(re.fullmatch(pattern, candidate) for candidate in shells):
            return True
        # A computed wrapper name is not itself a shell sink. Interpret each
        # compatible wrapper with the existing option/operand peeling rules.
        if not narrowed:
            for wrapper in guard._TRANSPARENT_COMMAND_PREFIXES:
                if re.fullmatch(pattern, wrapper):
                    candidate = view[:index] + [wrapper] + view[index + 1:]
                    consumer = guard._executed_command_index(candidate)
                    if consumer is not None and guard._executable_name(candidate[consumer]) in shells:
                        return True
                    if _shell_input_is_computed(
                            candidate, [], opened=bindings, depth=depth,
                            anchored_only=anchored_only):
                        return True
    if name not in guard._SHELL_EXECUTABLES:
        return False
    operand, code, reads_stdin = guard._shell_options_end(arguments, name)
    # Unknown option output may change the option layout or inject a code input.
    selected = arguments[:operand]
    if code or not reads_stdin:
        selected += arguments[operand:operand + 1]
    if not code and (reads_stdin or operand >= len(arguments)):
        selected += stdin
    if any(_OUTPUT in word for word in selected):
        return True
    if code and operand < len(arguments) and any(_OUTPUT in word for word in bindings.values()):
        # -c selects code, but that code can launch another stdin shell. Carry
        # the final fd map into its commands; explicit redirects still override.
        if depth >= 16:
            return True
        return _contexts_are_computed(
            arguments[operand], opened=bindings, depth=depth + 1,
            anchored_only=anchored_only)
    return False


def _contexts_are_computed(command, *, opened=None, depth=0, alias_names=frozenset(),
                           anchored_only=False) -> bool:
    """Attach descriptor provenance to the existing scope/pipeline feed walk.

    Flattened segment contexts cannot carry persistent redirects: a child close
    must not close the parent's fd, nor may a child open taint its parent. Reuse
    the feed walk's group and operator events rather than parsing shell syntax
    for a second time.
    """
    from cron import lifecycle_guard as guard
    from cron.lifecycle_guard_groups import _LineWalk, _walk, heredoc_scripts, join_continued_lines

    def peel(words):
        # Decoded quoted keywords are command names, not shell grammar. The
        # shared legacy peeler otherwise turns `"if" exec 3<&-` into a close.
        end = next((i for i, word in enumerate(words) if getattr(word, 'quoted', False)), len(words))
        return guard._peel_reserved_words(words[:end])

    def join(left, right):
        # Only output provenance matters here. A may-join must not erase a
        # computed binding when the other path closes/rebinds that descriptor.
        result = dict(left)
        for fd, value in right.items():
            if fd not in result or _OUTPUT in value:
                result[fd] = value
        return result

    loop_entries = {}
    function_effects = {}
    function_entries = {}

    class DescriptorWalk(_LineWalk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.bindings = dict(opened or {})
            self.command_start = dict(self.bindings)
            self.pipeline = None
            self.fd_frames = []
            self.computed = False
            self.conditional = False
            self.function_pending = None
            self.function_header = False
            self.case_patterns = set()
            self.last_operator = None
            self.branch_exits = {}
            self.local_aliases = set(alias_names)

        def open_group(self, closer, outer=None, feed=None):
            function = self.function_pending if outer is None else None
            self.function_pending = None
            group = super().open_group(closer, outer=outer, feed=feed)
            self.fd_frames.append((group, dict(self.bindings), self.pipeline, self.command_start,
                                   self.conditional, function))
            if closer == 'esac':
                self.case_patterns.add(group.ordinal)
            if function:
                self.bindings = join(self.bindings, function_entries.get(function, {}))
            if closer == 'done':
                self.bindings = join(self.bindings, loop_entries.get(group.ordinal, {}))
            self.command_start = dict(self.bindings)
            self.pipeline = None
            self.conditional = False
            return group

        def open_segment_groups(self):
            if self.groups_open:
                return
            prefix = self.segment[:peel(self.segment)]
            if prefix and prefix[0] == 'function':
                self.function_pending = str(self.segment[1]) if len(self.segment) > 1 else None
            for word in prefix:
                closer = {'{': '}', 'if': 'fi', 'while': 'done', 'until': 'done'}.get(word)
                if closer:
                    self.open_group(closer)
            rest = self.segment[len(prefix):]
            if rest and not getattr(rest[0], 'quoted', False):
                closer = {'for': 'done', 'select': 'done', 'case': 'esac'}.get(rest[0])
                if closer:
                    self.open_group(closer)
            self.groups_open = True

        def restore_groups(self):
            while self.fd_frames and not any(self.fd_frames[-1][0] is g for g in self.groups):
                group, parent, pipeline, start, conditional, function = self.fd_frames.pop()
                if self.pipeline is not None:
                    self.bindings = dict(self.pipeline)
                if function:
                    # Definitions do not run. Save their may-effects separately
                    # and reapply them only when a command invokes the name.
                    effects = function_effects.setdefault(function, {})
                    # Literal file bindings also identify a computed command
                    # as a possible stdin shell. Retain them monotonically,
                    # without promoting harmless files to output provenance.
                    function_effects[function] = join(effects, self.bindings)
                    self.bindings = parent
                elif group.closer == ')':
                    self.bindings = parent
                else:
                    self.bindings = join(self.branch_exits.pop(group.ordinal, {}), self.bindings)
                    if group.closer == 'done':
                        entry = loop_entries.setdefault(group.ordinal, {})
                        # Literal file inputs also identify a computed program
                        # as a possible shell on the next iteration. Keep their
                        # provenance rather than upgrading them to output code.
                        loop_entries[group.ordinal] = join(entry, self.bindings)
                    if function or conditional or group.closer in ('fi', 'done', 'esac'):
                        # Definitions/branches/zero-iteration loops cannot
                        # certify a close. Their possible opens remain visible
                        # conservatively, including a later function invocation.
                        self.bindings = join(parent, self.bindings)
                self.pipeline, self.command_start = pipeline, start
                self.conditional = conditional

        def word(self, token):
            syntax = not getattr(token, 'quoted', False)
            if self.fd_frames and self.fd_frames[-1][0].ordinal in self.case_patterns:
                if not (syntax and token == 'esac'):
                    return
                self.case_patterns.discard(self.fd_frames[-1][0].ordinal)
            if syntax and token in ('else', 'elif') and not self.segment and self.fd_frames:
                # The next branch starts with the entry state, not the preceding
                # branch's strong close. Retain both possible outcomes.
                group, parent, *_ = self.fd_frames[-1]
                self.branch_exits[group.ordinal] = join(
                    self.branch_exits.get(group.ordinal, {}), self.bindings)
                self.bindings = dict(parent)
            if not syntax and token in ('}', 'fi', 'done', 'esac'):
                self.segment.append(token)
            else:
                super().word(token)
            self.restore_groups()

        def flush(self):
            for segment, call_argument, piped in super().flush():
                if not call_argument:
                    views = guard._command_segment_views(segment)
                    for view in views:
                        self.computed |= _shell_input_is_computed(
                            view, piped, opened=self.bindings, depth=depth,
                            anchored_only=anchored_only)
                    candidate = dict(self.bindings)
                    arguments, _ = _input_words(segment[peel(segment):], candidate)
                    index = guard._executed_command_index(arguments)
                    if index is not None:
                        name = arguments[index]
                        # Dynamic dispatch can invoke any locally defined helper.
                        # Keep these may-effects local to descriptor uncertainty;
                        # they are not literal variable/alias value candidates.
                        if name == 'alias':
                            self.local_aliases.update(
                                word.split('=', 1)[0] for word in arguments[index + 1:] if '=' in word)
                        dynamic = ('$' in name or _OUTPUT in name or name in self.local_aliases)
                        if (name == 'eval' and arguments[index + 1:]) or dynamic:
                            targets = function_effects
                        else:
                            targets = {name: function_effects[name]} if name in function_effects else {}
                        for target, effects in targets.items():
                            function_entries[target] = join(
                                function_entries.get(target, {}), candidate)
                            self.bindings = join(self.bindings, effects)
                    if arguments == ['exec']:
                        self.bindings = join(self.bindings, candidate) if self.conditional else candidate
                yield segment, call_argument, piped

        def operator(self, operator):
            # Case patterns are not subshells. The shared flat walker splits
            # ;& and ;;& into punctuation events; join every arm at those exits
            # and seed later arms with both entry and fallthrough provenance.
            case_words = self.segment[peel(self.segment):]
            if (case_words[:1] == ['case']
                    and not getattr(case_words[0], 'quoted', False)):
                self.open_segment_groups()
                self.segment = []
                self.groups_open = False
            case = self.fd_frames[-1] if self.fd_frames and self.fd_frames[-1][0].closer == 'esac' else None
            if case:
                group, parent, *_ = case
                if group.ordinal in self.case_patterns:
                    if operator == ')':
                        self.case_patterns.discard(group.ordinal)
                    self.last_operator = operator
                    return
                if operator == ';;' or (operator == '&' and self.last_operator == ';'):
                    yield from self.flush()
                    self.branch_exits[group.ordinal] = join(
                        self.branch_exits.get(group.ordinal, {}), self.bindings)
                    self.bindings = join(parent, self.branch_exits[group.ordinal])
                    self.case_patterns.add(group.ordinal)
                    self.last_operator = operator
                    return
            self.last_operator = operator
            # Save before the first member runs; a group may already have run
            # by the time its following pipe operator is encountered.
            start = dict(self.command_start)
            header_words = self.segment[peel(self.segment):]
            # Bash (outside POSIX mode) permits punctuation in function names.
            # This is a may-definition, not an identifier-validity certificate.
            header = (operator == '(' and len(header_words) == 1
                      and bool(header_words[0])
                      and not any(ch in header_words[0] for ch in '$<>='))
            if operator == '(' and self.segment[:1] == ['function']:
                header = True
            name = (str(self.segment[1]) if self.segment[:1] == ['function'] and len(self.segment) > 1
                    else str(header_words[0]) if header_words else None)
            end_header = operator == ')' and self.function_header
            yield from super().operator(operator)
            self.restore_groups()
            if header:
                self.function_header = name
            elif end_header:
                self.function_pending = self.function_header
                self.function_header = False
            if operator in ('&&', '||'):
                self.conditional = True
            elif operator not in ('(', ')'):
                self.conditional = False
            if operator in ('|', '|&'):
                if self.pipeline is None:
                    self.pipeline = start
                self.bindings = dict(self.pipeline)
                self.command_start = dict(self.bindings)
            elif operator not in ('(', ')'):
                if self.pipeline is not None:
                    self.bindings = dict(self.pipeline)
                    self.pipeline = None
                elif operator == '&':
                    self.bindings = start
                self.command_start = dict(self.bindings)

    callables = (guard._cat_operands, guard._cat_relays_stdin,
                 lambda segment: guard._opens_call_arguments(segment) and alias_names.isdisjoint(segment),
                 peel, lambda words: _input_words(words)[1])
    for script in heredoc_scripts(command):
        tokens = [token for line in join_continued_lines(guard._iter_command_token_lists(script))
                  for token in (*line, ';')]
        # Preserve the feed tracker's look-ahead for redirects after a group
        # closer and writers surrounding a process substitution.
        probe = _LineWalk(*callables)
        for _ in _walk(probe, tokens):
            pass
        loop_entries.clear()
        function_effects.clear()
        function_entries.clear()
        while True:
            previous = {ordinal: dict(fds) for ordinal, fds in loop_entries.items()}
            previous_functions = {name: dict(fds) for name, fds in function_effects.items()}
            previous_entries = {name: dict(fds) for name, fds in function_entries.items()}
            walk = DescriptorWalk(*callables, redirected=probe.found_redirections,
                                  writers=probe.found_writers)
            for _ in _walk(walk, tokens):
                if walk.computed:
                    return True
            if walk.computed:
                return True
            if (loop_entries == previous and function_effects == previous_functions
                    and function_entries == previous_entries):
                break
    return False


def _mark_variables(text: str, uncertain: set[str]) -> str:
    """Replace active scalar references only, not single-quoted/escaped data."""
    result = []
    single = double = escaped = False
    index = 0
    while index < len(text):
        ch = text[index]
        if not single and not escaped and ch == '$':
            match = _VARIABLE_REFERENCE.match(text, index)
            if match and (match[1] or match[2]) in uncertain:
                result.append(_OUTPUT)
                index = match.end()
                continue
        result.append(ch)
        if escaped:
            escaped = False
        elif ch == '\\' and not single:
            escaped = True
        elif ch == "'" and not double:
            single = not single
        elif ch == '"' and not single:
            double = not double
        index += 1
    return ''.join(result)


def computed_shell_input(command: str, alias_names: frozenset[str], *,
                         anchored_only: bool = False) -> tuple[bool, str | None]:
    """Return uncertainty and an additive empty-output scan, only for executed text."""
    from cron import lifecycle_guard as guard
    from cron.lifecycle_guard_lines import strip_shell_comments
    from cron.lifecycle_guard_substitutions import normalize_ansi_c_quotes

    outer, _ = separate_substitutions(command, output_placeholder=_OUTPUT)
    if _OUTPUT not in outer:
        return False, None
    # Normalize only this uncertainty view. Keep the original empty-output
    # interpretation separate for the existing executable-evidence walk.
    binding_view = strip_shell_comments(normalize_ansi_c_quotes(outer))
    # Uncertainty is a separate, local may-fact, never a literal value in the
    # shared VariableCandidates store. Join assignments conservatively (also
    # through functions/branches), and propagate scalar aliases to a fixed point.
    uncertain: set[str] = set()
    # Every growing round adds a name from a finite set of assignment LHSs;
    # exhausting an arbitrary round budget is not evidence of a shell sink.
    while True:
        marked = _mark_variables(binding_view, uncertain) if uncertain else binding_view
        contexts = list(guard._iter_segment_contexts(marked, alias_names))
        previous = set(uncertain)
        for segment, call_argument, _ in contexts:
            if call_argument:
                continue
            words = segment[guard._peel_reserved_words(segment):]
            words = guard._split_redirections(words)[0]
            bindings = words[1:] if words and words[0] in _DECLARATIONS else words
            for word in bindings:
                match = _ASSIGNMENT.fullmatch(word)
                if match:
                    if _OUTPUT in match[2]:
                        uncertain.add(match[1])
                elif not word.startswith('-'):
                    break
        if previous == uncertain:
            break
    if _contexts_are_computed(marked, alias_names=alias_names, anchored_only=anchored_only):
        return True, None
    return False, outer.replace(_OUTPUT, '')


def scan_computed_shell_input(command, *, executed, budget, recurse, cwd, source,
                              enabled=True, anchored_only=False) -> bool:
    """Add uncertainty/empty-output coverage after the ordinary evidence walks.

    *enabled* is False only inside the text of a directly invoked Python-shebang
    file. *anchored_only* drops placeholder-only basename splices; callers set
    it for a raw quote the parent does not feed to a shell."""
    if not executed or not enabled:
        return False
    uncertain, empty_output = computed_shell_input(
        command, budget.alias_names or frozenset(), anchored_only=anchored_only)
    if uncertain:
        budget.refusal = 'the shell input depends on unresolved command substitution output'
        return True
    # This guessed interpretation is executable evidence, not a literal binding
    # source: HB=$(producer) must not become a shared, invented HB='' fact.
    return empty_output is not None and recurse(
        empty_output, cwd, executed, source, variable_source=False)
