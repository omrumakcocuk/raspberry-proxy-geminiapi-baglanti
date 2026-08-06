from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings


class SubscriptionServiceUnavailable(RuntimeError):
    """Raised when Orbit cannot provide a trustworthy verification result."""


@dataclass(frozen=True, slots=True)
class SubscriptionVerification:
    authorized: bool


def _explicit_boolean_result(payload: Any) -> bool | None:
    """Read common verification fields without depending on one response wrapper."""
    if not isinstance(payload, dict):
        return None

    for key in (
        "authorized",
        "valid",
        "active",
        "subscribed",
        "hasActiveSubscription",
        "has_active_subscription",
    ):
        value = payload.get(key)
        if isinstance(value, bool):
            return value

    for key in ("data", "subscription", "result"):
        nested = _explicit_boolean_result(payload.get(key))
        if nested is not None:
            return nested
    return None


def _verify_sync(token: str, settings: Settings) -> SubscriptionVerification:
    request = Request(
        settings.subscription_verify_url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "gemini-live-proxy/0.1",
        },
    )

    try:
        with urlopen(
            request, timeout=settings.subscription_verify_timeout_seconds
        ) as response:
            body = response.read(64 * 1024)
    except HTTPError as exc:
        if exc.code in {400, 401, 403, 404}:
            return SubscriptionVerification(authorized=False)
        raise SubscriptionServiceUnavailable(
            f"Orbit verification returned HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SubscriptionServiceUnavailable("Orbit verification failed") from exc

    if not body:
        return SubscriptionVerification(authorized=True)

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubscriptionServiceUnavailable(
            "Orbit verification returned an invalid response"
        ) from exc

    explicit_result = _explicit_boolean_result(payload)
    return SubscriptionVerification(
        authorized=True if explicit_result is None else explicit_result
    )


async def verify_subscription(
    token: str, settings: Settings
) -> SubscriptionVerification:
    return await asyncio.to_thread(_verify_sync, token, settings)
