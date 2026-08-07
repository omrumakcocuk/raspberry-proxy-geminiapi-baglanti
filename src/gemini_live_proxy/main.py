from __future__ import annotations

import logging
from typing import Dict, Optional

from fastapi import FastAPI, WebSocket

from .config import Settings
from .proxy import bridge


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    app = FastAPI(
        title="Gemini Live WebSocket Proxy",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = resolved_settings

    @app.on_event("startup")
    async def startup_warning() -> None:
        if not resolved_settings.access_control_enabled:
            logging.getLogger(__name__).warning(
                "Access control is disabled; use only in a trusted test environment"
            )

    @app.get("/health")
    async def health() -> Dict[str, object]:
        try:
            resolved_settings.validate()
            configured = True
        except ValueError:
            configured = False
        return {
            "status": "ok" if configured else "not_configured",
            "geminiConfigured": configured,
            "accessControlEnabled": resolved_settings.access_control_enabled,
        }

    @app.websocket("/ws/live")
    async def live(websocket: WebSocket) -> None:
        await bridge(websocket, resolved_settings)

    return app


app = create_app()
