"""Explicit browser cache policy, shared by every deployment entry point."""
from __future__ import annotations

import re

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


HASHED_ASSET = re.compile(r"^/assets/[^/]+-[A-Za-z0-9_-]{8,}\.[^/]+$")


class CachePolicyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                path = scope["path"]
                if path == "/api" or path.startswith("/api/") or message["status"] >= 400:
                    # Cookies alone do not prevent personalized responses being cached.
                    headers["Cache-Control"] = "no-store"
                elif HASHED_ASSET.fullmatch(path):
                    headers["Cache-Control"] = "public, max-age=31536000, immutable"
                else:
                    # HTML and unversioned icons retain validators, but must be checked.
                    headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_policy)
