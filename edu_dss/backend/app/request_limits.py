"""Bounded ASGI request-body buffering for JSON API endpoints."""
from __future__ import annotations

import json
from typing import Any


class RequestBodyLimitMiddleware:
    """Enforce the body limit even when clients use chunked transfer encoding."""

    def __init__(self, app, max_bytes: int):
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.app = app
        self.max_bytes = max_bytes

    async def _respond(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", ())}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._respond(send, 400, "Invalid Content-Length")
                return
            if declared_size < 0:
                await self._respond(send, 400, "Invalid Content-Length")
                return
            if declared_size > self.max_bytes:
                await self._respond(send, 413, "Request too large")
                return

        messages: list[dict[str, Any]] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self.max_bytes:
                await self._respond(send, 413, "Request too large")
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                item = messages[index]
                index += 1
                return item
            return await receive()

        await self.app(scope, replay_receive, send)
