"""Conservative, additive execution candidates for scalar shell variables.

This is not a shell-state evaluator: retain every possible assignment and match it
only to execution operands, including function bodies discovered in sourced files.
Never use this approximation to mask input or prove that a command is safe.
"""
from __future__ import annotations

import re
import shlex

_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_BINDING_HINT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=")
_VARIABLE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})\Z")
_DECLARATIONS = frozenset({'export', 'readonly', 'local', 'declare', 'typeset'})
_SHELLS = frozenset({'bash', 'sh', 'zsh', 'dash', 'ksh', 'source', '.'})


def scan_variable_payloads(command, *, cwd, executed, budget, recurse, source, variable_source):
    if executed and variable_source:
        for payload, payload_cwd in budget.variable_candidates.payloads(command, cwd):
            if recurse(payload, payload_cwd, True, source):
                return True
    return False


def scan_recursive_payloads(command, nested_texts, *, cwd, executed, budget,
                            covered_scope, recurse, source):
    from cron.lifecycle_guard_output import iter_recursive_payloads

    for payload, nested, covered, variable_source in iter_recursive_payloads(
            command, nested_texts, cwd=cwd, executed=executed,
            budget=budget, covered_scope=covered_scope):
        if recurse(payload, cwd, executed, source, nested=nested, covered_quotes=covered,
                   variable_source=variable_source):
            return True
    return False


class VariableCandidates:
    def __init__(self):
        self.values: dict[str, set[str]] = {}
        self.sinks: set[tuple[str, tuple[str, ...], int, str | None]] = set()
        self.emitted: set[tuple[str, str | None]] = set()

    def payloads(self, command, cwd):
        from cron import lifecycle_guard as guard

        if '$' not in command and not _BINDING_HINT.search(command):
            return
        for segment in guard._iter_command_segments(command):
            words = segment[guard._peel_reserved_words(segment):]
            words = guard._split_redirections(words)[0]
            # Assignment arguments of ordinary commands are data, not bindings.
            assignment_words = words[1:] if words and words[0] in _DECLARATIONS else words
            for word in assignment_words:
                match = _ASSIGNMENT.fullmatch(word)
                if match:
                    # Dynamic values are not literal bindings: substitutions are
                    # traversed by the ordinary walk, never reinterpreted here.
                    if not any(char in match[2] for char in '$`'):
                        self.values.setdefault(match[1], set()).add(match[2])
                elif not word.startswith('-'):
                    break
            index = guard._executed_command_index(words)
            if index is None:
                continue
            argv = tuple(words[index:])
            name = guard._executable_name(argv[0])
            positions = range(len(argv)) if name in _SHELLS | {'eval'} else range(1)
            for position in positions:
                match = _VARIABLE.fullmatch(argv[position])
                if match:
                    self.sinks.add((match[1] or match[2], argv, position, cwd))
        # Snapshot: recursive consumers can add bindings and sinks while yielding.
        for variable, argv, position, sink_cwd in tuple(self.sinks):
            for value in tuple(self.values.get(variable, ())):
                if position == 0:
                    # Include both field-split command text and a quoted executable
                    # (the tokenizer intentionally does not retain quote provenance).
                    variants = (value + ' ' + shlex.join(argv[1:]),
                                shlex.join((value, *argv[1:])))
                elif argv[0] == 'eval':
                    variants = (' '.join((*argv[1:position], value, *argv[position + 1:])),)
                else:
                    variants = (shlex.join((*argv[:position], value, *argv[position + 1:])),)
                for payload in variants:
                    key = payload, sink_cwd
                    if key not in self.emitted:
                        self.emitted.add(key)
                        yield key
