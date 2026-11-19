"""Literal Python shebang recognition. Not interpreter proof: callers keep the shell walk."""
import re


_PYTHON = re.compile(r"(?:/[^\s'\"\\$`]+/)?(?:python(?:\d+(?:\.\d+)*)?|pypy3?)\Z")
_FLAGS = frozenset({'-u', '-B', '-E', '-I', '-s', '-S'})


def is_python_shebang(text: str) -> bool:
    """Recognize only literal Python shebangs with non-program-selecting flags.

    Unknown env options, assignments, quoting and CPython -c/-m stay shell-walked.
    env without -S accepts exactly one program word (the kernel's single argument).
    """
    if not text.startswith('#!'):
        return False
    words = text.partition('\n')[0][2:].split()
    if not words:
        return False
    if words[0] in ('/usr/bin/env', '/bin/env'):
        words = words[1:]
        if words[:1] == ['-S']:
            words = words[1:]
        elif len(words) != 1:
            return False
    return bool(words and _PYTHON.fullmatch(words[0])
                and all(word in _FLAGS for word in words[1:]))


def checks_computed_input(direct_invocation: bool, text: str) -> bool:
    """False only for the text of a directly invoked literal-Python-shebang file."""
    return not (direct_invocation and is_python_shebang(text))
