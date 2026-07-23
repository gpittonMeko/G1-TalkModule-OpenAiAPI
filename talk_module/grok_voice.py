"""Proxy WebSocket verso xAI Grok Voice Agent (Realtime API)."""

from __future__ import annotations

import asyncio
import json
import logging

from talk_module.config import settings

log = logging.getLogger(__name__)


def grok_realtime_url() -> str:
    agent = (settings.xai_agent_id or "").strip()
    base = "wss://api.x.ai/v1/realtime"
    if agent:
        return f"{base}?agent_id={agent}"
    return f"{base}?model=grok-voice-latest"


def grok_voice_configured() -> bool:
    return bool((settings.xai_api_key or "").strip())


async def proxy_grok_voice(client_ws) -> None:
    """Inoltra messaggi tra browser e xAI; la API key resta solo sul server."""
    from fastapi import WebSocketDisconnect

    if not grok_voice_configured():
        await client_ws.send_text(
            json.dumps({"type": "error", "error": "XAI_API_KEY non configurata sul server (.env)"})
        )
        await client_ws.close()
        return

    try:
        import websockets
    except ImportError:
        await client_ws.send_text(
            json.dumps({"type": "error", "error": "Pacchetto websockets mancante sul server"})
        )
        await client_ws.close()
        return

    url = grok_realtime_url()
    headers = {"Authorization": f"Bearer {settings.xai_api_key.strip()}"}

    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as xai_ws:
            await client_ws.send_text(
                json.dumps(
                    {
                        "type": "proxy.ready",
                        "agent_id": settings.xai_agent_id or "",
                        "url_hint": "xai-realtime",
                    }
                )
            )

            async def forward_client() -> None:
                try:
                    while True:
                        raw = await client_ws.receive_text()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            await client_ws.send_text(
                                json.dumps({"type": "error", "error": "JSON non valido"})
                            )
                            continue
                        if data.get("type") == "proxy.ping":
                            await client_ws.send_text(json.dumps({"type": "proxy.pong"}))
                            continue
                        await xai_ws.send(raw)
                except WebSocketDisconnect:
                    pass

            async def forward_xai() -> None:
                try:
                    async for message in xai_ws:
                        await client_ws.send_text(message)
                except Exception as exc:
                    log.warning("[grok-voice] stream xAI terminato: %s", exc)

            client_task = asyncio.create_task(forward_client())
            xai_task = asyncio.create_task(forward_xai())
            done, pending = await asyncio.wait(
                {client_task, xai_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    except Exception as exc:
        log.exception("[grok-voice] connessione fallita")
        try:
            await client_ws.send_text(json.dumps({"type": "error", "error": str(exc)[:240]}))
        except Exception:
            pass
