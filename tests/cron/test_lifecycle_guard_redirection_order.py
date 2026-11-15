"""Command exposure must preserve the shell's left-to-right fd bindings."""
import subprocess

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle


@pytest.mark.parametrize("command", ["<clean bash <bad", "<clean bash 3<bad <&3"])
def test_command_position_stdin_override(tmp_path, command):
    (tmp_path / "bad").write_text(" ".join(["hermes", "gateway", "restart"]) + "\n", encoding="utf-8")
    (tmp_path / "clean").write_text("echo clean\n", encoding="utf-8")
    assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (True, None)


# Each sequence is crossed with every command-word position. Only harmless
# marker files are ever executed; lifecycle text is written after Bash exits.
@pytest.mark.parametrize("redirects, final", [
    (["<clean", "<bad"], "bad"),
    (["<bad", "<clean"], "clean"),
    (["<clean", "3<bad", "<&3"], "bad"),
    (["3<bad", "4<&3", "3<clean", "<&4"], "bad"),
    (["3<bad", "4>&3-", "3<clean", "<&4"], "bad"),
    (["3<bad", "<&3-", "<clean"], "clean"),
    (["<bad", "0<&-", "<clean"], "clean"),
    (["3<bad", "3>out", "<clean"], "clean"),
    (["3<clean", "4<&3", "3<bad", "<&4"], "clean"),
])
@pytest.mark.parametrize("argument", ["", "'0<&-'", r"0\<\&-"])
def test_redirection_order_against_bash(tmp_path, redirects, final, argument):
    for position in range(len(redirects) + 1):
        command = " ".join(redirects[:position] + ["bash -s"] + redirects[position:] + [argument])
        for name in ("bad", "clean"):
            (tmp_path / name).write_text(f"printf '{name}\\n'\n", encoding="utf-8")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command],
                                cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=5, check=False)
        assert result.returncode == 0, (command, result.stderr)
        assert result.stdout == final + "\n", command
        (tmp_path / "bad").write_text(" ".join(["hermes", "gateway", "restart"]) + "\n", encoding="utf-8")
        assert scan_gateway_lifecycle(command, cwd=str(tmp_path)) == (final == "bad", None), command
