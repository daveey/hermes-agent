"""Literal prose is not an arithmetic producer; code-like unknown bodies stay closed."""

import shutil
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_arith_sink import substitution_in_arithmetic
from tools.shell_heredoc import output_reaches_shell


@pytest.mark.parametrize("owner", ["git commit -F -", "tee", "unknown"])
@pytest.mark.parametrize("body", ["Don't break things", 'An unmatched "quotation'])
def test_literal_prose_does_not_cancel_inert_masking(owner, body):
    command = f"{owner} <<'R'\n{body}\nR\n"
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert not output_reaches_shell(command)
    prefix = "cat > notes.md <<'EOF'\nTo apply: hermes gateway restart\nEOF\n"
    assert not scan_gateway_lifecycle(prefix + command)[0]


def test_nested_cat_prose_is_not_a_sink():
    command = "y=$(cat <<'EOF'\nIt's fine\nEOF\n)"
    assert not substitution_in_arithmetic(command, date_exempt=True)
    assert not output_reaches_shell(command)


@pytest.mark.parametrize("mark", ["$value", "`cmd`", "<(cmd)", ">(cmd)"])
def test_code_like_unclosed_nested_quote_stays_closed(mark):
    command = f"unknown <<'R'\nDon't {mark}\nR\n"
    assert substitution_in_arithmetic(command, date_exempt=True)


@pytest.mark.parametrize("prefix", ["printf '%s' \\'\n", "# don't\n", "case x in x) :;; esac\n"])
@pytest.mark.parametrize("transport", ["bash <<'R'\nBODY\nR\n", "cat > F <<'R'\nBODY\nR\nbash F\n"])
def test_quote_divergence_cannot_hide_real_bash_execution(tmp_path, prefix, transport):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    big = tmp_path / "BIG"
    big.write_text("touch executed\n" + "# padding\n" * 150000)
    body = prefix + 'x=$(( 1 + $(echo "b[\\$(bash BIG)]") ))'
    command = transport.replace("BODY", body)
    # The file walker inspects existing files, not predicted shell writes.
    (tmp_path / "F").write_text(body)
    unsafe, _ = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True,
                   timeout=10, check=False)
    assert (tmp_path / "executed").exists(), "fixture must execute its marker"
    assert unsafe, "real Bash executed a payload the guard allowed"


@pytest.mark.parametrize("owner", ["cat", "tee", "git commit -F -"])
def test_real_bash_literal_prose_is_data(tmp_path, owner):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    # A shell function consumes git's stdin without creating a real commit.
    setup = 'git() { cat; }; '
    command = setup + f"{owner} <<'R'\nDon't break things\nR\n"
    result = subprocess.run([bash, "-c", command], cwd=tmp_path, capture_output=True,
                            text=True, timeout=10, check=False)
    assert result.returncode == 0
    assert result.stdout == "Don't break things\n"
    assert not substitution_in_arithmetic(command, date_exempt=True)
