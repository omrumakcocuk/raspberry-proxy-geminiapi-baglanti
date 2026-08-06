from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import Settings
from .subscription import SubscriptionServiceUnavailable, verify_subscription

logger = logging.getLogger(__name__)


def _client_token(client: WebSocket) -> str | None:
    authorization = client.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()

    # Browser WebSocket APIs cannot attach an Authorization header. Query-token
    # support is therefore provided as a fallback and must only be used over WSS.
    query_token = client.query_params.get("token")
    return query_token.strip() if query_token and query_token.strip() else None


async def client_to_gemini(client: WebSocket, upstream: Any) -> None:
    while True:
        message = await client.receive()
        message_type = message["type"]
        if message_type == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1000))
        if message_type != "websocket.receive":
            continue
        if message.get("text") is not None:
            await upstream.send(message["text"])
        elif message.get("bytes") is not None:
            await upstream.send(message["bytes"])


async def gemini_to_client(upstream: Any, client: WebSocket) -> None:
    async for message in upstream:
        if isinstance(message, str):
            await client.send_text(message)
        else:
            await client.send_bytes(message)


async def bridge(client: WebSocket, settings: Settings) -> None:
    connection_id = uuid4().hex[:12]
    client_close_code = 1000
    client_close_reason = ""
    await client.accept()
    logger.info("WebSocket accepted connection_id=%s", connection_id)

    if settings.access_control_enabled:
        token = _client_token(client)
        if token is None:
            logger.info("Orbit token missing connection_id=%s", connection_id)
            await client.close(code=4401, reason="Orbit token required")
            return
        try:
            verification = await verify_subscription(token, settings)
        except SubscriptionServiceUnavailable:
            logger.warning(
                "Orbit verification unavailable connection_id=%s", connection_id
            )
            await client.close(code=1013, reason="Subscription verification unavailable")
            return
        if not verification.authorized:
            logger.info("Orbit subscription rejected connection_id=%s", connection_id)
            await client.close(code=4403, reason="Active subscription required")
            return
        logger.info("Orbit subscription accepted connection_id=%s", connection_id)

    try:
        upstream_url, upstream_headers = settings.upstream_connection()
    except ValueError as exc:
        logger.error("Configuration error connection_id=%s error=%s", connection_id, exc)
        await client.close(code=1011, reason="Proxy is not configured")
        return

    try:
        async with connect(
            upstream_url,
            additional_headers=upstream_headers or None,
            open_timeout=settings.upstream_open_timeout_seconds,
            ping_interval=settings.upstream_ping_interval_seconds,
            ping_timeout=settings.upstream_ping_timeout_seconds,
            max_size=settings.max_message_bytes,
            compression=None,
        ) as upstream:
            logger.info("Gemini session opened connection_id=%s", connection_id)
            to_gemini = asyncio.create_task(client_to_gemini(client, upstream))
            to_client = asyncio.create_task(gemini_to_client(upstream, client))
            tasks = {to_gemini, to_client}
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            for task in done:
                try:
                    task.result()
                except WebSocketDisconnect:
                    logger.info("Client disconnected connection_id=%s", connection_id)
                except ConnectionClosed as exc:
                    if task is to_client:
                        client_close_code = (
                            exc.code if 1000 <= exc.code <= 4999 else 1011
                        )
                        client_close_reason = (exc.reason or "Gemini closed")[:120]
                        logger.info(
                            "Gemini session closed connection_id=%s code=%s reason=%r",
                            connection_id,
                            exc.code,
                            exc.reason,
                        )

            if to_client in done and not to_client.cancelled():
                exception = to_client.exception()
                if exception is None:
                    upstream_code = upstream.close_code or 1000
                    client_close_code = (
                        upstream_code if 1000 <= upstream_code <= 4999 else 1011
                    )
                    client_close_reason = (upstream.close_reason or "")[:120]
                    logger.info(
                        "Gemini session closed connection_id=%s code=%s reason=%r",
                        connection_id,
                        upstream.close_code,
                        upstream.close_reason,
                    )
    except ConnectionClosed as exc:
        client_close_code = exc.code if 1000 <= exc.code <= 4999 else 1011
        client_close_reason = (exc.reason or "Gemini closed")[:120]
        logger.info(
            "Gemini session closed connection_id=%s code=%s reason=%r",
            connection_id,
            exc.code,
            exc.reason,
        )
    except (TimeoutError, OSError) as exc:
        logger.warning(
            "Gemini connection failed connection_id=%s error_type=%s",
            connection_id,
            type(exc).__name__,
        )
        with suppress(RuntimeError, WebSocketDisconnect):
            await client.close(code=1011, reason="Gemini connection failed")
    except Exception:
        logger.exception("Unexpected proxy error connection_id=%s", connection_id)
        with suppress(RuntimeError, WebSocketDisconnect):
            await client.close(code=1011, reason="Proxy error")
    finally:
        with suppress(RuntimeError, WebSocketDisconnect):
            await client.close(code=client_close_code, reason=client_close_reason)
        logger.info("Connection cleaned up connection_id=%s", connection_id)
