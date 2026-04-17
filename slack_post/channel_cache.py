"""TTL cache for Slack ``conversations_info`` channel-name lookups.

Slack rate-limits ``conversations.info`` (Tier 3) so we avoid calling it on
every inbound event. Channel renames are rare; a 1-hour TTL is fine."""

import time


CHANNEL_NAME_TTL_SECONDS = 3600

_cache: dict[str, tuple[str, float]] = {}


def get_channel_name(slack_client, channel: str) -> str:
    """Return the Slack channel's name, using a TTL cache to cut API calls."""
    now = time.monotonic()
    cached = _cache.get(channel)
    if cached is not None and (now - cached[1]) < CHANNEL_NAME_TTL_SECONDS:
        return cached[0]
    response = slack_client.conversations_info(channel=channel)
    name = response["channel"]["name"]
    _cache[channel] = (name, now)
    return name


def clear_cache() -> None:
    """Clear the channel-name cache. Intended for tests."""
    _cache.clear()
