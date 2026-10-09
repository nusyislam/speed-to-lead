import asyncio
import json

import httpx
import pytest

from app.sinks.discord import MAX_CONTENT_LENGTH, send_to_discord

URL = "https://discord.test/api/webhooks/123/token"


def send(content, handler, url=URL):
    return asyncio.run(send_to_discord(content, webhook_url=url, transport=httpx.MockTransport(handler)))


def test_success_posts_content_with_mentions_disabled():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(204)

    assert send("hello @everyone", handler) is True
    assert len(requests) == 1
    assert str(requests[0].url) == URL
    body = json.loads(requests[0].content)
    assert body == {"content": "hello @everyone", "allowed_mentions": {"parse": []}}


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500])
def test_http_error_returns_false(status):
    assert send("hi", lambda request: httpx.Response(status)) is False


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("no route"),
        httpx.ReadTimeout("slow"),
        RuntimeError("boom"),
    ],
)
def test_transport_error_returns_false(exc):
    def handler(request):
        raise exc

    assert send("hi", handler) is False


def test_missing_url_returns_false_without_request(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    def handler(request):
        raise AssertionError("should not be called")

    assert send("hi", handler, url=None) is False


def test_url_read_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(204)

    assert send("hi", handler, url=None) is True
    assert seen == [URL]


def test_long_content_truncated():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    send("x" * 5000, handler)
    assert len(bodies[0]["content"]) == MAX_CONTENT_LENGTH
