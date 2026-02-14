from __future__ import annotations

import asyncio
import json
import ssl
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


def normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("Home Assistant base URL must start with http:// or https://")
    return base_url


def to_absolute_url(base_url: str, maybe_relative: str) -> str:
    if maybe_relative.startswith(("http://", "https://")):
        return maybe_relative
    return urljoin(base_url + "/", maybe_relative.lstrip("/"))


def to_ws_url(base_url: str) -> str:
    base_url = normalize_base_url(base_url)
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/api/websocket"
    return "ws://" + base_url[len("http://") :] + "/api/websocket"


def http_get_bytes(url: str, token: str, *, insecure_ssl: bool = False, timeout_s: float = 20.0) -> bytes:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "bk-light/ha-client",
    }
    req = Request(url, headers=headers, method="GET")
    context = ssl._create_unverified_context() if insecure_ssl else None
    with urlopen(req, context=context, timeout=timeout_s) as resp:
        return resp.read()


def ha_get_state(base_url: str, token: str, entity_id: str, *, insecure_ssl: bool = False) -> dict[str, Any]:
    base_url = normalize_base_url(base_url)
    url = to_absolute_url(base_url, f"/api/states/{quote(entity_id)}")
    raw = http_get_bytes(url, token, insecure_ssl=insecure_ssl)
    return json.loads(raw.decode("utf-8"))


def ha_get_entity_picture_url(
    base_url: str, token: str, entity_id: str, *, insecure_ssl: bool = False
) -> str:
    st = ha_get_state(base_url, token, entity_id, insecure_ssl=insecure_ssl)
    attr = (st or {}).get("attributes") or {}
    rel = attr.get("entity_picture") or attr.get("entity_picture_local")
    if not rel:
        rel = f"/api/media_player_proxy/{quote(entity_id)}"
    return to_absolute_url(normalize_base_url(base_url), rel)


def ha_fetch_entity_picture_bytes(
    base_url: str, token: str, entity_id: str, *, insecure_ssl: bool = False
) -> bytes:
    url = ha_get_entity_picture_url(base_url, token, entity_id, insecure_ssl=insecure_ssl)
    return http_get_bytes(url, token, insecure_ssl=insecure_ssl)


@dataclass(frozen=True)
class HassStateChangedEvent:
    entity_id: str
    old_state: Optional[dict[str, Any]]
    new_state: Optional[dict[str, Any]]
    raw: dict[str, Any]


class HomeAssistantWS:
    """
    Minimal Home Assistant WebSocket client (auth + subscribe state_changed).
    Requires `websockets` package.
    """

    def __init__(self, base_url: str, token: str, *, ping_interval_s: float = 30.0):
        self.base_url = normalize_base_url(base_url)
        self.token = token
        self.ping_interval_s = max(5.0, float(ping_interval_s))
        self._next_id = 1

    def _next(self) -> int:
        nid = self._next_id
        self._next_id += 1
        return nid

    async def connect(self):
        try:
            import websockets  # type: ignore
        except ImportError as e:
            raise ImportError("Missing dependency: websockets. Install with: pip install websockets") from e

        ws_url = to_ws_url(self.base_url)
        ws = await websockets.connect(ws_url, ping_interval=None)

        # Handshake
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_required":
            await ws.close()
            raise RuntimeError(f"Unexpected first WS message: {msg}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(await ws.recv())
        if msg.get("type") != "auth_ok":
            await ws.close()
            raise RuntimeError(f"Auth failed: {msg}")

        return ws

    async def subscribe_state_changed(
        self,
        *,
        entity_id: Optional[str] = None,
    ) -> AsyncIterator[HassStateChangedEvent]:
        ws = await self.connect()
        sub_id = self._next()
        await ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}))
        res = json.loads(await ws.recv())
        if res.get("type") != "result" or res.get("id") != sub_id or not res.get("success"):
            await ws.close()
            raise RuntimeError(f"Failed to subscribe: {res}")

        async def ping_loop():
            while True:
                await asyncio.sleep(self.ping_interval_s)
                try:
                    await ws.send(json.dumps({"id": self._next(), "type": "ping"}))
                except Exception:
                    return

        ping_task = asyncio.create_task(ping_loop())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "event" or msg.get("id") != sub_id:
                    continue
                ev = msg.get("event") or {}
                data = ev.get("data") or {}
                ent = data.get("entity_id")
                if not ent:
                    continue
                if entity_id and ent != entity_id:
                    continue
                yield HassStateChangedEvent(
                    entity_id=ent,
                    old_state=data.get("old_state"),
                    new_state=data.get("new_state"),
                    raw=msg,
                )
        finally:
            ping_task.cancel()
            try:
                await ws.close()
            except Exception:
                pass


async def watch_state_changes_forever(
    base_url: str,
    token: str,
    *,
    entity_id: str,
    on_event,
    reconnect_delay_s: float = 2.0,
) -> None:
    """
    Convenience loop with reconnect handling.
    `on_event` is an async function taking HassStateChangedEvent.
    """

    delay = max(0.5, float(reconnect_delay_s))
    while True:
        try:
            client = HomeAssistantWS(base_url, token)
            async for ev in client.subscribe_state_changed(entity_id=entity_id):
                await on_event(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(delay)


