"""Owner records refuse invalid metadata even when payload and liveness are valid."""
import json
import os
import stat
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_broker as shared


@pytest.mark.parametrize('change', ['nonregular', 'foreign', 'oversized', 'zero', 'negative'])
def test_record_metadata_refuses_independently_of_valid_payload(tmp_path, monkeypatch, change):
    good = {'version': 1, 'ready': True, 'pid': 123, 'start': 's',
            'capability': 'a' * 64, 'generation': 'b' * 32, 'digest': 'c' * 64}
    if change in ('zero', 'negative'):
        good['pid'] = 0 if change == 'zero' else -1
    path = tmp_path / shared.OWNER_FILE
    path.write_text(json.dumps(good))
    path.chmod(0o600)
    actual = os.fstat

    def metadata(fd):
        info = actual(fd)
        return SimpleNamespace(st_mode=(stat.S_IFDIR | 0o600 if change == 'nonregular' else info.st_mode),
                               st_uid=(info.st_uid + 1 if change == 'foreign' else info.st_uid),
                               st_size=(shared.MAX_OWNER_BYTES + 1 if change == 'oversized' else info.st_size))

    monkeypatch.setattr(shared.os, 'fstat', metadata)
    monkeypatch.setattr(shared, 'owner_alive', lambda record: True)
    with pytest.raises(shared.OwnerUnavailable):
        shared.read_owner(tmp_path)
