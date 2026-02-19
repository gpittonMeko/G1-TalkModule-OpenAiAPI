"""
Web App G1 Talk Module - AI Accelerator.
Wizard setup: seleziona microfono e altoparlante (locale o client web nella rete).
Tutto gira sulla macchina AI Accelerator.
"""

import asyncio
import base64
import json
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_executor = ThreadPoolExecutor(max_workers=2)

# FastAPI + WebSocket
try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
    from fastapi.responses import HTMLResponse, RedirectResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from talk_module.config import settings
import os
from talk_module.network_discovery import (
    register_web_client,
    unregister_web_client,
    list_network_clients,
)

# Config file per scelte utente
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "audio_devices.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_device_config() -> dict:
    """Carica configurazione microfono/speaker da file."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"microphone": None, "speaker": None}


def save_device_config(cfg: dict) -> None:
    """Salva configurazione."""
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _save_debug_audio(audio_bytes: bytes) -> str | None:
    """Salva audio ricevuto in temp/audio/debug per verifica. Ritorna path."""
    try:
        from datetime import datetime
        settings.ensure_dirs()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = settings.audio_dir / f"debug_{ts}.webm"
        out.write_bytes(audio_bytes)
        return f"{out.name} ({len(audio_bytes)} byte)"
    except Exception as e:
        print(f"[Debug] Salvataggio audio fallito: {e}")
        return None


# WebSocket clients: {client_id: ws}
_ws_clients: dict = {}


if HAS_FASTAPI:
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="G1 Talk Module",
        description="Setup e controllo vocale - AI Accelerator",
        version="1.0.0",
    )

    @app.exception_handler(Exception)
    def _json_exception_handler(request, exc):
        """Ritorna sempre JSON, mai HTML."""
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "message": str(exc.detail)})
        return JSONResponse(status_code=500, content={"message": str(exc), "detail": "Errore interno"})

    # Lazy init dei servizi (richiedono API key)
    _stt = _llm = _tts = _player = _recorder = None

    def get_services():
        global _stt, _llm, _tts, _player, _recorder
        if _stt is None:
            from talk_module.stt import WhisperClient
            from talk_module.llm import LLMClient
            from talk_module.tts import TTSClient
            _stt = WhisperClient()
            _llm = LLMClient()
            _tts = TTSClient()
            _player = _recorder = None
            try:
                from talk_module.audio import AudioRecorder, AudioPlayer, _AUDIO_AVAILABLE
                if _AUDIO_AVAILABLE and AudioRecorder:
                    cfg = load_device_config()
                    mic_cfg = cfg.get("microphone")
                    mic_id = mic_cfg.get("device_id") if isinstance(mic_cfg, dict) and mic_cfg.get("type") == "local" else None
                    spk_cfg = cfg.get("speaker")
                    spk_id = spk_cfg.get("device_id") if isinstance(spk_cfg, dict) and spk_cfg.get("type") == "local" else None
                    _player = AudioPlayer(device_id=spk_id)
                    _recorder = AudioRecorder(device_id=mic_id)
            except (OSError, ImportError, TypeError, AttributeError, NameError):
                pass
        return _stt, _llm, _tts, _player, _recorder

    @app.get("/")
    def index():
        return RedirectResponse(url="/client", status_code=302)

    @app.get("/client", response_class=HTMLResponse)
    def client_page():
        return CLIENT_TEMPLATE

    @app.get("/local")
    @app.get("/listen")
    @app.get("/local-fetch")
    def _redir_client():
        return RedirectResponse(url="/client", status_code=302)


    @app.get("/api/health")
    def health():
        return {"status": "ok", "host": "AI Accelerator"}

    @app.get("/api/version")
    def api_version():
        return {"version": "2", "deploy": "ok"}

    @app.get("/api/debug-audio")
    def api_debug_audio():
        """Lista ultimi file audio salvati per debug (temp/audio/debug_*.webm)."""
        settings.ensure_dirs()
        files = sorted(settings.audio_dir.glob("debug_*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
        return {
            "dir": str(settings.audio_dir),
            "files": [{"name": f.name, "size": f.stat().st_size, "path": str(f)} for f in files[:20]],
        }

    def _collapse_ape(devices: list) -> list:
        """Riduce i 20+ canali 'NVIDIA Jetson AGX Orin APE' a uno solo (primo canale)."""
        seen_base = set()
        out = []
        for d in devices:
            name = d.get("name", "")
            base = name.split("(")[0].strip() if "(" in name else name
            if "NVIDIA Jetson" in name and "APE" in name:
                if base in seen_base:
                    continue
                seen_base.add(base)
                d = {**d, "name": base + " (primo canale)"}
            out.append(d)
        return out

    @app.get("/api/devices")
    def api_devices(all: bool = False):
        """Lista dispositivi: integrati, USB, Bluetooth, rete WiFi."""
        inputs, outputs = [], []
        try:
            from talk_module.audio.device_utils import list_microphones, list_speakers, TYPE_LABELS
            mics_raw = list_microphones(physical_only=not all)
            spks_raw = list_speakers(physical_only=not all)
            if (not mics_raw or not spks_raw) and not all:
                mics_raw = list_microphones(physical_only=False)
                spks_raw = list_speakers(physical_only=False)
            if not all:
                mics_raw = _collapse_ape(mics_raw)
                spks_raw = _collapse_ape(spks_raw)
            def _label(d):
                name = d.get("name", "?")
                t = d.get("device_type", "")
                lbl = TYPE_LABELS.get(t, "")
                if lbl:
                    return f"{name} ({lbl})"
                return name
            inputs = [{"type": "local", "device_id": d["index"], "name": _label(d), "value": f"local_{d['index']}"}
                     for d in mics_raw]
            outputs = [{"type": "local", "device_id": d["index"], "name": _label(d), "value": f"local_{d['index']}"}
                      for d in spks_raw]
        except (OSError, Exception):
            pass
        # Dispositivi di rete: client web connessi (apri /client sul telefono o G1)
        net = list_network_clients()
        for n in net:
            inputs.append({**n, "name": n["name"] + " (microfono)"})
            outputs.append({**n, "name": n["name"] + " (altoparlante)"})
        if not net:
            inputs.append({"type": "network", "value": "web_wait", "name": "Rete WiFi: apri /client su telefono o G1"})
            outputs.append({"type": "network", "value": "web_wait", "name": "Rete WiFi: apri /client su telefono o G1"})
        bt = []
        try:
            from talk_module.audio.device_utils import list_bluetooth_devices_available
            bt = list_bluetooth_devices_available()
        except Exception:
            pass
        return {"microphones": inputs, "speakers": outputs, "network_clients": net, "bluetooth_paired": bt}

    @app.get("/api/devices-check")
    def api_devices_check():
        """Test: verifica che il server veda microfoni e altoparlanti (come Teams sul server)."""
        try:
            from talk_module.audio.device_utils import list_microphones, list_speakers
            m = list_microphones(physical_only=False)
            s = list_speakers(physical_only=False)
            m = _collapse_ape(m) if m else []
            s = _collapse_ape(s) if s else []
            return {
                "ok": True,
                "microphones_count": len(m),
                "speakers_count": len(s),
                "microphones": [{"id": d["index"], "name": d.get("name", "?")} for d in m[:10]],
                "speakers": [{"id": d["index"], "name": d.get("name", "?")} for d in s[:10]],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/config")
    def api_get_config():
        return load_device_config()

    @app.post("/api/config")
    def api_save_config(data: dict = Body(...)):
        save_device_config(data)
        return {"ok": True}

    WAKE_WORDS = ("hey markone", "hey mark one", "ehi markone", "ehi mark one")
    WAKE_WORD_REQUIRED = os.getenv("WAKE_WORD_REQUIRED", "true").lower() in ("1", "true", "yes")

    def _extract_prompt(text: str, skip_wake_word: bool = False, audio_size: int = 0):
        """Ritorna (prompt, message). Se wake word trovata: (resto, ""). Altrimenti: (None, msg)."""
        if not text or not text.strip():
            debug = f" ({audio_size} byte inviati)" if audio_size else ""
            return None, f"Nessun testo riconosciuto{debug}. Parla piu a lungo (1-2 sec) e vicino al microfono."
        t = text.strip()
        if any(h in t.lower() for h in ("sottotitoli", "amara.org", "amara ", "qtss", "subtitle", "created by", "a cura di")):
            return None, "Audio non chiaro. Riprova a parlare piu vicino al microfono."
        if skip_wake_word or not WAKE_WORD_REQUIRED:
            return t, ""
        # Match "hey/ehi" + "markone/mark one" all'inizio (flessibile su punteggiatura)
        m = re.match(r"^(?:hey|ehi)\s*[,.\s]*\s*(?:mark\s*one|markone)\s*[,.\s]*\s*(.*)$", t, re.IGNORECASE)
        if m:
            rest = m.group(1).strip()
            if not rest:
                return None, "Di 'Hey Markone' seguito dalla domanda. Es: Hey Markone, che ore sono?"
            return rest, ""
        return None, "Di 'Hey Markone' seguito dalla tua domanda per attivarmi."

    def _process_audio(audio_bytes: bytes, skip_wake_word: bool = False, format_hint: str = "webm") -> dict:
        """Pipeline: audio -> STT -> LLM -> TTS. skip_wake_word=True per pulsante Parla."""
        try:
            stt, llm, tts, _, _ = get_services()
            text = stt.transcribe(audio_bytes, format_hint=format_hint)
            prompt, msg = _extract_prompt(text or "", skip_wake_word=skip_wake_word, audio_size=len(audio_bytes))
            if msg:
                return {"text": text or "", "response": "", "audio_base64": "", "message": msg}
            resp = llm.chat(prompt)
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            return {
                "text": text,
                "response": resp or "",
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
            }
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}"}

    @app.post("/api/voice-chat")
    async def api_voice_chat(audio_b64: str = None):
        """Pipeline: audio base64 -> STT -> LLM -> TTS. Ritorna response + audio base64."""
        errs = settings.validate()
        if errs:
            raise HTTPException(400, "; ".join(errs))
        if not audio_b64:
            raise HTTPException(400, "audio_b64 mancante")
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception:
            raise HTTPException(400, "audio base64 non valido")
        if len(audio_bytes) < 500:
            raise HTTPException(400, "Audio troppo corto")
        return _process_audio(audio_bytes)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        """WebSocket: client invia audio, riceve TTS. Si registra come dispositivo di rete."""
        await ws.accept()
        client_ip = ws.client.host if ws.client else "unknown"
        client_id = f"{client_ip}_{id(ws)}"
        register_web_client(client_id, client_ip, ws)
        _ws_clients[client_id] = ws
        try:
            while True:
                msg = await ws.receive_text()
                data = json.loads(msg)
                if data.get("type") == "audio":
                    audio_b64 = data.get("data")
                    play_on = data.get("play_on", "browser")  # "browser" | "server"
                    out_device_id = data.get("device_id") if play_on == "server" else None
                    if audio_b64:
                        try:
                            audio_bytes = base64.b64decode(audio_b64)
                            result = _process_audio(audio_bytes, skip_wake_word=True, format_hint="webm")
                            if play_on == "server" and out_device_id is not None and result.get("audio_base64"):
                                try:
                                    from talk_module.audio import AudioPlayer
                                    p = AudioPlayer(device_id=int(out_device_id))
                                    p.play_bytes(base64.b64decode(result["audio_base64"]), format_hint="mp3")
                                except Exception:
                                    pass
                            await ws.send_text(json.dumps({"type": "response", "data": result}))
                        except Exception as e:
                            await ws.send_text(json.dumps({"type": "error", "data": str(e)}))
        except WebSocketDisconnect:
            pass
        finally:
            unregister_web_client(client_id)
            _ws_clients.pop(client_id, None)

    @app.websocket("/ws/listen")
    async def websocket_listen(ws: WebSocket):
        """Ascolto continuo: Hey Markone attiva, 10 sec silenzio = stop. Solo mic/speaker locali."""
        await ws.accept()
        cfg = load_device_config()
        if not cfg.get("microphone") or cfg.get("microphone", {}).get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura microfono locale dal setup"}))
            await ws.close()
            return
        if cfg.get("speaker", {}).get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura altoparlante locale dal setup"}))
            await ws.close()
            return

        listen_queue = queue.Queue()
        listen_stop = threading.Event()

        def _record_loop():
            try:
                _, _, _, _, recorder = get_services()
                if not recorder:
                    listen_queue.put({"error": "PortAudio non disponibile"})
                    return
                for audio_bytes in recorder.record_until_silence(
                    silence_seconds=6,
                    chunk_duration=0.5,
                    silence_threshold=0.01,
                    max_duration=60.0,
                    stop_check=lambda: listen_stop.is_set(),
                ):
                    if listen_stop.is_set():
                        break
                    if len(audio_bytes) > 500:
                        listen_queue.put(audio_bytes)
            except Exception as e:
                listen_queue.put({"error": str(e)})

        rec_thread = threading.Thread(target=_record_loop, daemon=True)
        rec_thread.start()

        try:
            await ws.send_text(json.dumps({"type": "status", "data": "In ascolto. Di 'Hey Markone'..."}))
            loop = asyncio.get_running_loop()
            _, _, _, player, _ = get_services()
            while True:
                try:
                    item = await loop.run_in_executor(_executor, lambda: listen_queue.get(timeout=0.5))
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue
                if isinstance(item, dict) and "error" in item:
                    await ws.send_text(json.dumps({"type": "error", "data": item["error"]}))
                    break
                result = _process_audio(item, skip_wake_word=False)
                await ws.send_text(json.dumps({"type": "response", "data": result}))
                if result.get("audio_base64") and player:
                    import base64 as b64
                    player.play_bytes(b64.b64decode(result["audio_base64"]), format_hint="mp3")
        except WebSocketDisconnect:
            pass
        finally:
            listen_stop.set()

    @app.websocket("/ws/parla")
    async def websocket_parla(ws: WebSocket):
        """Push-to-talk: tieni premuto per registrare, rilascia per inviare."""
        await ws.accept()
        cfg = load_device_config()
        if not cfg.get("microphone") or cfg.get("microphone", {}).get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura microfono locale dal setup"}))
            await ws.close()
            return
        if cfg.get("speaker", {}).get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura altoparlante locale dal setup"}))
            await ws.close()
            return

        ptt_stop = threading.Event()
        ptt_queue = queue.Queue()
        recording = False

        try:
            while True:
                msg = await ws.receive_text()
                data = json.loads(msg)
                if data.get("type") == "start":
                    if recording:
                        continue
                    recording = True
                    ptt_stop.clear()

                    def _do_record():
                        try:
                            _, _, _, _, recorder = get_services()
                            if not recorder:
                                ptt_queue.put({"error": "PortAudio non disponibile"})
                                return
                            audio = recorder.record_until_stop(lambda: ptt_stop.is_set(), chunk_duration=0.2, min_duration=0.3)
                            ptt_queue.put(audio)
                        except Exception as e:
                            ptt_queue.put({"error": str(e)})

                    threading.Thread(target=_do_record, daemon=True).start()
                elif data.get("type") == "stop":
                    if not recording:
                        continue
                    ptt_stop.set()
                    recording = False
                    loop = asyncio.get_running_loop()
                    try:
                        item = await asyncio.wait_for(loop.run_in_executor(_executor, lambda: ptt_queue.get(timeout=15)), 20)
                    except (queue.Empty, asyncio.TimeoutError):
                        recording = False
                        continue
                    if isinstance(item, dict) and "error" in item:
                        await ws.send_text(json.dumps({"type": "error", "data": item["error"]}))
                        continue
                    if len(item) < 500:
                        await ws.send_text(json.dumps({"type": "response", "data": {"text": "", "response": "", "audio_base64": "", "message": "Registrazione troppo corta"}}))
                        continue
                    result = _process_audio(item, skip_wake_word=True)
                    await ws.send_text(json.dumps({"type": "response", "data": result}))
                    if result.get("audio_base64"):
                        _, _, _, player, _ = get_services()
                        if player:
                            import base64 as b64
                            player.play_bytes(b64.b64decode(result["audio_base64"]), format_hint="mp3")
                    recording = False
        except WebSocketDisconnect:
            pass
        finally:
            ptt_stop.set()

    @app.post("/api/record-and-process")
    async def api_record_and_process(duration: float = Body(10, embed=True)):
        """Registra da microfono locale, elabora, riproduce localmente. Per setup locale."""
        errs = settings.validate()
        if errs:
            raise HTTPException(400, "; ".join(errs))
        cfg = load_device_config()
        if not cfg.get("microphone") or cfg.get("microphone", {}).get("type") != "local":
            raise HTTPException(400, "Configura microfono locale (PortAudio) dal setup")
        spk = cfg.get("speaker") or {}
        if spk.get("type") not in ("local", "network"):
            raise HTTPException(400, "Configura altoparlante locale o di rete dal setup")

        def _do_record():
            _, _, _, player, recorder = get_services()
            if not recorder:
                return {"text": "", "response": "", "message": "PortAudio non disponibile. Esegui: sudo bash scripts/install_audio_jetson.sh"}
            audio = recorder.record_fixed_duration(min(max(duration, 2), 30))
            if not audio or len(audio) < 500:
                return {"text": "", "response": "", "message": "Audio non registrato"}
            # Pulsante Parla: risponde sempre, senza wake word
            result = _process_audio(audio, skip_wake_word=True)
            spk_cfg = cfg.get("speaker", {})
            if result.get("audio_base64"):
                audio_b64 = result["audio_base64"]
                if isinstance(spk_cfg, dict) and spk_cfg.get("type") == "network":
                    result["_send_to_client"] = spk_cfg.get("value", "").replace("net_", "")
                    result["_audio_b64"] = audio_b64
                elif player:
                    import base64 as b64
                    player.play_bytes(b64.b64decode(audio_b64), format_hint="mp3")
            return result

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(_executor, _do_record)
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": str(e)}
        if result.get("_send_to_client"):
            cid = result.pop("_send_to_client", None)
            audio_b64 = result.pop("_audio_b64", None)
            if cid and cid in _ws_clients and audio_b64:
                try:
                    await _ws_clients[cid].send_text(json.dumps({"type": "play", "data": audio_b64}))
                except Exception:
                    pass
        return result


# HTML Template - Setup
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Setup</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 24px; background: #0f0f12; color: #e4e4e7; max-width: 620px; margin: 0 auto; }
    h1 { font-size: 1.5rem; margin-bottom: 8px; }
    .step { background: #18181b; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #27272a; }
    .step h2 { font-size: 1rem; margin: 0 0 12px; color: #a1a1aa; font-weight: 600; }
    .step .device-label { font-size: 12px; color: #71717a; margin-bottom: 6px; }
    select { width: 100%; padding: 14px 16px; border-radius: 8px; border: 2px solid #3f3f46; background: #27272a; color: #fff; font-size: 15px; min-height: 48px; cursor: pointer; }
    select:focus { outline: none; border-color: #3b82f6; }
    select option { padding: 8px; background: #1e1e22; color: #fff; }
    button { padding: 12px 24px; border-radius: 8px; border: none; font-weight: 600; cursor: pointer; font-size: 14px; margin-right: 8px; margin-bottom: 8px; }
    .btn-primary { background: #3b82f6; color: white; }
    .btn-primary:hover { background: #2563eb; }
    .btn-secondary { background: #3f3f46; color: white; }
    .link-box { background: #27272a; padding: 16px; border-radius: 8px; margin: 12px 0; font-size: 13px; word-break: break-all; }
    .link-box strong { color: #3b82f6; }
    .hint { color: #71717a; font-size: 13px; margin-top: 8px; line-height: 1.5; }
    .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #3f3f46; border-top-color: #3b82f6; border-radius: 50%; animation: spin 0.8s linear infinite; vertical-align: middle; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .status { font-size: 13px; color: #a1a1aa; margin-top: 4px; }
    .device-count { font-size: 12px; color: #22c55e; margin-top: 4px; }
  </style>
</head>
<body>
  <h1>G1 Talk Module</h1>
  <div style="background:#14532d;border:2px solid #22c55e;border-radius:12px;padding:20px;margin-bottom:20px;font-size:15px;">
    <strong style="color:#86efac;">Mic e cuffie sul TUO PC, elaborazione sull'AI Accelerator</strong>
    <p style="margin:12px 0 0;color:#e4e4e7;">Usa il microfono e le cuffie del tuo PC. Whisper, GPT e TTS girano sull’AI Accelerator.</p>
    <p style="margin:12px 0 0;"><a href="/client" style="display:inline-block;padding:14px 24px;background:#22c55e;color:#0f0f12;border-radius:8px;text-decoration:none;font-weight:700;">Avvia (Parla)</a></p>
  </div>
  <p style="color:#71717a;font-size:14px;">Oppure: dispositivi sull’AI Accelerator (per Parla/Ascolto sul robot).</p>

  <div class="step">
    <h2>1. Microfono</h2>
    <div id="searchStatus"><span class="spinner" id="mainSpinner" style="display:none"></span> <span id="statusText"></span></div>
    <p class="device-label">Scegli il microfono</p>
    <select id="mic" aria-label="Seleziona microfono"><option value="web_wait">Rete WiFi: apri /client su telefono o G1</option></select>
    <p class="hint">Integrati, USB, Bluetooth. Oppure Rete WiFi.</p>
    <p class="device-count" id="micCount"></p>
  </div>
  <div class="step">
    <h2>2. Altoparlante / Cuffie</h2>
    <p class="device-label">Scegli altoparlante o cuffie</p>
    <select id="speaker" aria-label="Seleziona altoparlante o cuffie"><option value="web_wait">Rete WiFi: apri /client su telefono o G1</option></select>
    <p class="hint">Integrati, cuffie (jack/USB), speaker USB/Bluetooth.</p>
    <p class="device-count" id="spkCount"></p>
  </div>
  <div class="step" id="btStep" style="display:none">
    <h2>Bluetooth accoppiati</h2>
    <p id="btList" class="hint"></p>
    <p class="hint">Connetti dalle impostazioni di sistema, poi clicca Aggiorna.</p>
  </div>
  <div class="step">
    <p style="margin:0 0 8px;color:#a1a1aa;font-size:13px;">Rete WiFi (telefono, G1, tablet):</p>
    <div class="link-box"><strong id="clientUrl">-</strong></div>
    <button class="btn-secondary" onclick="refreshDevices()">Aggiorna ricerca</button>
    <button class="btn-secondary" id="showAllBtn" onclick="toggleShowAll()">Mostra tutti i dispositivi</button>
  </div>
  <button class="btn-primary" onclick="saveConfig()">Salva configurazione</button>
  <div style="margin-top:24px;padding-top:20px;border-top:1px solid #3f3f46;">
    <p style="color:#a1a1aa;font-size:13px;margin-bottom:12px;">Modalità (usa dopo aver salvato):</p>
    <p style="margin:8px 0;"><a href="/local" style="display:inline-block;padding:12px 20px;background:#3b82f6;color:white;border-radius:8px;text-decoration:none;font-weight:600;">Parla</a> <span class="hint">— Tieni premuto, parla, rilascia. Mic e cuffie sull’AI Accelerator.</span></p>
    <p style="margin:8px 0;"><a href="/listen" style="display:inline-block;padding:12px 20px;background:#3b82f6;color:white;border-radius:8px;text-decoration:none;font-weight:600;">Ascolto</a> <span class="hint">— Di’ «Hey Markone» + domanda. Mic e cuffie sull’AI Accelerator.</span></p>
    <p style="margin:8px 0;"><a href="/client" style="display:inline-block;padding:12px 20px;background:#3f3f46;color:white;border-radius:8px;text-decoration:none;font-weight:600;">Client rete</a> <span class="hint">— Apri su telefono/tablet: userai mic e cuffie di quel dispositivo.</span></p>
  </div>

  <script>
    document.getElementById('clientUrl').textContent = location.origin + '/client';
    let mics = [], spks = [], showAll = false;
    function setLoading(loading){
      document.getElementById('mainSpinner').style.display = loading ? 'inline-block' : 'none';
      document.getElementById('statusText').textContent = loading ? 'Ricerca in corso...' : '';
    }
    function renderSelects(){
      const opts = m=>'<option value="'+m.value+'">'+m.name+'</option>';
      document.getElementById('mic').innerHTML = mics.map(opts).join('');
      document.getElementById('speaker').innerHTML = spks.map(opts).join('');
    }
    function loadDevices(){
      setLoading(true);
      const url = '/api/devices' + (showAll ? '?all=1' : '');
      const fallback = ()=>{ mics=[{value:'web_wait',name:'Rete WiFi: apri /client su telefono o G1'}]; spks=[...mics]; renderSelects(); setLoading(false); };
      const ctrl = new AbortController();
      setTimeout(()=>ctrl.abort(), 5000);
      fetch(url, {signal: ctrl.signal}).then(r=>{ if(!r.ok) throw new Error(r.status); return r.json(); }).then(d=>{
        mics = d.microphones || [];
        spks = d.speakers || [];
        if(!mics.length) mics=[{value:'web_wait',name:'Rete WiFi'}];
        if(!spks.length) spks=[...mics];
        const bt = d.bluetooth_paired || [];
        if(bt.length){ document.getElementById('btStep').style.display='block'; document.getElementById('btList').textContent = bt.map(b=>b.name).join(', '); }
        else document.getElementById('btStep').style.display='none';
        renderSelects();
        setLoading(false);
        document.getElementById('showAllBtn').textContent = showAll ? 'Solo consigliati' : 'Mostra tutti i dispositivi';
        const micLocal = mics.filter(m => m.type === 'local' || (m.value && m.value.startsWith('local_')));
        const spkLocal = spks.filter(s => s.type === 'local' || (s.value && s.value.startsWith('local_')));
        document.getElementById('micCount').textContent = micLocal.length ? micLocal.length + ' microfono/i rilevato/i' : '';
        document.getElementById('spkCount').textContent = spkLocal.length ? spkLocal.length + ' altoparlante/cuffie rilevati' : '';
        return fetch('/api/config');
      }).then(r=>r&&r.json()).then(cfg=>{
        if(cfg&&cfg.microphone&&cfg.microphone.value) document.getElementById('mic').value = cfg.microphone.value;
        if(cfg&&cfg.speaker&&cfg.speaker.value) document.getElementById('speaker').value = cfg.speaker.value;
      }).catch(e=>{ setLoading(false); document.getElementById('statusText').textContent = 'Dispositivi locali non disponibili - usa Rete WiFi'; });
    }
    function toggleShowAll(){ showAll = !showAll; refreshDevices(); }
    function refreshDevices(){ loadDevices(); }
    function saveConfig(){
      const micVal = document.getElementById('mic').value;
      const spkVal = document.getElementById('speaker').value;
      if(!micVal || !spkVal){ alert('Seleziona microfono e altoparlante'); return; }
      const mic = mics.find(m=>m.value===micVal) || {type: micVal.startsWith('local_')?'local':'network', device_id: micVal.startsWith('local_')?parseInt(micVal.split('_')[1]):micVal.replace('net_',''), value: micVal, name: ''};
      const spk = spks.find(s=>s.value===spkVal) || {type: spkVal.startsWith('local_')?'local':'network', device_id: spkVal.startsWith('local_')?parseInt(spkVal.split('_')[1]):spkVal.replace('net_',''), value: spkVal, name: ''};
      fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({microphone:mic,speaker:spk})})
        .then(r=>{ if(!r.ok) throw new Error('Salvataggio fallito'); return r.json(); })
        .then(()=>{
          if(spkVal.startsWith('net_') || spkVal==='web_wait') location.href = '/client';
          else location.href = '/local';
        })
        .catch(err=>alert('Errore: '+err.message));
    }
  </script>
</body>
</html>
"""

# Client page - microfono e altoparlanti del browser (come Teams), + opzione AI Accelerator
CLIENT_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Parla (questo PC)</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: #0f0f12; color: #e4e4e7; max-width: 460px; margin: 0 auto; }
    h1 { font-size: 1.25rem; }
    .step { background: #18181b; border-radius: 12px; padding: 16px; margin: 12px 0; border: 1px solid #27272a; }
    .step label { display: block; font-size: 12px; color: #a1a1aa; margin-bottom: 6px; }
    select { width: 100%; padding: 12px; border-radius: 8px; border: 2px solid #3f3f46; background: #27272a; color: #fff; font-size: 14px; }
    .btn { width: 120px; height: 120px; border-radius: 50%; border: 4px solid #3b82f6; background: #1e1e22; color: #fff; font-size: 16px; cursor: pointer; margin: 20px auto; display: block; }
    .btn:active, .btn.recording { background: #dc2626; border-color: #dc2626; }
    .btn-allow { padding: 12px 24px; background: #22c55e; color: #0f0f12; border: none; border-radius: 8px; font-weight: 600; cursor: pointer; margin-bottom: 12px; }
    .result { background: #18181b; padding: 16px; border-radius: 12px; margin-top: 16px; font-size: 14px; }
    .result div { margin: 8px 0; }
    .ok { color: #22c55e; }
    .warn { color: #f59e0b; }
    .hint { color: #71717a; font-size: 12px; margin-top: 8px; }
    #deviceStatus { font-size: 12px; color: #a1a1aa; margin-top: 8px; }
  </style>
</head>
<body>
  <h1>G1 Parla</h1>
  <p class="hint" style="margin-bottom:16px;">Mic e cuffie: tuo PC. Elaborazione: AI Accelerator.</p>
  <div id="secureContextWarn" class="step" style="display:none;border-color:#dc2626;background:#450a0a;padding:24px;">
    <strong style="color:#fca5a5;font-size:16px;">Per usare microfono e cuffie: apri via localhost</strong>
    <p style="margin:16px 0;color:#e4e4e7;">Stai usando l'indirizzo IP. Il microfono richiede localhost. Fai cosi:</p>
    <p style="margin:8px 0;font-size:13px;"><b>1.</b> Sul PC Windows apri PowerShell e lancia:</p>
    <code style="display:block;margin:8px 0 16px;padding:14px;background:#1e1e22;border-radius:8px;font-size:13px;">ssh -L 8081:localhost:8081 lab@192.168.10.191 -N</code>
    <p style="margin:8px 0;font-size:13px;"><b>2.</b> Tieni aperta quella finestra, poi clicca:</p>
    <a href="http://localhost:8081/client" style="display:inline-block;margin:12px 0;padding:16px 28px;background:#22c55e;color:#0f0f12;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px;">Apri http://localhost:8081/client</a>
    <p style="margin:12px 0 0;font-size:12px;color:#a1a1aa;">Oppure da Windows: cd G1-TalkModule-OpenAiAPI poi .\avvia.ps1</p>
  </div>
  <p class="hint" id="hintAccess">Consenti l&apos;accesso al microfono per vedere la lista dispositivi.</p>

  <div id="allowWrap" class="step" style="display:none;">
    <button type="button" class="btn-allow" id="btnAllow">Consenti microfono e aggiorna dispositivi</button>
    <p id="deviceStatus">Clicca il pulsante per consentire l&apos;accesso e caricare microfoni e altoparlanti.</p>
  </div>
  <details id="devicesWrap" class="step" style="margin-bottom:16px;">
    <summary style="cursor:pointer;color:#a1a1aa;">Dispositivi (mic/cuffie)</summary>
    <label style="margin-top:12px;">Microfono</label>
    <select id="mic"><option value="">Caricamento...</option></select>
    <label style="margin-top:8px;">Altoparlante</label>
    <select id="speaker"><option value="">Caricamento...</option></select>
  </details>
  <button class="btn" id="btn">Tieni premuto</button>
  <div id="recStatus" style="margin:12px 0;min-height:50px;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
      <div style="width:120px;height:8px;background:#27272a;border-radius:4px;overflow:hidden;">
        <div id="levelBar" style="width:0%;height:100%;background:#22c55e;transition:width 0.05s;"></div>
      </div>
      <span id="levelLabel" style="font-size:12px;color:#71717a;">Livello: --</span>
    </div>
    <p class="hint" id="recDebug" style="font-size:11px;color:#71717a;min-height:18px;margin:0;"></p>
  </div>
  <div class="result" id="result"></div>

  <script>
    const wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    const MAX_REC_SEC = 20;
    const MIN_REC_MS = 600;
    let ws = null, mediaRecorder = null, chunks = [], recTimeout = null, lastPlayOn = 'browser', lastSinkId = null;
    let recStartTime = 0, recDurationInterval = null, levelInterval = null, analyserNode = null, audioCtx = null;
    let isRecording = false, pendingStop = false, currentStream = null;

    if (!navigator.mediaDevices) {
      document.getElementById('secureContextWarn').style.display = 'block';
      document.getElementById('hintAccess').style.display = 'none';
      document.getElementById('allowWrap').style.display = 'none';
      document.getElementById('devicesWrap').style.display = 'none';
      document.querySelectorAll('.step').forEach(el => { if(el.querySelector('select')) el.style.display = 'none'; });
      document.getElementById('btn').disabled = true;
      document.getElementById('recStatus').style.display = 'none';
      document.getElementById('result').innerHTML = '';
    }


    function connect(){
      ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        document.getElementById('result').innerHTML = '<div class="ok">Connesso al server. Tieni premuto e parla.</div>';
        document.getElementById('recDebug').textContent = 'WebSocket OK';
      };
      ws.onclose = () => { setTimeout(connect, 3000); document.getElementById('result').innerHTML = '<div class="warn">Riconnessione...</div>'; document.getElementById('recDebug').textContent = 'WebSocket disconnesso'; };
      ws.onmessage = (e) => {
        let d;
        try { d = JSON.parse(e.data); } catch(_) { document.getElementById('result').innerHTML = '<div class="warn">Errore risposta server</div>'; return; }
        if(d.type==='response'){
          const r = d.data;
          btn.disabled = false;
          document.getElementById('recDebug').textContent = r.text ? '' : (r.message || '');
          document.getElementById('recDebug').style.color = r.message ? '#f59e0b' : '#71717a';
          const msg = r.message ? '<div class="warn">'+r.message+'</div>' : '';
          document.getElementById('result').innerHTML = msg + '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+'</div>';
          if(lastPlayOn === 'browser' && r.audio_base64){
            const audio = new Audio('data:audio/mpeg;base64,'+r.audio_base64);
            if(lastSinkId && audio.setSinkId){ try { audio.setSinkId(lastSinkId); } catch(_){} }
            audio.play();
          }
        } else if(d.type==='error'){
          btn.disabled = false;
          document.getElementById('result').innerHTML = '<div class="warn">Errore: '+ (d.data || '')+'</div>';
        } else if(d.type==='play' && d.data){
          const a = new Audio('data:audio/mpeg;base64,'+d.data);
          if(lastSinkId && a.setSinkId){ try { a.setSinkId(lastSinkId); } catch(_){} }
          a.play();
        }
      };
    }
    connect();

    async function requestAndLoadDevices(){
      if (!navigator.mediaDevices) return;
      const statusEl = document.getElementById('deviceStatus');
      const allowWrap = document.getElementById('allowWrap');
      try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        stream.getTracks().forEach(t => t.stop());
      } catch(e) {
        allowWrap.style.display = 'block';
        statusEl.textContent = 'Accesso al microfono negato o non disponibile. Clicca il pulsante e consenti quando il browser lo chiede.';
        loadDevices();
        return;
      }
      allowWrap.style.display = 'none';
      await loadDevices();
    }

    async function loadDevices(){
      if (!navigator.mediaDevices) return;
      const micSel = document.getElementById('mic');
      const spkSel = document.getElementById('speaker');
      const statusEl = document.getElementById('deviceStatus');
      try {
        const devs = await navigator.mediaDevices.enumerateDevices();
        const mics = devs.filter(d => d.kind === 'audioinput');
        const spks = devs.filter(d => d.kind === 'audiooutput');

        micSel.innerHTML = mics.length === 0
          ? '<option value="">Nessun microfono rilevato</option>'
          : mics.map((m,i) => '<option value="'+m.deviceId+'">'+(m.label || ('Microfono '+(i+1)))+'</option>').join('');

        spkSel.innerHTML = '';
        spks.forEach((s,i) => spkSel.appendChild(new Option(s.label || ('Output '+(i+1)), 'browser_'+s.deviceId)));
        if(spks.length === 0) spkSel.appendChild(new Option('Predefinito', 'browser_default'));

        statusEl.textContent = '';
      } catch(e) {
        micSel.innerHTML = '<option value="">Errore: '+e.message+'</option>';
        spkSel.innerHTML = '<option value="browser_default">Riproduci qui</option>';
        statusEl.textContent = 'Errore lettura dispositivi.';
      }
    }

    document.getElementById('btnAllow').onclick = () => { requestAndLoadDevices(); };
    if (navigator.mediaDevices) requestAndLoadDevices();

    const btn = document.getElementById('btn');
    function onRecStart(e){ e.preventDefault(); if(!isRecording) startRec(); }
    function onRecStop(e){ e.preventDefault(); if(isRecording) stopRec(); }
    btn.onmousedown = btn.ontouchstart = onRecStart;
    btn.onmouseup = btn.ontouchend = btn.ontouchcancel = onRecStop;
    document.addEventListener('mouseup', onRecStop);
    document.addEventListener('touchend', onRecStop, {passive:false});

    async function startRec(){
      if(isRecording) return;
      isRecording = true;
      pendingStop = false;
      const micId = document.getElementById('mic').value;
      const spkVal = document.getElementById('speaker').value;
      lastPlayOn = 'browser';
      lastSinkId = (spkVal && spkVal.startsWith('browser_') && spkVal !== 'browser_default') ? spkVal.replace('browser_','') : null;
      const deviceId = null;
      try {
        const constraints = { audio: micId ? { deviceId: micId.length > 5 ? { exact: micId } : true } : true };
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        if(pendingStop){ stream.getTracks().forEach(t=>t.stop()); isRecording=false; return; }
        currentStream = stream;
        mediaRecorder = new MediaRecorder(stream);
        chunks = [];
        mediaRecorder.ondataavailable = e => { if(e.data && e.data.size) chunks.push(e.data); };
        mediaRecorder.onstop = () => {
          if(currentStream){ currentStream.getTracks().forEach(t=>t.stop()); currentStream=null; }
          clearAllIntervals();
          document.getElementById('levelBar').style.width = '0%';
          document.getElementById('levelLabel').textContent = 'Livello: --';
          btn.classList.remove('recording');
          isRecording = false;
          const dur = Date.now() - recStartTime;
          if(dur < MIN_REC_MS){
            document.getElementById('recDebug').textContent = 'Troppo breve ('+Math.round(dur/100)+' decimi sec). Tieni premuto 1-2 secondi.';
            document.getElementById('recDebug').style.color = '#f59e0b';
            return;
          }
          sendAudio(lastPlayOn, deviceId);
        };
        mediaRecorder.onerror = (e) => {
          clearAllIntervals();
          isRecording = false;
          btn.classList.remove('recording');
          document.getElementById('recDebug').textContent = 'Errore registrazione: '+e.error;
          document.getElementById('recDebug').style.color = '#dc2626';
        };
        mediaRecorder.start(250);
        recStartTime = Date.now();
        recDurationInterval = setInterval(() => {
          const s = ((Date.now()-recStartTime)/1000).toFixed(1);
          document.getElementById('recDebug').textContent = 'Registrazione: '+s+' sec';
          document.getElementById('recDebug').style.color = '#22c55e';
        }, 200);
        try {
          audioCtx = new (window.AudioContext||window.webkitAudioContext)();
          const src = audioCtx.createMediaStreamSource(stream);
          analyserNode = audioCtx.createAnalyser();
          analyserNode.fftSize = 256;
          analyserNode.smoothingTimeConstant = 0.5;
          src.connect(analyserNode);
          const data = new Uint8Array(analyserNode.frequencyBinCount);
          levelInterval = setInterval(() => {
            if(!analyserNode) return;
            analyserNode.getByteFrequencyData(data);
            let sum = 0;
            for(let i=0;i<data.length;i++) sum += data[i];
            const avg = sum / data.length;
            const pct = Math.min(100, Math.round(avg * 2));
            document.getElementById('levelBar').style.width = pct+'%';
            document.getElementById('levelLabel').textContent = pct > 5 ? 'Ti sento! ('+pct+'%)' : 'Livello: '+pct+'%';
          }, 80);
        } catch(_){}
        btn.classList.add('recording');
        document.getElementById('recDebug').textContent = 'Registrazione: 0.0 sec';
        recTimeout = setTimeout(() => { stopRec(); }, MAX_REC_SEC * 1000);
      } catch(err) {
        isRecording = false;
        pendingStop = false;
        document.getElementById('result').innerHTML = '<div class="warn">Microfono non disponibile. Consenti accesso al microfono e scegli un dispositivo dalla lista.</div>';
      }
    }
    function clearAllIntervals(){
      if(recTimeout){ clearTimeout(recTimeout); recTimeout = null; }
      if(recDurationInterval){ clearInterval(recDurationInterval); recDurationInterval = null; }
      if(levelInterval){ clearInterval(levelInterval); levelInterval = null; }
    }
    function stopRec(){
      if(!isRecording) return;
      pendingStop = true;
      clearAllIntervals();
      btn.classList.remove('recording');
      if(mediaRecorder && (mediaRecorder.state === 'recording' || mediaRecorder.state === 'paused')){
        try { mediaRecorder.stop(); } catch(_){}
      } else if(currentStream){
        currentStream.getTracks().forEach(t=>t.stop());
        currentStream = null;
        isRecording = false;
        document.getElementById('recDebug').textContent = 'Registrazione interrotta.';
      } else {
        isRecording = false;
      }
    }
    function sendAudio(playOn, outDeviceId){
      if(!chunks.length || !ws || ws.readyState !== WebSocket.OPEN){
        document.getElementById('recDebug').textContent = 'Errore: '+(!chunks.length ? 'nessun audio' : 'WebSocket chiuso');
        document.getElementById('recDebug').style.color = '#dc2626';
        return;
      }
      const blob = new Blob(chunks, {type: 'audio/webm'});
      const sizeKb = (blob.size/1024).toFixed(1);
      document.getElementById('recDebug').textContent = 'Invio '+sizeKb+' KB...';
      document.getElementById('recDebug').style.color = '#3b82f6';
      document.getElementById('result').innerHTML = '<div style="color:#3b82f6;">Elaborazione...</div>';
      btn.disabled = true;
      const fr = new FileReader();
      fr.onload = () => {
        const b64 = btoa(String.fromCharCode.apply(null, new Uint8Array(fr.result)));
        const msg = { type: 'audio', data: b64, play_on: playOn };
        if(playOn === 'server' && outDeviceId != null) msg.device_id = outDeviceId;
        ws.send(JSON.stringify(msg));
        chunks = [];
      };
      fr.readAsArrayBuffer(blob);
    }
  </script>
</body>
</html>
"""

# Local page - push-to-talk: tieni premuto per registrare, rilascia per rispondere
LOCAL_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Parla</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: #0f0f12; color: #e4e4e7; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
    h1 { font-size: 1.25rem; }
    .btn { width: 140px; height: 140px; border-radius: 50%; border: 4px solid #3b82f6; background: #1e1e22; color: #fff; font-size: 18px; font-weight: 600; cursor: pointer; transition: box-shadow 0.15s, transform 0.15s; }
    .btn:active, .btn.rec { background: #dc2626; border-color: #dc2626; }
    .btn.hearing { box-shadow: 0 0 24px #22c55e, 0 0 48px rgba(34,197,94,0.4); transform: scale(1.05); }
    .result { background: #18181b; padding: 16px; border-radius: 12px; margin-top: 16px; max-width: 400px; font-size: 14px; }
    .warn { color: #f59e0b; }
    .ok { color: #22c55e; }
    .level-wrap { width: 200px; margin: 12px 0; }
    .level-bar { height: 8px; background: #27272a; border-radius: 4px; overflow: hidden; }
    .level-fill { height: 100%; width: 0%; background: linear-gradient(90deg, #22c55e, #3b82f6); border-radius: 4px; transition: width 0.08s; }
    .level-label { font-size: 11px; color: #71717a; margin-top: 4px; }
  </style>
</head>
<body>
  <h1>Parla (push-to-talk)</h1>
  <p style="color:#71717a;">Tieni premuto il pulsante mentre parli, rilascia per inviare.</p>
  <p style="color:#71717a;font-size:12px;">Serve mic e speaker LOCALI nel setup (non Rete WiFi).</p>
  <button class="btn" id="btn">Tieni premuto</button>
  <div class="level-wrap">
    <div class="level-bar"><div class="level-fill" id="levelFill"></div></div>
    <div class="level-label" id="levelLabel">Tocca il pulsante per attivare il livello</div>
  </div>
  <div class="result" id="result"></div>
  <p style="margin-top:24px;"><a href="/" style="color:#3b82f6;">Modifica setup</a> | <a href="/local-fetch" style="color:#71717a;">Se resta in Riconnessione: versione 6 sec</a></p>
  <script>
    const wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/parla';
    let ws = null, noReconnect = false, reconnectCount = 0;
    function connect(){
      if (noReconnect) return;
      ws = new WebSocket(wsUrl);
      ws.onopen = () => { reconnectCount = 0; document.getElementById('result').innerHTML = '<span class="ok">Pronto</span>'; };
      ws.onclose = () => {
        if (noReconnect) return;
        document.getElementById('result').innerHTML = '<span class="warn">Riconnessione...</span>';
        if (reconnectCount < 5) { reconnectCount++; setTimeout(connect, 3000); }
        else { document.getElementById('result').innerHTML = '<span class="warn">Connessione fallita. <a href="/" style="color:#3b82f6">Configura mic e speaker locali</a> dal setup.</span>'; }
      };
      ws.onerror = () => {};
      ws.onmessage = (e) => {
        const d = JSON.parse(e.data);
        if (d.type === 'response') {
          const r = d.data;
          document.getElementById('result').innerHTML = (r.message ? '<div class="warn">'+r.message+'</div>' : '') + '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+'</div>';
          document.getElementById('btn').classList.remove('rec','hearing');
        } else if (d.type === 'error') {
          noReconnect = true;
          document.getElementById('result').innerHTML = '<span class="warn">'+d.data+'</span> <a href="/" style="color:#3b82f6">Modifica setup</a>';
          document.getElementById('btn').classList.remove('rec','hearing');
        }
      };
    }
    connect();
    const btn = document.getElementById('btn');
    const levelFill = document.getElementById('levelFill');
    const levelLabel = document.getElementById('levelLabel');
    let levelStarted = false;
    function startLevelMeter(){
      if(levelStarted) return;
      levelStarted = true;
      navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false}}).then(stream=>{
        const ctx = new (window.AudioContext||window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.6;
        src.connect(analyser);
        const data = new Uint8Array(analyser.fftSize);
        function update(){
          analyser.getByteTimeDomainData(data);
          let sum = 0;
          for(let i=0;i<data.length;i++){
            const v = (data[i]-128)/128;
            sum += v*v;
          }
          const rms = Math.sqrt(sum/data.length);
          const pct = Math.min(100, Math.round(rms*400));
          levelFill.style.width = pct+'%';
          levelLabel.textContent = pct>8 ? 'Ti sento!' : 'Livello microfono';
          if(btn.classList.contains('rec') && pct>15) btn.classList.add('hearing');
          else btn.classList.remove('hearing');
          requestAnimationFrame(update);
        }
        ctx.resume().then(()=>update()).catch(()=>update());
      }).catch(()=>{ levelLabel.textContent = 'Microfono non disponibile'; });
    }
    btn.onmousedown = btn.ontouchstart = (e) => {
      e.preventDefault();
      startLevelMeter();
      if(ws&&ws.readyState===1){ btn.classList.add('rec'); ws.send(JSON.stringify({type:'start'})); }
    };
    btn.onmouseup = btn.onmouseleave = btn.ontouchend = btn.ontouchcancel = (e) => { e.preventDefault(); if(ws&&ws.readyState===1){ ws.send(JSON.stringify({type:'stop'})); btn.classList.remove('hearing'); } };
  </script>
</body>
</html>
"""

# Alternativa fetch (6 sec) - se WebSocket non connette
LOCAL_FETCH_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Parla (6 sec)</title>
  <style>
    body { font-family: system-ui; padding: 24px; background: #0f0f12; color: #e4e4e7; text-align: center; }
    .btn { padding: 16px 48px; border-radius: 12px; border: none; background: #3b82f6; color: white; font-size: 18px; cursor: pointer; }
    .btn:disabled { background: #3f3f46; }
    .result { background: #18181b; padding: 16px; border-radius: 12px; margin-top: 16px; max-width: 400px; margin-left: auto; margin-right: auto; }
  </style>
</head>
<body>
  <h1>Parla (6 sec)</h1>
  <p>Clicca per registrare 6 secondi.</p>
  <button class="btn" id="btn" onclick="record()">Parla</button>
  <div class="result" id="result"></div>
  <p><a href="/local" style="color:#3b82f6">Push-to-talk</a> | <a href="/" style="color:#3b82f6">Setup</a></p>
  <script>
    async function record(){
      var btn=document.getElementById('btn'), res=document.getElementById('result');
      btn.disabled=true; res.innerHTML='Registrazione...';
      try {
        var r=await fetch('/api/record-and-process',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({duration:6})});
        var d=await r.json();
        res.innerHTML='<div><b>Hai detto:</b> '+(d.text||'')+'</div><div><b>Risposta:</b> '+(d.response||'')+'</div>';
      } catch(e){ res.innerHTML='Errore: '+e.message; }
      btn.disabled=false;
    }
  </script>
</body>
</html>
"""

# Listen page - ascolto continuo: Hey Markone attiva, 10 sec silenzio = stop
LISTEN_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>G1 Talk - Ascolto</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: #0f0f12; color: #e4e4e7; display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
    h1 { font-size: 1.25rem; }
    .status { background: #18181b; padding: 16px; border-radius: 12px; margin: 16px 0; max-width: 360px; font-size: 14px; }
    .ok { color: #22c55e; }
    .warn { color: #f59e0b; }
    .pulse { animation: pulse 1.5s ease-in-out infinite; }
    @keyframes pulse { 0%,100%{ opacity:1 } 50%{ opacity:0.5 } }
  </style>
</head>
<body>
  <h1>Ascolto continuo</h1>
  <p style="color:#71717a;">Di "Hey Markone" + domanda. Dopo 6 sec di silenzio risponde. Non toccare nulla.</p>
  <div class="status" id="status">Connessione...</div>
  <div class="status" id="result" style="display:none"></div>
  <p style="margin-top:24px;"><a href="/" style="color:#3b82f6;">Setup</a> | <a href="/local" style="color:#3b82f6;">Parla (push)</a></p>
  <script>
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/listen');
    ws.onopen = () => document.getElementById('status').innerHTML = '<span class="ok pulse">In ascolto...</span> Di "Hey Markone" seguito dalla domanda.';
    ws.onclose = () => document.getElementById('status').innerHTML = '<span class="warn">Disconnesso. Ricarica la pagina.</span>';
    ws.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.type === 'status') document.getElementById('status').innerHTML = '<span class="ok pulse">'+d.data+'</span>';
      else if (d.type === 'response') {
        const r = d.data;
        const el = document.getElementById('result');
        el.style.display = 'block';
        el.innerHTML = '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+'</div>';
        if (r.message) el.innerHTML = '<div class="warn">'+r.message+'</div>' + el.innerHTML;
      } else if (d.type === 'error') document.getElementById('status').innerHTML = '<span class="warn">Errore: '+d.data+'</span>';
    };
  </script>
</body>
</html>
"""


def run(host: str = "0.0.0.0", port: int = 8081, skip_audio_check: bool = False):
    if not HAS_FASTAPI:
        print("Installa: pip install fastapi uvicorn")
        return
    # Verifica PortAudio all'avvio
    if not skip_audio_check:
        try:
            from talk_module.audio import list_audio_devices
            devs = list_audio_devices()
            print(f"PortAudio OK - {len([d for d in devs if d.get('input_channels')])} mic, {len([d for d in devs if d.get('output_channels')])} speaker")
        except OSError as e:
            if "PortAudio" in str(e):
                print("ATTENZIONE: PortAudio non trovato. Solo modalità rete disponibile.")
                print("Su Jetson Orin NX esegui: sudo bash scripts/install_audio_jetson.sh")
                print("Per ora: usa --no-audio-check per avviare comunque (dispositivi di rete).")
    import uvicorn
    print(f"G1 Talk Module - http://{host}:{port}")
    print("  Setup: /")
    print("  Ascolto Hey Markone: /listen")
    print("  Parla (push): /local")
    print("  Client rete: /client")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--no-audio-check", action="store_true", help="Avvia anche senza PortAudio (solo dispositivi di rete)")
    args = p.parse_args()
    run(args.host, args.port, skip_audio_check=args.no_audio_check)
