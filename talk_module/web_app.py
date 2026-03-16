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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_executor = ThreadPoolExecutor(max_workers=2)

# FastAPI + WebSocket
try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body
    from fastapi.responses import HTMLResponse, RedirectResponse, Response
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
KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "config" / "knowledge.json"
CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Knowledge base: pattern -> risposta. Controllo prima dell'LLM per risposte veloci.
_knowledge_cache: dict[str, str] | None = None


def load_knowledge() -> dict[str, str]:
    """Carica knowledge da config/knowledge.json. Pattern (minuscolo) -> risposta."""
    global _knowledge_cache
    if _knowledge_cache is not None:
        return _knowledge_cache
    if not KNOWLEDGE_PATH.exists():
        _knowledge_cache = {}
        return _knowledge_cache
    try:
        data = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8"))
        _knowledge_cache = {str(k).strip().lower(): str(v).strip() for k, v in (data or {}).items() if k and v}
        return _knowledge_cache
    except Exception:
        _knowledge_cache = {}
        return _knowledge_cache


def reload_knowledge() -> None:
    """Svuota cache per ricaricare da file (dopo modifica a config/knowledge.json)."""
    global _knowledge_cache
    _knowledge_cache = None


def check_knowledge(user_input: str) -> str | None:
    """Se user_input contiene un pattern della knowledge, ritorna la risposta. Altrimenti None."""
    if not user_input or not user_input.strip():
        return None
    txt = user_input.strip().lower()
    # Ordina per lunghezza decrescente: match più specifici prima (es. "che ore sono" prima di "ore")
    for pattern, response in sorted(load_knowledge().items(), key=lambda x: -len(x[0])):
        if pattern and pattern in txt:
            return response
    return None


def _apply_stt_fuzzy_correction(text: str) -> str:
    """Corregge trascrizione STT con fuzzy matching su vocabolario (knowledge + stt_config)."""
    from talk_module.stt.fuzzy_correct import apply_fuzzy_correction
    return apply_fuzzy_correction(text or "", load_knowledge())


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


def _save_sample_audio(audio_bytes: bytes) -> Path | None:
    """Salva ultimo campione vocale per riuso (es. 'pronto pronto pronto'). Sovrascrive."""
    try:
        settings.ensure_dirs()
        out = settings.audio_dir / "last_sample.webm"
        out.write_bytes(audio_bytes)
        return out
    except Exception as e:
        print(f"[Debug] Salvataggio campione fallito: {e}")
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
            from talk_module.llm import LLMClient
            from talk_module.tts import TTSClient
            prov = settings.stt_provider
            if (prov == "deepgram" or settings.deepgram_api_key) and settings.deepgram_api_key:
                from talk_module.stt.deepgram_client import DeepgramClient
                _stt = DeepgramClient()
            elif (prov == "groq" or settings.groq_api_key) and settings.groq_api_key:
                from talk_module.stt.groq_client import GroqWhisperClient
                _stt = GroqWhisperClient()
            else:
                from talk_module.stt import WhisperClient
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

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

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

    @app.get("/api/stt-info")
    def api_stt_info():
        """Info sul provider STT attivo e alternative."""
        prov = settings.stt_provider
        return {
            "provider": prov,
            "alternatives": [
                {"id": "whisper", "name": "OpenAI Whisper", "env": "STT_PROVIDER=whisper"},
                {"id": "deepgram", "name": "Deepgram Nova", "env": "STT_PROVIDER=deepgram, DEEPGRAM_API_KEY=..."},
                {"id": "groq", "name": "Groq Whisper", "env": "STT_PROVIDER=groq, GROQ_API_KEY=..."},
            ],
        }

    @app.get("/api/test-pipeline")
    def api_test_pipeline():
        """Test pipeline con audio generato: TTS('prova prova prova') -> STT -> LLM -> TTS. Verifica che tutto funzioni."""
        errs = settings.validate()
        if errs:
            return {"ok": False, "error": "; ".join(errs)}
        t0 = time.perf_counter()
        try:
            stt, llm, tts, _, _ = get_services()
            test_phrase = "prova prova prova"
            audio_bytes = tts.synthesize(test_phrase, format="mp3")
            if not audio_bytes or len(audio_bytes) < 100:
                return {"ok": False, "error": "TTS non ha generato audio"}
            text = _apply_stt_fuzzy_correction(stt.transcribe(audio_bytes, format_hint="mp3", language="it") or "")
            if not text or not text.strip():
                return {"ok": False, "error": "STT non ha trascritto", "audio_size": len(audio_bytes)}
            resp = check_knowledge(text.strip()) or llm.chat(text.strip())
            if not resp:
                return {"ok": False, "error": "LLM non ha risposto", "transcribed": text}
            audio_out = tts.synthesize(resp, format="mp3")
            stt_used = "groq" if "GroqWhisperClient" in type(stt).__name__ else ("deepgram" if "Deepgram" in type(stt).__name__ else "whisper")
            return {
                "ok": True,
                "test_phrase": test_phrase,
                "transcribed": text.strip(),
                "llm_response": resp,
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "stt_provider": stt_used,
                "llm_model": settings.llm_model,
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "duration_ms": int((time.perf_counter() - t0) * 1000)}

    @app.get("/api/test-with-sample")
    def api_test_with_sample():
        """Test pipeline con ultimo campione vocale salvato (es. pronto pronto pronto)."""
        errs = settings.validate()
        if errs:
            return {"ok": False, "error": "; ".join(errs)}
        sample_path = settings.audio_dir / "last_sample.webm"
        if not sample_path.exists():
            return {"ok": False, "error": "Nessun campione salvato. Registra prima con 'Tieni premuto'."}
        try:
            audio_bytes = sample_path.read_bytes()
            if len(audio_bytes) < 500:
                return {"ok": False, "error": "Campione troppo corto"}
            result = _process_audio(audio_bytes, skip_wake_word=True, format_hint="webm")
            return {"ok": True, "duration_ms": result.get("duration_ms"), **result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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

    @app.get("/api/knowledge")
    def api_get_knowledge():
        """Lista pattern -> risposta dalla knowledge base."""
        return {"path": str(KNOWLEDGE_PATH), "entries": load_knowledge()}

    @app.post("/api/knowledge/reload")
    def api_reload_knowledge():
        """Ricarica knowledge da file dopo modifica."""
        reload_knowledge()
        return {"ok": True, "entries": len(load_knowledge())}

    @app.get("/api/robot-actions")
    def api_get_robot_actions():
        """Lista azioni robot (config/robot_actions.json)."""
        try:
            from talk_module.robot_actions import _load_robot_actions, ROBOT_ACTIONS_PATH
            return {"path": str(ROBOT_ACTIONS_PATH), "entries": _load_robot_actions()}
        except Exception as e:
            return {"path": "", "entries": {}, "error": str(e)}

    @app.get("/api/config")
    def api_get_config():
        return load_device_config()

    @app.post("/api/config")
    def api_save_config(data: dict = Body(...)):
        save_device_config(data)
        return {"ok": True}

    WAKE_WORDS = ("hey g1", "hey g 1", "ehi g1", "ehi g 1", "hey markone", "hey mark one", "ehi markone", "ehi mark one")
    WAKE_WORD_REQUIRED = os.getenv("WAKE_WORD_REQUIRED", "true").lower() in ("1", "true", "yes")

    def _extract_prompt(text: str, skip_wake_word: bool = False, audio_size: int = 0):
        """Ritorna (prompt, message). Se wake word trovata: (resto, ""). Altrimenti: (None, msg)."""
        if not text or not text.strip():
            debug = f" ({audio_size} byte inviati)" if audio_size else ""
            return None, f"Nessun testo riconosciuto{debug}. Parla piu a lungo (1-2 sec), vicino al microfono. Prova STT_PROVIDER=deepgram o groq in .env se persiste."
        t = text.strip()
        if any(h in t.lower() for h in ("sottotitoli", "amara.org", "amara ", "qtss", "subtitle", "created by", "a cura di")):
            return None, "Audio non chiaro. Riprova a parlare piu vicino al microfono."
        if skip_wake_word or not WAKE_WORD_REQUIRED:
            return t, ""
        # Match "hey/ehi" + "markone/mark one" all'inizio (flessibile su punteggiatura)
        m = re.match(r"^(?:hey|ehi)\s*[,.\s]*\s*(?:g\s*1|g1|mark\s*one|markone)\s*[,.\s]*\s*(.*)$", t, re.IGNORECASE)
        if m:
            rest = m.group(1).strip()
            if not rest:
                return None, "Di 'Hey G1' seguito dalla domanda. Es: Hey G1, che ore sono?"
            return rest, ""
        return None, "Di 'Hey G1' seguito dalla tua domanda per attivarmi."

    def _process_audio(audio_bytes: bytes, skip_wake_word: bool = False, format_hint: str = "webm") -> dict:
        """Pipeline: audio -> STT -> LLM -> TTS. skip_wake_word=True per pulsante Parla."""
        t0 = time.perf_counter()
        try:
            _save_sample_audio(audio_bytes)  # Salva ultimo campione per riuso (Test con campione)
            stt, llm, tts, _, _ = get_services()
            text = stt.transcribe(audio_bytes, format_hint=format_hint, language="it")
            text = _apply_stt_fuzzy_correction(text or "")
            # Debug: salva audio quando trascrizione fallisce (per analisi)
            if not text or not text.strip():
                saved = _save_debug_audio(audio_bytes)
                if saved:
                    print(f"[Debug] Audio non trascritto salvato: {saved}")
            prompt, msg = _extract_prompt(text or "", skip_wake_word=skip_wake_word, audio_size=len(audio_bytes))
            if msg:
                return {"text": text or "", "response": "", "audio_base64": "", "message": msg, "duration_ms": int((time.perf_counter() - t0) * 1000)}
            # Routing azioni robot (dare la mano, saluta, teaching)
            robot_match = None
            try:
                from talk_module.robot_actions import check_robot_action, execute_robot_action
                robot_match = check_robot_action(prompt)
            except Exception:
                pass
            if robot_match:
                resp_text, action_id = robot_match
                execute_robot_action(action_id)  # esegue se possibile, ignora errore nella risposta
                resp = resp_text
            else:
                resp = check_knowledge(prompt)
                if not resp:
                    from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup
                    if is_quick_lookup_question(prompt):
                        resp = quick_lookup(prompt)
                if not resp:
                    resp = llm.chat(prompt)
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            return {
                "text": text,
                "response": resp or "",
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}", "duration_ms": int((time.perf_counter() - t0) * 1000)}

    def _process_text(prompt: str) -> dict:
        """Pipeline: testo -> LLM -> TTS. Per domande scritte."""
        t0 = time.perf_counter()
        try:
            if not prompt or not prompt.strip():
                return {"text": "", "response": "", "audio_base64": "", "message": "Scrivi qualcosa.", "duration_ms": 0}
            stt, llm, tts, _, _ = get_services()
            robot_match = None
            try:
                from talk_module.robot_actions import check_robot_action, execute_robot_action
                robot_match = check_robot_action(prompt.strip())
            except Exception:
                pass
            if robot_match:
                resp_text, action_id = robot_match
                execute_robot_action(action_id)  # esegue se possibile, ignora errore nella risposta
                resp = resp_text
            else:
                resp = check_knowledge(prompt)
                if not resp:
                    from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup
                    if is_quick_lookup_question(prompt.strip()):
                        resp = quick_lookup(prompt.strip())
                if not resp:
                    resp = llm.chat(prompt.strip())
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            return {
                "text": prompt.strip(),
                "response": resp or "",
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}", "duration_ms": int((time.perf_counter() - t0) * 1000)}

    @app.post("/api/text-chat")
    def api_text_chat(text: str = Body(..., embed=True)):
        """Pipeline: testo -> LLM -> TTS. Domande scritte senza microfono."""
        errs = settings.validate()
        if errs:
            raise HTTPException(400, "; ".join(errs))
        return _process_text(text)

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
                            if len(audio_bytes) < 2000:
                                await ws.send_text(json.dumps({"type": "response", "data": {"text": "", "response": "", "audio_base64": "", "message": "Audio troppo corto. Tieni premuto 1-2 secondi mentre parli."}}))
                                continue
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
        """Ascolto continuo: Hey G1 attiva, 10 sec silenzio = stop. Solo mic/speaker locali."""
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
            await ws.send_text(json.dumps({"type": "status", "data": "In ascolto. Di 'Hey G1'..."}))
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
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; }
    body { font-family: 'Outfit', system-ui, sans-serif; margin: 0; padding: 24px; background: linear-gradient(160deg, #0c0e14 0%, #141922 50%, #0d0f14 100%); color: #e8eaed; min-height: 100vh; max-width: 560px; margin: 0 auto; }
    h1 { font-size: 1.5rem; margin-bottom: 8px; }
    .step { background: rgba(255,255,255,0.03); border-radius: 14px; padding: 20px; margin-bottom: 16px; border: 1px solid rgba(255,255,255,0.06); }
    .step h2 { font-size: 0.95rem; margin: 0 0 12px; color: #9ca3af; font-weight: 600; }
    .step .device-label { font-size: 12px; color: #71717a; margin-bottom: 6px; }
    select { width: 100%; padding: 14px 16px; border-radius: 8px; border: 2px solid #3f3f46; background: #27272a; color: #fff; font-size: 15px; min-height: 48px; cursor: pointer; }
    select:focus { outline: none; border-color: #14b8a6; }
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
    <p style="margin:8px 0;"><a href="/local" style="display:inline-block;padding:12px 20px;background:#14b8a6;color:#0c0e14;border-radius:8px;text-decoration:none;font-weight:600;">Parla</a> <span class="hint">— Tieni premuto, parla, rilascia. Mic e cuffie sull’AI Accelerator.</span></p>
    <p style="margin:8px 0;"><a href="/listen" style="display:inline-block;padding:12px 20px;background:#14b8a6;color:#0c0e14;border-radius:8px;text-decoration:none;font-weight:600;">Ascolto</a> <span class="hint">— Di’ «Hey G1» + domanda. Mic e cuffie sull’AI Accelerator.</span></p>
    <p style="margin:8px 0;"><a href="/client" style="display:inline-block;padding:12px 20px;background:rgba(255,255,255,0.1);color:#e8eaed;border-radius:8px;text-decoration:none;font-weight:600;">Client rete</a> <span class="hint">— Apri su telefono/tablet: userai mic e cuffie di quel dispositivo.</span></p>
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
  <title>G1 Talk - Parla</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: 'Outfit', system-ui, sans-serif;
      margin: 0;
      padding: 24px;
      background: linear-gradient(160deg, #0c0e14 0%, #141922 50%, #0d0f14 100%);
      color: #e8eaed;
      min-height: 100vh;
      max-width: 420px;
      margin: 0 auto;
    }
    h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.02em; margin-bottom: 4px; }
    .step {
      background: rgba(255,255,255,0.03);
      border-radius: 16px;
      padding: 18px;
      margin: 12px 0;
      border: 1px solid rgba(255,255,255,0.06);
    }
    .step label { display: block; font-size: 12px; color: #9ca3af; margin-bottom: 6px; font-weight: 500; }
    select {
      width: 100%;
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(0,0,0,0.3);
      color: #fff;
      font-size: 14px;
      font-family: inherit;
    }
    select:focus { outline: none; border-color: #14b8a6; }
    .btn {
      width: 140px;
      height: 140px;
      border-radius: 50%;
      border: 3px solid #14b8a6;
      background: linear-gradient(145deg, rgba(20,184,166,0.15) 0%, rgba(20,184,166,0.05) 100%);
      color: #fff;
      font-size: 17px;
      font-weight: 600;
      cursor: pointer;
      margin: 24px auto;
      display: block;
      transition: all 0.2s ease;
      font-family: inherit;
      box-shadow: 0 0 0 0 rgba(20,184,166,0.3);
    }
    .btn:hover { transform: scale(1.02); box-shadow: 0 0 24px rgba(20,184,166,0.25); }
    .btn:active, .btn.recording {
      background: linear-gradient(145deg, #dc2626 0%, #b91c1c 100%);
      border-color: #ef4444;
      transform: scale(0.98);
      box-shadow: 0 0 32px rgba(220,38,38,0.4);
    }
    .btn-allow {
      padding: 14px 24px;
      background: linear-gradient(135deg, #14b8a6 0%, #0d9488 100%);
      color: #0c0e14;
      border: none;
      border-radius: 10px;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;
      font-size: 14px;
      transition: transform 0.15s;
    }
    .btn-allow:hover { transform: translateY(-1px); }
    .result {
      background: rgba(255,255,255,0.03);
      padding: 18px;
      border-radius: 14px;
      margin-top: 16px;
      font-size: 14px;
      line-height: 1.5;
      border: 1px solid rgba(255,255,255,0.06);
    }
    .result div { margin: 8px 0; }
    .ok { color: #34d399; }
    .warn { color: #fbbf24; }
    .hint { color: #9ca3af; font-size: 13px; margin-top: 8px; }
    #deviceStatus { font-size: 12px; color: #9ca3af; margin-top: 8px; }
    summary { font-weight: 500; }
    input[type="text"] {
      padding: 12px 14px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.1);
      background: rgba(0,0,0,0.3);
      color: #fff;
      font-size: 14px;
      font-family: inherit;
    }
    input[type="text"]:focus { outline: none; border-color: #14b8a6; }
    input::placeholder { color: #6b7280; }
  </style>
</head>
<body>
  <h1>G1 Talk</h1>
  <p class="hint" style="margin-bottom:20px;">Tieni premuto e parla. Mic e cuffie: questo dispositivo.</p>
  <div id="secureContextWarn" class="step" style="display:none;border-color:rgba(239,68,68,0.5);background:rgba(185,28,28,0.2);padding:24px;">
    <strong style="color:#fca5a5;font-size:15px;">Microfono richiede localhost</strong>
    <p style="margin:14px 0;color:#e8eaed;font-size:14px;">Stai usando l'IP. Per mic/cuffie del browser:</p>
    <p style="margin:8px 0;font-size:13px;"><b>1.</b> PowerShell: <code style="padding:6px 10px;background:rgba(0,0,0,0.4);border-radius:6px;font-size:12px;">ssh -L 8081:localhost:8081 lab@192.168.10.191 -N</code></p>
    <p style="margin:8px 0;font-size:13px;"><b>2.</b> Poi apri:</p>
    <a href="http://localhost:8081/client" style="display:inline-block;margin:12px 0;padding:14px 24px;background:#14b8a6;color:#0c0e14;border-radius:10px;text-decoration:none;font-weight:600;font-size:14px;">localhost:8081/client</a>
    <p style="margin:12px 0 0;font-size:12px;color:#9ca3af;">Oppure: .\avvia.ps1</p>
  </div>
  <p class="hint" id="hintAccess">Per vedere microfoni e cuffie: clicca sotto e consenti quando il browser lo chiede.</p>

  <div id="allowWrap" class="step" style="display:block;">
    <button type="button" class="btn-allow" id="btnAllow">Consenti microfono e aggiorna dispositivi</button>
    <p id="deviceStatus">Clicca il pulsante per consentire l&apos;accesso e caricare microfoni e altoparlanti.</p>
  </div>
  <details id="knowledgeWrap" class="step" style="margin-bottom:12px;">
    <summary style="cursor:pointer;color:#a1a1aa;">Knowledge (risposte veloci)</summary>
    <p class="hint" style="margin-top:8px;">Modifica config/knowledge.json sul server. Pattern -&gt; risposta. Se la domanda contiene il pattern, risposta immediata (senza LLM).</p>
    <pre id="knowledgeList" style="font-size:11px;color:#71717a;max-height:120px;overflow:auto;margin-top:6px;">Caricamento...</pre>
  </details>
  <details id="devicesWrap" class="step" style="margin-bottom:16px;">
    <summary style="cursor:pointer;color:#a1a1aa;">Dispositivi (mic/cuffie)</summary>
    <label style="margin-top:12px;">Microfono</label>
    <select id="mic"><option value="">Caricamento...</option></select>
    <label style="margin-top:8px;">Altoparlante</label>
    <select id="speaker"><option value="">Caricamento...</option></select>
  </details>
  <button class="btn" id="btn">Tieni premuto e parla</button>
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
  <div class="step" style="margin-top:16px;">
    <label style="display:block;margin-bottom:6px;color:#a1a1aa;font-size:13px;">Scrivi una domanda (senza microfono)</label>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <input type="text" id="textInput" placeholder="Es: Che ore sono?" style="flex:1;min-width:180px;padding:10px 14px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:14px;" />
      <button type="button" id="btnText" style="padding:12px 22px;background:#14b8a6;color:#0c0e14;border:none;border-radius:10px;cursor:pointer;font-weight:600;">Invia</button>
    </div>
    <p class="hint" id="textStatus" style="margin-top:6px;min-height:18px;"></p>
  </div>
  <p class="hint" style="margin-top:12px;">
    <button type="button" id="btnTest" style="padding:8px 14px;background:rgba(255,255,255,0.06);color:#9ca3af;border:1px solid rgba(255,255,255,0.08);border-radius:8px;cursor:pointer;font-size:12px;">Test pipeline</button>
    <button type="button" id="btnSample" style="padding:8px 14px;background:rgba(255,255,255,0.06);color:#9ca3af;border:1px solid rgba(255,255,255,0.08);border-radius:8px;cursor:pointer;font-size:12px;margin-left:8px;">Test campione</button>
    <span id="testStatus"></span>
  </p>

  <script>
    const wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    const MAX_REC_SEC = 20;
    const MIN_REC_MS = 1000;
    let ws = null, mediaRecorder = null, chunks = [], recTimeout = null, lastPlayOn = 'browser', lastSinkId = null;
    let recStartTime = 0, recDurationInterval = null, levelInterval = null, analyserNode = null, audioCtx = null;
    let isRecording = false, pendingStop = false, currentStream = null;

    const isLocalhost = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    if (!isLocalhost) {
      document.getElementById('secureContextWarn').style.display = 'block';
      document.getElementById('hintAccess').style.display = 'none';
      document.getElementById('allowWrap').style.display = 'none';
      document.getElementById('devicesWrap').style.display = 'none';
    }
    if (!navigator.mediaDevices) {
      document.getElementById('secureContextWarn').style.display = 'block';
      document.getElementById('hintAccess').style.display = 'none';
      document.getElementById('allowWrap').style.display = 'none';
      document.getElementById('devicesWrap').style.display = 'none';
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
          const dur = r.duration_ms ? ' <span style="color:#71717a;font-size:12px;">('+r.duration_ms+' ms)</span>' : '';
          document.getElementById('result').innerHTML = msg + '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+dur+'</div>';
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
      statusEl.textContent = 'Richiesta permesso...';
      try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        stream.getTracks().forEach(t => t.stop());
        allowWrap.style.display = 'none';
        await loadDevices();
      } catch(e) {
        allowWrap.style.display = 'block';
        statusEl.textContent = "Accesso negato. Clicca il pulsante sopra e scegli Consenti. Se hai bloccato: apri impostazioni sito (lucchetto) e resetta permessi microfono.";
        loadDevices();
      }
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

        statusEl.textContent = mics.length === 0 && spks.length === 0 ? "Nessun dispositivo audio trovato. Collega microfono/cuffie e ricarica." : "";
      } catch(e) {
        micSel.innerHTML = '<option value="">Errore: '+e.message+'</option>';
        spkSel.innerHTML = '<option value="browser_default">Riproduci qui</option>';
        statusEl.textContent = 'Errore lettura dispositivi.';
      }
    }

    document.getElementById('btnAllow').onclick = () => { requestAndLoadDevices(); };
    if (navigator.mediaDevices && isLocalhost) {
      loadDevices();
      requestAndLoadDevices();
    }

    fetch('/api/knowledge').then(r=>r.json()).then(d=>{
      const el = document.getElementById('knowledgeList');
      if (el && d.entries) {
        const lines = Object.entries(d.entries).map(([k,v])=>'"'+k+'" -> "'+v.substring(0,50)+(v.length>50?'...':'')+'"');
        el.textContent = lines.length ? lines.join("\\n") : "(vuoto)";
      }
    }).catch(()=>{ const el=document.getElementById('knowledgeList'); if(el) el.textContent='(errore)'; });

    document.getElementById('btnTest').onclick = async () => {
      const btn = document.getElementById('btnTest');
      const status = document.getElementById('testStatus');
      btn.disabled = true;
      status.textContent = ' Test in corso (attendi 5-10 sec)...';
      status.style.color = '#a1a1aa';
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 60000);
      try {
        const r = await fetch('/api/test-pipeline', { signal: ctrl.signal });
        clearTimeout(t);
        const d = await r.json();
        if (d.ok) {
          const dur = d.duration_ms ? ' ('+d.duration_ms+' ms)' : '';
          status.textContent = ' OK: trascritto "'+d.transcribed+'", risposta: "'+(d.llm_response||'').substring(0,50)+'..."'+dur;
          status.style.color = '#22c55e';
          if (d.audio_base64) {
            const a = new Audio('data:audio/mpeg;base64,'+d.audio_base64);
            a.play();
          }
        } else {
          status.textContent = ' Errore: '+(d.error||'');
          status.style.color = '#dc2626';
        }
      } catch (e) {
        clearTimeout(t);
        if (e.name === 'AbortError') {
          status.textContent = ' Timeout (60s). Il server e lento o non raggiungibile.';
        } else {
          status.textContent = ' Errore: '+(e.message || String(e));
        }
        status.style.color = '#dc2626';
      }
      btn.disabled = false;
    };

    document.getElementById('btnText').onclick = async () => {
      const input = document.getElementById('textInput');
      const status = document.getElementById('textStatus');
      const txt = (input.value || '').trim();
      if (!txt) { status.textContent = 'Scrivi qualcosa.'; status.style.color = '#f59e0b'; return; }
      document.getElementById('btnText').disabled = true;
      status.textContent = ' Elaborazione...';
      status.style.color = '#a1a1aa';
      try {
        const r = await fetch('/api/text-chat', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: txt}) });
        const d = await r.json().catch(function(){ return {}; });
        if (!r.ok) {
          status.textContent = ' Errore: ' + (d.detail || d.error || r.status);
          status.style.color = '#dc2626';
        } else if (d.message && !d.response) {
          status.textContent = ' ' + d.message;
          status.style.color = '#f59e0b';
        } else {
          const dur = d.duration_ms ? d.duration_ms + ' ms' : '';
          status.textContent = dur ? ' Tempo: ' + dur : ' OK';
          status.style.color = '#22c55e';
          document.getElementById('result').innerHTML = '<div><b>Hai scritto:</b> '+(d.text||'')+'</div><div><b>Risposta:</b> '+(d.response||'')+' <span style="color:#71717a;font-size:12px;">('+(d.duration_ms||0)+' ms)</span></div>';
          if (d.audio_base64) {
            const a = new Audio('data:audio/mpeg;base64,'+d.audio_base64);
            if(lastSinkId && a.setSinkId){ try { a.setSinkId(lastSinkId); } catch(_){} }
            a.play();
          }
        }
      } catch (e) {
        status.textContent = ' Errore: ' + (e.message || String(e));
        status.style.color = '#dc2626';
      }
      document.getElementById('btnText').disabled = false;
    };
    document.getElementById('textInput').onkeydown = (e) => { if (e.key === 'Enter') document.getElementById('btnText').click(); };

    document.getElementById('btnSample').onclick = async () => {
      const btn = document.getElementById('btnSample');
      const status = document.getElementById('testStatus');
      btn.disabled = true;
      status.textContent = ' Test con ultimo campione...';
      status.style.color = '#a1a1aa';
      try {
        const r = await fetch('/api/test-with-sample');
        const d = await r.json();
        if (d.ok) {
          const dur = d.duration_ms ? ' ('+d.duration_ms+' ms)' : '';
          status.textContent = ' OK: "'+(d.text||'').substring(0,40)+'" -> "'+(d.response||'').substring(0,30)+'..."'+dur;
          status.style.color = '#22c55e';
          document.getElementById('result').innerHTML = '<div><b>Hai detto:</b> '+(d.text||'')+'</div><div><b>Risposta:</b> '+(d.response||'')+'</div>';
          if (d.audio_base64) {
            const a = new Audio('data:audio/mpeg;base64,'+d.audio_base64);
            if(lastSinkId && a.setSinkId){ try { a.setSinkId(lastSinkId); } catch(_){} }
            a.play();
          }
        } else {
          status.textContent = ' '+(d.error||'');
          status.style.color = '#dc2626';
        }
      } catch (e) {
        status.textContent = ' Errore: '+(e.message || String(e));
        status.style.color = '#dc2626';
      }
      btn.disabled = false;
    };

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
        const constraints = {
          audio: Object.assign(
            { echoCancellation: true, noiseSuppression: true },
            micId && micId.length > 5 ? { deviceId: { exact: micId } } : {}
          )
        };
        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        if(pendingStop){ stream.getTracks().forEach(t=>t.stop()); isRecording=false; return; }
        currentStream = stream;
        await new Promise(r => setTimeout(r, 150));
        if(pendingStop){ stream.getTracks().forEach(t=>t.stop()); isRecording=false; return; }
        const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
        mediaRecorder = new MediaRecorder(stream, { mimeType: mimeType, audioBitsPerSecond: 128000 });
        chunks = [];
        mediaRecorder.ondataavailable = e => { if(e.data && e.data.size > 0) chunks.push(e.data); };
        mediaRecorder.onstop = () => {
          clearAllIntervals();
          document.getElementById('levelBar').style.width = '0%';
          document.getElementById('levelLabel').textContent = 'Livello: --';
          btn.classList.remove('recording');
          isRecording = false;
          if(currentStream){ currentStream.getTracks().forEach(t=>t.stop()); currentStream=null; }
          const dur = Date.now() - recStartTime;
          if(dur < MIN_REC_MS){
            document.getElementById('recDebug').textContent = 'Troppo breve ('+Math.round(dur/100)+' decimi sec). Tieni premuto 1-2 secondi.';
            document.getElementById('recDebug').style.color = '#f59e0b';
            return;
          }
          setTimeout(() => sendAudio(lastPlayOn, deviceId), 80);
        };
        mediaRecorder.onerror = (e) => {
          clearAllIntervals();
          isRecording = false;
          btn.classList.remove('recording');
          document.getElementById('recDebug').textContent = 'Errore registrazione: '+e.error;
          document.getElementById('recDebug').style.color = '#dc2626';
        };
        mediaRecorder.start(500);
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
        try { mediaRecorder.requestData(); mediaRecorder.stop(); } catch(_){}
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
      const recMime = mediaRecorder && mediaRecorder.mimeType ? mediaRecorder.mimeType : 'audio/webm';
      const blob = new Blob(chunks, {type: recMime});
      if(blob.size < 2000){
        document.getElementById('recDebug').textContent = 'Audio troppo corto ('+(blob.size/1024).toFixed(1)+' KB). Tieni premuto 1-2 secondi.';
        document.getElementById('recDebug').style.color = '#f59e0b';
        btn.disabled = false;
        return;
      }
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

# Listen page - ascolto continuo: Hey G1 attiva, 10 sec silenzio = stop
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
  <p style="color:#71717a;">Di "Hey G1" + domanda. Dopo 6 sec di silenzio risponde. Non toccare nulla.</p>
  <div class="status" id="status">Connessione...</div>
  <div class="status" id="result" style="display:none"></div>
  <p style="margin-top:24px;"><a href="/" style="color:#3b82f6;">Setup</a> | <a href="/local" style="color:#3b82f6;">Parla (push)</a></p>
  <script>
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/listen');
    ws.onopen = () => document.getElementById('status').innerHTML = '<span class="ok pulse">In ascolto...</span> Di "Hey G1" seguito dalla domanda.';
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
    print("  Ascolto Hey G1: /listen")
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
