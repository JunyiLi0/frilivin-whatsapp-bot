"""HTTP client for the Node bridge.

``POST /send`` returns ``202`` as soon as the message is queued on the bridge
side: the 2-8 s human-like delay is applied there, *after* the response. That is
what keeps a handler's send call well inside the worker's 2 s budget.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import httpx

from whatsapp_bot.config import Settings, get_settings
from whatsapp_bot.models import Outbound


class BridgeError(RuntimeError):
    """The bridge refused or could not be reached."""


class BridgeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Bot-Token": token},
            timeout=timeout,
        )

    def send(self, outbound_id: str, out: Outbound) -> str:
        """Queue an outbound message. Returns the id acknowledged by the bridge."""
        payload = {
            "id": outbound_id,
            "jid": out.jid,
            "text": out.text,
            "quoted_id": out.quoted_id,
        }
        try:
            response = self._client.post("/send", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:200]
            raise BridgeError(
                f"bridge rejected send ({exc.response.status_code}): {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable: {exc}") from exc

        body: dict[str, Any] = response.json()
        return str(body.get("id", outbound_id))

    def groups(self) -> list[dict[str, Any]]:
        try:
            response = self._client.get("/groups")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(f"cannot list groups: {exc}") from exc
        body: dict[str, Any] = response.json()
        groups: list[dict[str, Any]] = body.get("groups", [])
        return groups

    def health(self) -> dict[str, Any]:
        try:
            response = self._client.get("/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BridgeError(f"bridge unreachable: {exc}") from exc
        body: dict[str, Any] = response.json()
        return body

    def close(self) -> None:
        self._client.close()


@lru_cache(maxsize=1)
def get_bridge_client(settings: Settings | None = None) -> BridgeClient:
    """Process-wide client — reuses the underlying connection pool."""
    settings = settings or get_settings()
    return BridgeClient(
        settings.bridge_url,
        settings.bot_token,
        timeout=settings.bridge_timeout_seconds,
    )
