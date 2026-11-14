"""Shell comments must not promote unrelated data literals into executed output."""

import subprocess

import pytest

from cron.lifecycle_guard import _iter_printed_shell_payloads, scan_gateway_lifecycle
from tools.shell_heredoc import output_reaches_shell


@pytest.mark.parametrize("prefix", ["", "x=1\n", "printf '%s' ok\n"])
@pytest.mark.parametrize("indent", ["", "  \t"])
@pytest.mark.parametrize("comment", [
    '# transport; `auth` (git helper)',
    '# call; `config` and `alias`',
    '# (`auth`',
    '# | bash',
    '# eval "$(printer)"',
    '# unclosed " \' $( ` ; | bash',
])
def test_comment_is_not_an_output_sink(prefix, indent, comment):
    text = prefix + indent + comment + '\nlog="$HOME/data.log"\n'
    assert not output_reaches_shell(text)
    assert list(_iter_printed_shell_payloads(text)) == []


def test_wrapper_does_not_execute_oversized_data_assignment(tmp_path):
    data = tmp_path / "data.log"
    data.write_text("padding\n" * 160000, encoding="utf-8")
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(
        '#!/usr/bin/env bash\nx=1\n# transport; `auth` (git helper)\n'
        f'log="{data}"\n', encoding="utf-8",
    )
    assert scan_gateway_lifecycle(f"bash {wrapper}", cwd=str(tmp_path)) == (False, None)


@pytest.mark.parametrize("command", [
    "`printf marker`", "$(printf marker)",
    "printf marker | bash", 'eval "$(printf marker)"',
    "bash -c 'marker'",  # shell -c execution does not itself need an output sink
])
def test_comments_and_real_execution_with_bash(tmp_path, command):
    marker = tmp_path / "marker"
    marker.write_text('#!/bin/sh\nprintf reached > "$MARK"\n', encoding="utf-8")
    marker.chmod(0o700)
    script = tmp_path / "run.sh"
    script.write_text('# inert; `marker` $(marker) | bash\n' + command + '\n', encoding="utf-8")
    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "MARK": str(tmp_path / "seen")},
        capture_output=True, check=False, text=True, encoding="utf-8", timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "seen").read_text() == "reached"
    if "-c" not in command:
        assert output_reaches_shell(script.read_text())


def test_bash_comment_does_not_run_substitutions(tmp_path):
    mark = tmp_path / "seen"
    script = '# inert; `touch seen` $(touch seen) | bash\nlog="data.log"\n'
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env={"PATH": "/usr/bin:/bin"},
        capture_output=True, check=False, text=True, encoding="utf-8", timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not mark.exists()
    assert not output_reaches_shell(script)


@pytest.mark.parametrize("text", [
    "printf '%s' '# data; `auth`'",
    'printf "%s" "# data; `auth`"',
    "printf '%s' 'multiline\n# data; `auth`\nend'",
    'python3 -c "s=\'# data; `auth`\'"',
    "cat <<'EOF'\n# data; `auth`\nEOF\n",
    "cat <<EOF\n# data; `auth`\nEOF\n",
    r"echo \# data; `auth`",
    "echo word# data; `auth`",
    'echo "$(printf \'# data; `auth`\')"',
    "echo ${x:-# data; `auth`}",
])
def test_quoted_expanded_and_heredoc_hashes_remain_conservative(text):
    assert output_reaches_shell(text)


@pytest.mark.parametrize("prefix", [
    "x='['\n", 'x="\'"\n', "x='\\'\n", 'x="${v:- # literal}"\n',
    "x='literal'\n", 'x="literal"\n', 'x="$(printf \'x\')"\n',
    "x=${v:-fallback}\n", "x=$((1 + 2))\n", "x=$[1 + 2]\n",
    "x=$'a\\\'b'\n", "x=`printf x`\n", "echo \\#\n", "echo a#b\n",
    "echo \\ # '\n'\n", "echo a\\\n# '\n'\n", "echo x # unclosed '\n",
    "cat <<'E'\n# opaque; `auth`\nE\n",
    'cat <<-"E"\n\tdata\n\tE\n',
    "cat <<'A' <<'B'\nfirst\nA\nsecond\nB\n",
    "cat <<''\ndata\n\n", "cat <<'E'\nE\r\n# body\nE\n",
    "cat <<<'text'\n",
])
def test_root_comment_mask_preserves_opaque_prefix(prefix):
    from tools.shell_comments import mask_root_comment_lines

    comment = '# comment; `auth`'
    assert mask_root_comment_lines(prefix + comment) == prefix + ' ' * len(comment)


@pytest.mark.parametrize("text", [
    "echo 'open\n# comment; `auth`", 'echo "open\n# comment; `auth`',
    "echo $(open\n# comment; `auth`", "echo ${open\n# comment; `auth`",
    "cat <<'E'\n# comment; `auth`", "cat <<\n# comment; `auth`",
    "cat <<E\nx\\\nE\n# comment; `auth`\nE\n",
    "cat <<'E'X\nE\n# comment; `auth`\nEX\n",
    "cat <<$'E'\n$E\n# comment; `auth`\nE\n",
    "x=$(cat <<'E'\nx\nE\n)\n# comment; `auth`",
    "( # )\n# comment; `auth`\n)",
    '(' * 35 + ')' * 35 + '\n# comment; `auth`',
])
def test_uncertain_regions_abandon_masking(text):
    from tools.shell_comments import mask_root_comment_lines

    assert mask_root_comment_lines(text) == text
    assert output_reaches_shell(text)


@pytest.mark.parametrize("tabs", [False, True])
def test_unquoted_heredoc_false_close_runs_real_substitution(tmp_path, tabs):
    from tools.shell_comments import mask_root_comment_lines

    script = 'cat <<' + ('-' if tabs else '') + 'E\nx\\\n' + ('\t' if tabs else '')
    script += 'E\n# $(printf marker | bash)\nE\n'
    marker = tmp_path / 'marker'
    marker.write_text('#!/bin/sh\nprintf reached > seen\n', encoding='utf-8')
    marker.chmod(0o700)
    result = subprocess.run(
        ['bash', '-c', script], cwd=tmp_path, env={'PATH': f'{tmp_path}:/usr/bin:/bin'},
        capture_output=True, check=False, text=True, encoding='utf-8', timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'seen').read_text() == 'reached'
    assert mask_root_comment_lines(script) == script
    assert output_reaches_shell(script)
