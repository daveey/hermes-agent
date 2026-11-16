"""Bounded reuse of the pure text parse used by skill catalog refreshes.

Do not cache catalog decisions here: profile/root precedence, disabled names,
platform/environment/app eligibility and collision notices remain caller-owned.
The caller still reads the current text at its existing refresh deadline, so
in-place edits, replacement and same-stat rewrites need no new filesystem gate.
"""
from copy import deepcopy
from functools import lru_cache


@lru_cache(maxsize=512)
def _parse(text, parser, yaml_loader):
    # yaml_loader is part of the parser's dependency identity, not an invocation.
    return parser(text)


def parse_catalog_text(text: str):
    """Parse the catalog's already-truncated text; never lend out cached YAML objects."""
    from agent import skill_utils
    from tools import skills_tool

    # Custom parsers need not be pure (and may carry caller scope). Preserve their
    # original invocation semantics rather than memoizing arbitrary callbacks.
    if (skills_tool._parse_frontmatter is not skills_tool._DEFAULT_PARSE_FRONTMATTER
            or skill_utils.parse_frontmatter is not skill_utils._DEFAULT_PARSE_FRONTMATTER):
        return skills_tool._parse_frontmatter(text)
    return deepcopy(_parse(text, skill_utils.parse_frontmatter, skill_utils.yaml_load))
