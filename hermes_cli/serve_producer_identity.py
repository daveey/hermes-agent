"""One admission identity; kernel birth probes alone never authorize a launcher.

Launcher incarnation is the digest of the twice-read exec observation (including
its namespace), not the mutable on-disk source generation. A changed exec is
uncertainty, not proof of retirement. Source generation is checked separately.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ProcessKey:
    boot_id: str
    namespace: str
    pid: int
    start_ticks: int
    launcher_incarnation: str


def observation_digest(record) -> str:
    return hashlib.sha256(json.dumps(asdict(record), sort_keys=True,
                                     ensure_ascii=True).encode('ascii')).hexdigest()


def process_key(record) -> ProcessKey:
    birth = record.identity
    return ProcessKey(birth.boot_id, record.namespace, birth.pid, birth.start_ticks,
                      observation_digest(record))


def receipt_key(receipt) -> ProcessKey:
    birth = receipt.identity
    return ProcessKey(birth.boot_id, receipt.namespace, birth.pid, birth.start_ticks,
                      receipt.observation)
