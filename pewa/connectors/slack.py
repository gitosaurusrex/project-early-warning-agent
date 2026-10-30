"""Slack connector (SPEC §7): channel history and thread replies, as `message` events.

Slack's history endpoint only returns messages posted after `oldest`, so a new reply in an
older thread is invisible unless its root is re-read. Incremental syncs therefore start
SLACK_THREAD_LOOKBACK_DAYS before the last ingested message and re-read the replies of every
thread found; anything already stored is ignored by ingest.

Message text is stored (linking and the blocker-language signal need it) but never logged.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pewa.connectors.base import ConnectorError
from pewa.connectors.http import HttpClient
from pewa.schemas import Event, to_utc

log = logging.getLogger(__name__)

PAGE_SIZE = 200
SKIP_SUBTYPES = {"channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
                 "channel_archive", "channel_unarchive", "bot_add", "bot_remove"}
HINTS = {
    "not_in_channel": "invite the bot to the channel (/invite @your-bot)",
    "channel_not_found": "check SLACK_CHANNELS holds channel ids (C...), not names",
    "missing_scope": "add the read scopes listed in the README and reinstall the app",
    "invalid_auth": "check SLACK_BOT_TOKEN",
    "not_authed": "check SLACK_BOT_TOKEN",
}


class SlackConnector:
    source = "slack"

    def __init__(
        self,
        token: str,
        channels: list[str],
        *,
        api_url: str = "https://slack.com/api",
        thread_lookback_days: int = 14,
        client: HttpClient | None = None,
    ):
        self.channels = channels
        self.api_url = api_url
        self.thread_lookback = timedelta(days=thread_lookback_days)
        self._token = token
        self._client = client

    def fetch(self, since: datetime | None) -> list[Event]:
        client = self._client or HttpClient("slack", self.api_url, headers={"Authorization": f"Bearer {self._token}"})
        events: list[Event] = []
        try:
            workspace = self._call(client, "auth.test").get("url", "").rstrip("/")
            oldest = f"{(to_utc(since) - self.thread_lookback).timestamp():.6f}" if since is not None else None
            for channel in self.channels:
                name = self._call(client, "conversations.info", channel=channel).get("channel", {}).get("name")
                count = 0
                for message in self._paged(client, "conversations.history", channel=channel, oldest=oldest,
                                           inclusive="true"):
                    for m in [message, *self._replies(client, channel, message)]:
                        if event := self.message_event(m, channel, name, workspace):
                            events.append(event)
                            count += 1
                log.info("slack %s: %d messages", name or channel, count)
        finally:
            if self._client is None:
                client.close()
        if since is not None:
            events = [e for e in events if e.occurred_at >= to_utc(since)]
        return events

    def _call(self, client: HttpClient, method: str, **params: Any) -> dict[str, Any]:
        data = client.get_json(f"/{method}", {k: v for k, v in params.items() if v is not None})
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            hint = f"; {HINTS[error]}" if error in HINTS else ""
            raise ConnectorError(f"slack: {method} failed: {error}{hint}")
        return data

    def _paged(self, client: HttpClient, method: str, **params: Any):
        cursor = None
        while True:
            data = self._call(client, method, limit=PAGE_SIZE, cursor=cursor, **params)
            yield from data.get("messages", [])
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return

    def _replies(self, client: HttpClient, channel: str, root: dict[str, Any]) -> list[dict[str, Any]]:
        if not root.get("reply_count") or root.get("thread_ts") != root.get("ts"):
            return []
        replies = self._paged(client, "conversations.replies", channel=channel, ts=root["ts"])
        return [m for m in replies if m.get("ts") != root["ts"]]  # the first message is the root again

    @staticmethod
    def message_event(m: dict[str, Any], channel: str, channel_name: str | None, workspace: str) -> Event | None:
        if m.get("subtype") in SKIP_SUBTYPES or not m.get("ts"):
            return None
        ts, thread_ts = m["ts"], m.get("thread_ts")
        url = f"{workspace}/archives/{channel}/p{ts.replace('.', '')}" if workspace else None
        if url and thread_ts and thread_ts != ts:
            url += f"?thread_ts={thread_ts}&cid={channel}"
        actor = m.get("user") or (m.get("bot_profile") or {}).get("name") or m.get("username") or m.get("bot_id")
        return Event(
            source="slack", kind="message", occurred_at=datetime.fromtimestamp(float(ts), UTC),
            entity_id=f"{channel}:{ts}", actor=actor, url=url,
            payload={"channel": channel, "channel_name": channel_name, "ts": ts, "thread_ts": thread_ts,
                     "text": m.get("text") or ""},
        )
