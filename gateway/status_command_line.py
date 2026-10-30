"""Command-line rendering for gateway process identity checks."""


def argv_to_command_line(parts) -> str:
    """[daveey] One command-line string from an exact argv, for the identity matchers.

    Whitespace-bearing args (``~/Applications/Hermes Python.app/...``, an inline ``-c`` source) are
    double-quoted so the quote-aware tokenizer in ``_gateway_command_subcommand`` keeps each one a
    single token. A space-joined argv (``ps -o command=``, a NUL->space ``/proc`` read) loses those
    boundaries: the interpreter path splits in two, the ``-c`` walk stops at its second half, and a
    Hermes runpy bootstrap (``python -I -c "import sys, runpy; …"``) stops matching as a gateway.
    Args that already carry a double quote stay as-is. Empty parts (the trailing NUL) are dropped."""
    return " ".join(
        f'"{part}"' if any(c.isspace() for c in part) and '"' not in part else part
        for part in parts
        if part
    ).strip()
