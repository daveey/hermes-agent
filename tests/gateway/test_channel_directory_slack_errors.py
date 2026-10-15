"""A Slack channel-list failure logs ONE line.

``SlackApiError.__str__`` is two lines ("The request to the Slack API failed. (url: ...)" then
"The server responded with: {...}"). Logged verbatim, the second line lands in the log file with
no timestamp or level, so line-oriented shippers file it as its own record and grade it by its
words — an ``invalid_auth`` warning became an error-level issue (Baby Bear mini, BB-69/BB-76).
"""

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.channel_directory import _build_slack


class _SlackApiError(Exception):
    """Shape of ``slack_sdk.errors.SlackApiError``: multi-line str, ``response['error']``."""

    def __init__(self, url: str, error: str):
        self.response = {"ok": False, "error": error}
        super().__init__(
            f"The request to the Slack API failed. (url: {url}, status: 200)\n"
            f"The server responded with: {self.response}"
        )


def test_slack_api_error_is_logged_on_one_line(tmp_path, caplog):
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=_SlackApiError("https://slack.com/api/users.conversations", "invalid_auth"))
    adapter = SimpleNamespace(_team_clients={"T0TEAM": client})

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}), \
            caplog.at_level(logging.WARNING, logger="gateway.channel_directory"):
        asyncio.run(_build_slack(adapter))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "\n" not in warnings[0]
    assert "invalid_auth" in warnings[0]
    assert "T0TEAM" in warnings[0]
