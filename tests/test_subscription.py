import json
from urllib.error import HTTPError

import pytest

from gemini_live_proxy.config import Settings
from gemini_live_proxy.subscription import _verify_sync


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


def test_active_subscription_is_authorized(monkeypatch) -> None:
    captured_request = None

    def successful(request, **kwargs):
        nonlocal captured_request
        captured_request = request
        return FakeResponse({"data": {"active": True}})

    monkeypatch.setattr(
        "gemini_live_proxy.subscription.urlopen",
        successful,
    )

    result = _verify_sync("user-token", Settings(gemini_api_key="key"))

    assert result.authorized is True
    assert captured_request.get_method() == "GET"
    assert captured_request.get_header("Authorization") == "Bearer user-token"


def test_explicit_inactive_subscription_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "gemini_live_proxy.subscription.urlopen",
        lambda *args, **kwargs: FakeResponse({"hasActiveSubscription": False}),
    )

    result = _verify_sync("user-token", Settings(gemini_api_key="key"))

    assert result.authorized is False


def test_unauthorized_http_response_is_rejected(monkeypatch) -> None:
    def unauthorized(*args, **kwargs):
        raise HTTPError("https://example.test", 401, "Unauthorized", {}, None)

    monkeypatch.setattr("gemini_live_proxy.subscription.urlopen", unauthorized)

    result = _verify_sync("bad-token", Settings(gemini_api_key="key"))

    assert result.authorized is False
