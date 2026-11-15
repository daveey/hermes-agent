"""Quoted argv must not overwrite the provenance of real stdin redirections."""
import subprocess

import pytest

from cron.lifecycle_guard import _iter_command_token_lists, _split_redirections, scan_gateway_lifecycle


ARGV = ["'0<&-'", "'0>out'", "'0<clean'", r"0\<\&-"]


@pytest.mark.parametrize("argument", ARGV)
@pytest.mark.parametrize("dangerous", [True, False])
def test_quoted_redirection_argument_keeps_stdin(tmp_path, argument, dangerous):
    script = tmp_path / "r.sh"
    script.write_text(" ".join(["hermes", "gateway", "restart"]) + "\n"
                      if dangerous else "echo harmless\n", encoding="utf-8")
    (tmp_path / "clean").write_text("echo clean\n", encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash -s <{script} {argument}", cwd=str(tmp_path)) == (dangerous, None)


@pytest.mark.parametrize("argument", ARGV)
def test_real_bash_quoted_redirection_argument_executes_stdin(tmp_path, argument):
    # Only a harmless marker is executed, never a lifecycle command.
    (tmp_path / "r.sh").write_text("printf 'STDIN_EXECUTED\\n'\n", encoding="utf-8")
    (tmp_path / "clean").write_text("echo clean\n", encoding="utf-8")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", f"bash -s <r.sh {argument}"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "STDIN_EXECUTED\n"
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("source, kept, stdin", [
    ("<bad '0<&-'", ["0<&-"], ["bad"]),
    ('<bad "0<&-"', ["0<&-"], ["bad"]),
    (r"<bad 0\<\&-", ["0<&-"], ["bad"]),
    ("<bad 0'<'\"&-\"", ["0<&-"], ["bad"]),
    ("<bad 0<&-", [], []),
    ("<bad 0>out", [], []),
    ("<bad 0<clean", [], ["clean"]),
    ("<bad 0<'&-'", [], ["&-"]),
    (r"<bad 0<\&-", [], ["&-"]),
    ("3<bad 4>&3- <&4", [], ["bad"]),
    ("3<bad '4>&3-' <&3", ["4>&3-"], ["bad"]),
    ("<'0<clean'", [], ["0<clean"]),
    ("< '\ue0000\ue000'", [], ["\ue0000\ue000"]),
])
def test_lexical_redirection_parts(source, kept, stdin):
    tokens, = _iter_command_token_lists(source)
    assert _split_redirections(tokens) == (kept, stdin)


@pytest.mark.parametrize("name", ["0<clean", "\ue0000\ue000", "\ue000\ue0001\ue000\ue000"])
@pytest.mark.parametrize("dangerous", [True, False])
def test_quoted_input_filename_preserved(tmp_path, name, dangerous):
    (tmp_path / name).write_text(" ".join(["hermes", "gateway", "restart"]) + "\n"
                                 if dangerous else "echo harmless\n", encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash <'{name}'", cwd=str(tmp_path)) == (dangerous, None)


@pytest.mark.parametrize("spelling", ["\ue0000\ue000", "\ue000''0\ue000", "\ue000''\ue0000\ue000''\ue000"])
@pytest.mark.parametrize("literal_dangerous", [True, False])
def test_quote_fragment_marker_collision(tmp_path, spelling, literal_dangerous):
    literal = spelling.replace("'", "")
    bad = " ".join(["hermes", "gateway", "restart"]) + "\n"
    for name, dangerous in [(literal, literal_dangerous), ("<", not literal_dangerous)]:
        (tmp_path / name).write_text(bad if dangerous else "echo harmless\n", encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash <{spelling}", cwd=str(tmp_path)) == (literal_dangerous, None)
