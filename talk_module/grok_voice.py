"""Proxy WebSocket verso xAI Grok Voice Agent (Realtime API)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
from array import array

from talk_module.config import settings

log = logging.getLogger(__name__)


class _GrokGestureState:
    """Track Grok turns and fire Parla gestures once per assistant response."""

    def __init__(self) -> None:
        self.last_user_prompt = ""
        self._fired_response_ids: set[str] = set()

    @staticmethod
    def _response_id_from_event(event: dict) -> str:
        rid = str(event.get("response_id") or "").strip()
        if rid:
            return rid
        response = event.get("response")
        if isinstance(response, dict):
            return str(response.get("id") or "").strip()
        return ""

    def observe_event(self, event: dict) -> None:
        etype = str(event.get("type") or "").strip()
        if etype == "conversation.item.input_audio_transcription.completed":
            self.last_user_prompt = str(event.get("transcript") or "").strip()
            return
        if etype not in ("response.created", "response.output_audio.delta"):
            return
        response_id = self._response_id_from_event(event)
        if not response_id:
            if etype != "response.created":
                return
            response_id = "__anonymous__"
        if response_id in self._fired_response_ids:
            return
        self._fired_response_ids.add(response_id)
        self._fire_gesture()

    def _fire_gesture(self) -> None:
        try:
            from talk_module.speak_gestures import start_talk_gesture

            start_talk_gesture(self.last_user_prompt, had_robot_match=False)
            log.info(
                "[grok-voice] gesto Parla avviato (prompt=%r)",
                (self.last_user_prompt[:80] + "…") if len(self.last_user_prompt) > 80 else self.last_user_prompt,
            )
        except Exception:
            log.exception("[grok-voice] errore avvio gesto Parla")


def grok_realtime_url() -> str:
    agent = (settings.xai_agent_id or "").strip()
    base = "wss://api.x.ai/v1/realtime"
    if agent:
        return f"{base}?agent_id={agent}"
    return f"{base}?model=grok-voice-latest"


def grok_voice_configured() -> bool:
    return bool((settings.xai_api_key or "").strip())


async def proxy_grok_voice(
    client_ws,
    *,
    local_mic: bool = False,
    input_gain: float = 1.0,
    threshold: int = 20,
) -> None:
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
    input_gain = max(0.4, min(float(input_gain), 4.0))
    threshold = max(1, min(int(threshold), 80))

    try:
        async with websockets.connect(
            url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as xai_ws:
            client_send_lock = asyncio.Lock()
            session_ready = asyncio.Event()

            async def send_client(payload: str) -> None:
                async with client_send_lock:
                    await client_ws.send_text(payload)

            await send_client(
                json.dumps(
                    {
                        "type": "proxy.ready",
                        "agent_id": settings.xai_agent_id or "",
                        "url_hint": "xai-realtime",
                        "input_source": "jetson" if local_mic else "browser",
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
                            await send_client(
                                json.dumps({"type": "error", "error": "JSON non valido"})
                            )
                            continue
                        if data.get("type") == "proxy.ping":
                            await send_client(json.dumps({"type": "proxy.pong"}))
                            continue
                        await xai_ws.send(raw)
                except WebSocketDisconnect:
                    pass

            gesture_state = _GrokGestureState()

            async def forward_xai() -> None:
                try:
                    async for message in xai_ws:
                        try:
                            event = json.loads(message)
                            if event.get("type") == "session.updated":
                                session_ready.set()
                            else:
                                await asyncio.to_thread(gesture_state.observe_event, event)
                        except (json.JSONDecodeError, AttributeError):
                            pass
                        await send_client(message)
                except Exception as exc:
                    log.warning("[grok-voice] stream xAI terminato: %s", exc)

            async def forward_local_mic() -> None:
                """Cattura il microfono USB della Jetson via PulseAudio e invia PCM16/24 kHz."""
                await session_ready.wait()
                process = None
                try:
                    from talk_module.audio.device_utils import ensure_pulse_usb_microphone_source

                    source_name = await asyncio.to_thread(ensure_pulse_usb_microphone_source)
                    if not source_name:
                        raise RuntimeError("sorgente PulseAudio DJI non disponibile")
                    process = await asyncio.create_subprocess_exec(
                        "arecord",
                        "-q",
                        "-D",
                        "pulse",
                        "-t",
                        "raw",
                        "-f",
                        "S16_LE",
                        "-r",
                        "24000",
                        "-c",
                        "1",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await send_client(
                        json.dumps({"type": "proxy.mic_info", "name": "DJI Mic (USB Jetson)"})
                    )
                    level_tick = 0
                    while True:
                        chunk = await process.stdout.read(4800)
                        if not chunk:
                            err = await process.stderr.read()
                            raise RuntimeError(
                                (err.decode("utf-8", errors="replace").strip() or "stream microfono terminato")[:240]
                            )
                        usable = len(chunk) - (len(chunk) % 2)
                        samples = array("h")
                        samples.frombytes(chunk[:usable])
                        if not samples:
                            continue
                        peak = max(abs(sample) for sample in samples) / 32768.0
                        peak255 = min(255, round(peak * 255 * input_gain))
                        if peak255 < threshold:
                            pcm = bytes(usable)
                        elif input_gain != 1.0:
                            adjusted = array(
                                "h",
                                (
                                    max(-32768, min(32767, round(sample * input_gain)))
                                    for sample in samples
                                ),
                            )
                            pcm = adjusted.tobytes()
                        else:
                            pcm = chunk[:usable]
                        await xai_ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm).decode("ascii"),
                                }
                            )
                        )
                        level_tick += 1
                        if level_tick % 2 == 0:
                            rms = math.sqrt(
                                sum(float(sample) * float(sample) for sample in samples)
                                / len(samples)
                            ) / 32768.0
                            await send_client(
                                json.dumps(
                                    {
                                        "type": "proxy.mic_level",
                                        "peak": round(peak, 5),
                                        "rms": round(rms, 5),
                                        "peak255": peak255,
                                    }
                                )
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("[grok-voice] microfono locale terminato")
                    with contextlib.suppress(Exception):
                        await send_client(
                            json.dumps({"type": "error", "error": f"Microfono DJI: {str(exc)[:220]}"})
                        )
                finally:
                    if process and process.returncode is None:
                        process.terminate()
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(process.wait(), timeout=2)

            client_task = asyncio.create_task(forward_client())
            xai_task = asyncio.create_task(forward_xai())
            local_mic_task = asyncio.create_task(forward_local_mic()) if local_mic else None
            tasks = {client_task, xai_task}
            if local_mic_task:
                tasks.add(local_mic_task)
            done, pending = await asyncio.wait(
                tasks,
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
