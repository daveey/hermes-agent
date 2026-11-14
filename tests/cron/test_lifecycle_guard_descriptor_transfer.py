"""Descriptor provenance regressions; scanner inputs never execute a shell."""
import pytest

from cron.lifecycle_guard import _split_redirections, scan_gateway_lifecycle


@pytest.mark.parametrize("suffix", ["<&3-", "4<&3 <&4", "4>&3 <&4"])
@pytest.mark.parametrize("dangerous", [True, False])
def test_opened_descriptor_transfer(tmp_path, suffix, dangerous):
    script = tmp_path / "script.sh"
    script.write_text(" ".join(["hermes", "gateway", "restart"]) + "\n"
                      if dangerous else "echo harmless\n", encoding="utf-8")
    assert scan_gateway_lifecycle(f"bash 3<{script} {suffix}") == (dangerous, None)


@pytest.mark.parametrize("redirects, expected", [
    (["3<bad", "4<&3", "3<&-", "<&4"], ["bad"]),
    (["3<bad", "4>&3-", "<&4"], ["bad"]),
    (["3<bad", "4<&3-", "<&3"], []),
    (["3<bad", "3<&-", "<&3"], []),
    (["3<bad", "3>&-", "<&3"], []),
    (["3<bad", "3>out", "<&3"], []),
    (["3<bad", "3<<<safe", "<&3"], []),
    (["3<bad", "3<&9", "<&3"], []),
    (["3<bad", "3<good", "<&3"], ["good"]),
    (["3<bad", "4<&3", "3<good", "<&4"], ["bad"]),
    (["<bad", "<good"], ["good"]),
    (["<bad", "0<&-"], []),
    (["3<bad", "0>&3"], ["bad"]),
    (["3<bad", "3<&3", "<&3"], ["bad"]),
    (["3<bad", "3<&3-", "<&3"], []),
])
def test_descriptor_close_and_rebind(redirects, expected):
    assert _split_redirections(redirects) == ([], expected)
