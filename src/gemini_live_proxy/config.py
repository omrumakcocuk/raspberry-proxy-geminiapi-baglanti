from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv


load_dotenv()


DEFAULT_GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
DEFAULT_SUBSCRIPTION_VERIFY_URL = (
    "https://manager.orbitkidslab.com/api/chatbot/verify-subscription"
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_int(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    gemini_ws_url: str = DEFAULT_GEMINI_WS_URL
    gemini_api_key: str | None = None
    gemini_access_token: str | None = None
    gemini_auth_mode: str = "api_key_query"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    access_control_enabled: bool = True
    subscription_verify_url: str = DEFAULT_SUBSCRIPTION_VERIFY_URL
    subscription_verify_timeout_seconds: float = 10.0
    upstream_open_timeout_seconds: float = 15.0
    upstream_ping_interval_seconds: float = 20.0
    upstream_ping_timeout_seconds: float = 60.0
    max_message_bytes: int | None = 16 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_ws_url=os.getenv("GEMINI_WS_URL", DEFAULT_GEMINI_WS_URL),
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_access_token=os.getenv("GEMINI_ACCESS_TOKEN") or None,
            gemini_auth_mode=os.getenv("GEMINI_AUTH_MODE", "api_key_query"),
            host=os.getenv("PROXY_HOST", "127.0.0.1"),
            port=int(os.getenv("PROXY_PORT", "8000")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            access_control_enabled=_env_bool("ACCESS_CONTROL_ENABLED", True),
            subscription_verify_url=os.getenv(
                "SUBSCRIPTION_VERIFY_URL", DEFAULT_SUBSCRIPTION_VERIFY_URL
            ),
            subscription_verify_timeout_seconds=float(
                os.getenv("SUBSCRIPTION_VERIFY_TIMEOUT_SECONDS", "10")
            ),
            upstream_open_timeout_seconds=float(
                os.getenv("UPSTREAM_OPEN_TIMEOUT_SECONDS", "15")
            ),
            upstream_ping_interval_seconds=float(
                os.getenv("UPSTREAM_PING_INTERVAL_SECONDS", "20")
            ),
            upstream_ping_timeout_seconds=float(
                os.getenv("UPSTREAM_PING_TIMEOUT_SECONDS", "60")
            ),
            max_message_bytes=_optional_int(
                "MAX_MESSAGE_BYTES", 16 * 1024 * 1024
            ),
        )

    def validate(self) -> None:
        if self.access_control_enabled:
            verification_url = urlsplit(self.subscription_verify_url)
            if verification_url.scheme != "https" or not verification_url.netloc:
                raise ValueError("SUBSCRIPTION_VERIFY_URL must be a valid https:// URL")
            if self.subscription_verify_timeout_seconds <= 0:
                raise ValueError(
                    "SUBSCRIPTION_VERIFY_TIMEOUT_SECONDS must be greater than zero"
                )
        if self.gemini_auth_mode not in {
            "api_key_query",
            "access_token_query",
            "bearer_header",
        }:
            raise ValueError(
                "GEMINI_AUTH_MODE must be api_key_query, access_token_query, or bearer_header"
            )
        if self.gemini_auth_mode == "api_key_query" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required for api_key_query authentication")
        if self.gemini_auth_mode in {"access_token_query", "bearer_header"}:
            if not self.gemini_access_token:
                raise ValueError(
                    "GEMINI_ACCESS_TOKEN is required for access token authentication"
                )
        parsed = urlsplit(self.gemini_ws_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
            raise ValueError("GEMINI_WS_URL must be a valid ws:// or wss:// URL")

    def upstream_connection(self) -> tuple[str, dict[str, str]]:
        """Return the authenticated URL and headers without exposing them to logs."""
        self.validate()
        headers: dict[str, str] = {}
        parsed = urlsplit(self.gemini_ws_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))

        if self.gemini_auth_mode == "api_key_query":
            query["key"] = self.gemini_api_key or ""
        elif self.gemini_auth_mode == "access_token_query":
            query["access_token"] = self.gemini_access_token or ""
        else:
            headers["Authorization"] = f"Bearer {self.gemini_access_token}"

        authenticated_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
        )
        return authenticated_url, headers
