"""The checkpoint back-off JSON accepts files written with or without a BOM."""

import pytest

from tools.checkpoint_staging import _read_slow, _slow_file


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_slow_folder_backoff_survives_bom(tmp_path, encoding):
    path = _slow_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"folder": 12345.0}', encoding=encoding)
    assert _read_slow(tmp_path) == {"folder": 12345.0}
