"""Selected artifact metadata under the blob transport's interruptible deadline."""
from __future__ import annotations

import json
import urllib.parse

from hermes_cli.kanban_sync_client import Hub

# Metadata is batched by the existing sync producer. Bound its response as well
# as its duration; excess/unknown replies retain custody and replay the outbox.
MAX_METADATA_RESPONSE_BYTES = 25 * 1024 * 1024


class SourceArtifactHub(Hub):
    def ops(self, board: str, ops: list[dict]) -> dict:
        from hermes_cli.kanban_sync_attachment_http import _request
        body = _request(
            self, "POST", f"/v2/boards/{urllib.parse.quote(board, safe='')}/ops",
            json.dumps({"ops": ops}).encode("utf-8"), MAX_METADATA_RESPONSE_BYTES,
            content_type="application/json", timeout=self.timeout,
        )
        return json.loads(body or b"{}")
