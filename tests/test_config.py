from urllib.parse import parse_qs, urlsplit

import pytest

from gemini_live_proxy.config import Settings


def test_api_key_is_added_to_upstream_url() -> None:
    settings = Settings(gemini_api_key="secret-key")
    url, headers = settings.upstream_connection()

    assert parse_qs(urlsplit(url).query)["key"] == ["secret-key"]
    assert headers == {}


def test_bearer_mode_uses_header() -> None:
    settings = Settings(
        gemini_auth_mode="bearer_header",
        gemini_access_token="secret-token",
    )
    url, headers = settings.upstream_connection()

    assert "secret-token" not in url
    assert headers == {"Authorization": "Bearer secret-token"}


def test_missing_api_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        Settings().validate()


def test_orbit_access_control_is_enabled_by_default() -> None:
    settings = Settings(gemini_api_key="key")

    settings.validate()

    assert settings.access_control_enabled is True


def test_subscription_verification_requires_https() -> None:
    with pytest.raises(ValueError, match="SUBSCRIPTION_VERIFY_URL"):
        Settings(
            gemini_api_key="key",
            subscription_verify_url="http://manager.example.test/verify",
        ).validate()
