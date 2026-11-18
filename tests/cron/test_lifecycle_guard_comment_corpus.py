"""Generated Bash marker controls for the comment-only permissive change.

Baseline: 04808b222ec7d6d4b3f4c226e7fd32370b0b330e, every generated case refuses.
The marker is a fake hermes executable; no real gateway lifecycle command runs.
"""

import itertools
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.spawns_gateway_lookalike
def test_comment_mask_preserves_generated_executed_payloads(tmp_path):
    executable = tmp_path / 'hermes'
    executable.write_text('#!/bin/sh\nprintf reached > seen\n', encoding='utf-8')
    executable.chmod(0o700)
    # The parent subprocess supplies the hard timeout; macOS has no GNU timeout.
    timeout = tmp_path / 'timeout'
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding='utf-8')
    timeout.chmod(0o700)
    marker = tmp_path / 'seen'
    payload = "printf '%s %s %s' hermes '\"gateway\"' restart"
    comments = ['# inert; `auth`\n', '# unclosed \' \" $(\n', '# | bash\n']
    wrappers = ['bash', 'env bash', 'command bash', 'timeout 5 bash']
    nesting = ['{body}', '(\n{body}\n)', 'f() {{\n{body}\n}}; f', 'bash -s <<\'E\'\n{body}\nE']
    for comment, wrapper, outer in itertools.product(comments, wrappers, nesting):
        body = comment + payload + ' | ' + wrapper
        command = outer.format(body=body)
        result = subprocess.run(
            ['bash', '-c', command], cwd=tmp_path,
            env={'PATH': f'{tmp_path}:/usr/bin:/bin'},
            capture_output=True, check=False, text=True, encoding='utf-8', timeout=5,
        )
        assert result.returncode == 0, (command, result.stderr)
        assert marker.read_text() == 'reached', command
        marker.unlink()
        unsafe, _refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe, command


@pytest.mark.spawns_gateway_lookalike
def test_pending_heredoc_multiline_groups_preserve_executed_hash(tmp_path):
    """A newline inside a group starts pending heredocs, unlike quoted newlines."""
    from tools.shell_comments import mask_root_comment_lines
    from tools.shell_heredoc import output_reaches_shell

    executable = tmp_path / 'hermes'
    executable.write_text('#!/bin/sh\nprintf reached > seen\n', encoding='utf-8')
    executable.chmod(0o700)
    marker = tmp_path / 'seen'
    groups = [('( :', ')'), ('{ :', '}'), ('[[ -n x', ']]'), ('f() ( :', ')')]
    delimiters = ["<<'E'", '<<"E"', "<<-'E'"]
    prefixes = ['', '# inert; `auth`\n']
    for (opening, closing), delimiter, prefix in itertools.product(groups, delimiters, prefixes):
        command = (
            prefix + "emit() { printf '%s %s %s' hermes '\"gateway\"' restart; }\n"
            + f'cat {delimiter}; {opening}\nE\n{closing}\n:\n: "\nE\n'
            + '# $(emit | bash)\n" #"'
        )
        result = subprocess.run(
            ['bash', '-c', command], cwd=tmp_path,
            env={'PATH': f'{tmp_path}:/usr/bin:/bin'},
            capture_output=True, check=False, text=True, encoding='utf-8', timeout=5,
        )
        assert result.returncode == 0, (command, result.stderr)
        assert marker.read_text() == 'reached', command
        marker.unlink()
        assert output_reaches_shell(command), command
        unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe, (command, refusal)
        assert mask_root_comment_lines(command) == command, command
