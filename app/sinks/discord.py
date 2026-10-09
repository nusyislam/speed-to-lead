"""Post a message to a Discord incoming webhook."""

import logging
import os

import httpx

log = logging.getLogger(__name__)

# The HTTP clients log full request URLs at INFO/DEBUG, and the webhook URL
# contains its token. Set here so it applies wherever this sink is imported.
# httpx2/httpcore2 are what the anthropic SDK uses.
for name in ("httpx", "httpx2", "httpcore", "httpcore2"):
    logging.getLogger(name).setLevel(logging.WARNING)

TIMEOUT_SECONDS = 5.0
MAX_CONTENT_LENGTH = 2000  # Discord rejects longer messages


async def send_to_discord(
    content: str,
    webhook_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,  # tests pass a mock transport
) -> bool:
    """Send content to Discord. Returns True on success, False on any failure.
    Never raises: a Discord problem must not break the lead pipeline."""
    url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        log.error("Discord send skipped: DISCORD_WEBHOOK_URL is not set")
        return False

    if len(content) > MAX_CONTENT_LENGTH:
        content = content[: MAX_CONTENT_LENGTH - 1] + "…"

    body = {
        "content": content,
        # Lead-supplied text could contain "@everyone" or role/user mentions
        "allowed_mentions": {"parse": []},
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(url, json=body)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # Don't log the URL: it contains the webhook token
        log.error("Discord send failed: HTTP %s", exc.response.status_code)
        return False
    except Exception as exc:
        log.error("Discord send failed: %s", type(exc).__name__)
        return False
    return True
