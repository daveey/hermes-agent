"""Literal private-use filenames must never alias an ampersand sibling."""

import shlex

import pytest

from cron.lifecycle_guard import _shlex_tokens, scan_gateway_lifecycle


@pytest.mark.parametrize("quote", [shlex.quote, lambda s: '"' + s + '"', str])
@pytest.mark.parametrize("template", ["bash {path}", ". {path}", "cat {path} | bash"])
@pytest.mark.parametrize("dangerous_marker", [True, False])
def test_literal_marker_and_ampersand_siblings(tmp_path, quote, template, dangerous_marker):
    marker = tmp_path / "task\ue000.sh"
    sibling = tmp_path / "task&.sh"
    lifecycle = " ".join(["hermes", "gateway", "re" + "start"])
    marker.write_text(lifecycle if dangerous_marker else "echo harmless\n", encoding="utf-8")
    sibling.write_text("echo harmless\n" if dangerous_marker else lifecycle, encoding="utf-8")
    assert scan_gateway_lifecycle(template.format(path=quote(str(marker)))) == (dangerous_marker, None)


@pytest.mark.parametrize("literal", ["\ue000", "a\ue000b", "\ue000\ue001", "a&b", "a\\b"])
def test_tokenizer_preserves_literal_characters(literal):
    assert _shlex_tokens("echo " + shlex.quote(literal) + " 2>&1") == ["echo", literal, "2>&1"]
