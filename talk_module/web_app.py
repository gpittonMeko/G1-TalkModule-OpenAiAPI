"""
Web App G1 Talk Module - AI Accelerator.
Wizard setup: seleziona microfono e altoparlante (locale o client web nella rete).
Tutto gira sulla macchina AI Accelerator.
"""

import asyncio
import base64
import json
import subprocess
import tempfile
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Optional

_executor = ThreadPoolExecutor(max_workers=2)

# FastAPI + WebSocket
try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body, Request, Query
    from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from talk_module.config import settings
from talk_module.audio_robot_effect import apply_robot_effect_base64
import os
from talk_module.network_discovery import (
    register_web_client,
    unregister_web_client,
    list_network_clients,
)

# Config file per scelte utente
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "audio_devices.json"
KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent / "config" / "knowledge.json"
SOUNDBOARD_PATH = Path(__file__).resolve().parent.parent / "config" / "soundboard.json"
SOUNDBOARD_LITE_PATH = Path(__file__).resolve().parent.parent / "config" / "soundboard_lite.json"
RUN_SHEET_PATH = Path(__file__).resolve().parent.parent / "config" / "run_sheet.json"
SOUNDBOARD_SLOT_COUNT = 20
SOUNDBOARD_TEXT_MAX_LEN = 280
# soundboard.json può essere ~20MB: una sola lettura parse in RAM finché il file non cambia (mtime).
_soundboard_cache: tuple[int, list[dict]] | None = None


def _invalidate_soundboard_cache() -> None:
    global _soundboard_cache
    _soundboard_cache = None


def _read_soundboard_lite_fast() -> list[dict] | None:
    """Elenco slot leggero da file piccolo; valido solo se source_mtime_ns coincide con soundboard.json."""
    if not SOUNDBOARD_LITE_PATH.exists() or not SOUNDBOARD_PATH.exists():
        return None
    try:
        main_ns = SOUNDBOARD_PATH.stat().st_mtime_ns
        data = json.loads(SOUNDBOARD_LITE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("source_mtime_ns") != main_ns:
            return None
        slots = data.get("slots")
        if not isinstance(slots, list) or len(slots) != SOUNDBOARD_SLOT_COUNT:
            return None
        # Sidecar vecchio senza robot_arm: rigenera da soundboard.json al prossimo GET lite
        if slots and isinstance(slots[0], dict) and "robot_arm" not in slots[0]:
            return None
        return slots
    except Exception:
        return None


def _write_soundboard_lite_sidecar(slots_lite: list[dict]) -> None:
    if not SOUNDBOARD_PATH.exists():
        return
    try:
        main_ns = SOUNDBOARD_PATH.stat().st_mtime_ns
        payload = {"source_mtime_ns": main_ns, "slots": slots_lite}
        SOUNDBOARD_LITE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:
        pass


CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Sentinel: solo wake word, senza domanda (risposta = settings.hey_g1_ack_text)
PROMPT_HEY_G1_ACK_ONLY = "__G1_HEY_ACK_ONLY__"

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


def _find_wake_and_rest(t: str) -> tuple[Optional[str], str]:
    """
    Ascolto continuo: rileva wake (Hey G1, G1, G one, gi one, Mark one, …).
    Ritorna (resto dopo wake, kind) con kind: miss | ack | ok.
    rest None se miss; stringa vuota se solo wake; altrimenti testo comando.
    """
    if not t or not t.strip():
        return None, "miss"
    s = t.strip()
    low = s.lower()
    if any(h in low for h in ("sottotitoli", "amara.org", "amara ", "qtss", "subtitle", "created by", "a cura di")):
        return None, "miss"
    # Varianti STT: g1, g one, gi one, jee one, mark one (ordine: prefisso vocale prima)
    wake_core = r"(?:g\s*1|\bg1\b|g\s*one|gi\s*one|mark\s*one|markone|jee\s*one)"
    m = re.search(rf"(?:hey|ehi|ei)\s*[,.\s]*\s*{wake_core}\s*[,.\s]*", s, re.IGNORECASE)
    if m:
        rest = s[m.end() :].strip().lstrip(",.; ")
        if not rest:
            return "", "ack"
        return rest, "ok"
    m2 = re.match(rf"^\s*{wake_core}\s*[,.\s]*\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m2:
        rest = (m2.group(1) or "").strip()
        if not rest:
            return "", "ack"
        return rest, "ok"
    m3 = re.search(rf"(?:^|[\s,;])({wake_core})(?=[\s,;]|$)", s, re.IGNORECASE)
    if m3:
        rest = s[m3.end() :].strip().lstrip(",.; ")
        if not rest:
            return "", "ack"
        return rest, "ok"
    return None, "miss"


# WebSocket clients: {client_id: ws}
_ws_clients: dict = {}


if HAS_FASTAPI:
    from fastapi.responses import JSONResponse
    from talk_module.audio.device_utils import resolve_configured_microphone_index

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
        global _stt, _llm, _tts, _player, _recorder, _device_config_mtime, _config_dirty
        if _config_dirty:
            _config_dirty = False
            _player = None
            _recorder = None
        if _stt is None:
            from talk_module.llm import LLMClient
            from talk_module.tts import TTSClient
            prov = settings.stt_provider
            # Solo se STT_PROVIDER è esplicitamente groq/deepgram E la chiave c'è (non basta avere GROQ_API_KEY nel .env con whisper)
            if prov == "deepgram" and settings.deepgram_api_key:
                from talk_module.stt.deepgram_client import DeepgramClient
                _stt = DeepgramClient()
            elif prov == "groq" and settings.groq_api_key:
                from talk_module.stt.groq_client import GroqWhisperClient
                _stt = GroqWhisperClient()
            else:
                if prov in ("groq", "deepgram"):
                    print(
                        f"[STT] STT_PROVIDER={prov} senza chiave valida: uso OpenAI Whisper (OPENAI_API_KEY).",
                        flush=True,
                    )
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
                    mic_id = resolve_configured_microphone_index(mic_cfg) if isinstance(mic_cfg, dict) else None
                    spk_cfg = cfg.get("speaker")
                    spk_id = spk_cfg.get("device_id") if isinstance(spk_cfg, dict) and spk_cfg.get("type") == "local" else None
                    try:
                        spk_id = int(spk_id) if spk_id is not None and str(spk_id).strip() != "" else None
                    except (TypeError, ValueError):
                        spk_id = None
                    _player = AudioPlayer(device_id=spk_id)
                    _recorder = AudioRecorder(device_id=mic_id)
                    if mic_id is not None:
                        print(f"[Audio] Recorder: PortAudio input index={mic_id} (config name={mic_cfg.get('name') if mic_cfg else None})", flush=True)
            except (OSError, ImportError, TypeError, AttributeError, NameError):
                pass
        else:
            # Dopo salvataggio config: _player/_recorder possono essere None — ricrea da file.
            if _recorder is None or _player is None:
                try:
                    from talk_module.audio import AudioRecorder, AudioPlayer, _AUDIO_AVAILABLE
                    if _AUDIO_AVAILABLE and AudioRecorder:
                        cfg = load_device_config()
                        mic_cfg = cfg.get("microphone")
                        mic_id = resolve_configured_microphone_index(mic_cfg) if isinstance(mic_cfg, dict) else None
                        spk_cfg = cfg.get("speaker")
                        spk_id = spk_cfg.get("device_id") if isinstance(spk_cfg, dict) and spk_cfg.get("type") == "local" else None
                        try:
                            spk_id = int(spk_id) if spk_id is not None and str(spk_id).strip() != "" else None
                        except (TypeError, ValueError):
                            spk_id = None
                        _player = AudioPlayer(device_id=spk_id)
                        _recorder = AudioRecorder(device_id=mic_id)
                        if mic_id is not None:
                            print(f"[Audio] Recorder: PortAudio input index={mic_id} (config name={mic_cfg.get('name') if mic_cfg else None})", flush=True)
                except (OSError, ImportError, TypeError, AttributeError, NameError):
                    pass
        return _stt, _llm, _tts, _player, _recorder

    @app.get("/")
    def index():
        return RedirectResponse(url="/client", status_code=302)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status_code=204)

    @app.get("/manifest.json")
    def manifest(request: Request):
        """PWA manifest per installazione su mobile (Add to Home Screen)."""
        base = str(request.base_url).rstrip("/")
        return JSONResponse({
            "name": "G1 Talk",
            "short_name": "G1 Talk",
            "description": "Assistente vocale per robot Unitree G1",
            "start_url": f"{base}/client",
            "display": "standalone",
            "background_color": "#0c0e14",
            "theme_color": "#14b8a6",
            "orientation": "portrait",
            "icons": [],
        })

    @app.get("/sw.js")
    def service_worker():
        """Service worker minimale per PWA (abilita Add to Home Screen)."""
        sw = """// G1 Talk - Service Worker minimal
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', () => self.clients.claim());
"""
        return Response(sw, media_type="application/javascript")

    @app.get("/client", response_class=HTMLResponse)
    def client_page():
        return CLIENT_TEMPLATE

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page():
        """Wizard setup: elenco dispositivi dalla Jetson via /api/devices (PortAudio + ALSA)."""
        return HTML_TEMPLATE

    @app.get("/launcher")
    def launcher_page(request: Request):
        """Pagina launcher: inserisci IP del server (robot o AI Accelerator) e connetti."""
        base = str(request.base_url).rstrip("/")
        host = request.url.hostname or "192.168.10.191"
        return HTMLResponse(LAUNCHER_TEMPLATE.format(host=host, base=base))

    @app.get("/robot-control", response_class=HTMLResponse)
    def robot_control_page():
        """Pagina telecomando robot: joystick + gesti braccia G1."""
        return ROBOT_CONTROL_TEMPLATE

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
        stt, _, _, _, _ = get_services()
        eff = type(stt).__name__
        effective = "whisper"
        if "Groq" in eff:
            effective = "groq"
        elif "Deepgram" in eff:
            effective = "deepgram"
        return {
            "provider": prov,
            "effective": effective,
            "alternatives": [
                {"id": "whisper", "name": "OpenAI Whisper", "env": "STT_PROVIDER=whisper (default)"},
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
            inputs = [
                {
                    "type": "local",
                    "device_id": d["index"],
                    "name": _label(d),
                    "value": f"local_{d['index']}",
                    "device_type": d.get("device_type") or "other",
                }
                for d in mics_raw
            ]
            outputs = [
                {
                    "type": "local",
                    "device_id": d["index"],
                    "name": _label(d),
                    "value": f"local_{d['index']}",
                    "device_type": d.get("device_type") or "other",
                }
                for d in spks_raw
            ]
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
        hardware_probe: dict = {}
        try:
            from talk_module.audio.device_utils import probe_system_audio_hardware

            hardware_probe = probe_system_audio_hardware()
        except Exception:
            pass
        return {
            "microphones": inputs,
            "speakers": outputs,
            "network_clients": net,
            "bluetooth_paired": bt,
            "hardware_probe": hardware_probe,
        }

    @app.get("/api/bluetooth-scan")
    def api_bluetooth_scan(seconds: int = 10):
        """Discovery Bluetooth (bluetoothctl scan): elenca device in prossimità + già accoppiati."""
        try:
            from talk_module.audio.device_utils import scan_bluetooth_devices

            devices, warning = scan_bluetooth_devices(seconds)
            return {
                "ok": True,
                "devices": devices,
                "count": len(devices),
                "warning": warning or None,
            }
        except Exception as e:
            return {"ok": False, "devices": [], "count": 0, "warning": str(e)}

    @app.post("/api/bluetooth-control")
    def api_bluetooth_control(data: dict = Body(...)):
        """trust | pair | connect | disconnect | pair_connect — eseguito sul server Linux (bluetoothctl)."""
        try:
            from talk_module.audio.device_utils import bluetooth_control_device

            action = str(data.get("action") or "").strip()
            mac = str(data.get("mac") or "").strip()
            ok, message = bluetooth_control_device(action, mac)
            return {"ok": ok, "message": message}
        except Exception as e:
            return {"ok": False, "message": str(e)}

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

    @app.get("/api/devices-detailed")
    def api_devices_detailed():
        """Tutti i device PortAudio (nomi esatti) + scansione ALSA/USB sulla Jetson."""
        try:
            from talk_module.audio.device_utils import list_audio_devices, probe_system_audio_hardware

            raw = list_audio_devices()
            hp = probe_system_audio_hardware()
            return {
                "ok": True,
                "portaudio_devices": raw,
                "portaudio_count": len(raw),
                "hardware_probe": hp,
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

    @app.post("/api/knowledge/save")
    def api_save_knowledge(data: dict = Body(...)):
        """Salva knowledge su config/knowledge.json."""
        entries = data.get("entries") or {}
        clean = {str(k).strip(): str(v).strip() for k, v in entries.items() if k and str(k).strip() and v is not None}
        try:
            KNOWLEDGE_PATH.write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
            reload_knowledge()
            return {"ok": True, "entries": len(clean)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _load_soundboard() -> list[dict]:
        """Carica soundboard da config/soundboard.json. N slot: {icon, text, audio_base64}."""
        global _soundboard_cache
        n = SOUNDBOARD_SLOT_COUNT
        if not SOUNDBOARD_PATH.exists():
            _soundboard_cache = None
            return [
                {
                    "icon": "🎤",
                    "text": f"Comando {i+1}",
                    "audio_base64": "",
                    "format": "webm",
                    "audio_base64_clean": "",
                    "format_clean": "mp3",
                    "robot_arm": "",
                    "robot_loco": "",
                }
                for i in range(n)
            ]
        try:
            mtime_ns = SOUNDBOARD_PATH.stat().st_mtime_ns
            if _soundboard_cache is not None and _soundboard_cache[0] == mtime_ns:
                return _soundboard_cache[1]
            data = json.loads(SOUNDBOARD_PATH.read_text(encoding="utf-8"))
            slots = data.get("slots") or []
            while len(slots) < n:
                slots.append(
                    {
                        "icon": "🎤",
                        "text": f"Comando {len(slots)+1}",
                        "audio_base64": "",
                        "format": "webm",
                        "audio_base64_clean": "",
                        "format_clean": "mp3",
                        "robot_arm": "",
                        "robot_loco": "",
                    }
                )
            for i in range(len(slots)):
                s = slots[i]
                s.setdefault("icon", "🎤")
                s.setdefault("text", f"Comando {i+1}")
                s.setdefault("audio_base64", "")
                s.setdefault("format", "webm")
                s.setdefault("audio_base64_clean", "")
                s.setdefault("format_clean", "mp3")
                s.setdefault("robot_arm", "")
                s.setdefault("robot_loco", "")
            out = slots[:n]
            _soundboard_cache = (mtime_ns, out)
            return out
        except Exception:
            _soundboard_cache = None
            return [
                {
                    "icon": "🎤",
                    "text": f"Comando {i+1}",
                    "audio_base64": "",
                    "format": "webm",
                    "audio_base64_clean": "",
                    "format_clean": "mp3",
                    "robot_arm": "",
                    "robot_loco": "",
                }
                for i in range(n)
            ]

    def _soundboard_slots_lite(slots: list[dict]) -> list[dict]:
        """Solo metadati per la griglia UI: evita ~20MB JSON su mobile (crash/OOM su /api/soundboard)."""
        lite: list[dict] = []
        for s in slots:
            ar = str(s.get("audio_base64") or "")
            ac = str(s.get("audio_base64_clean") or "")
            lite.append(
                {
                    "icon": s.get("icon"),
                    "text": s.get("text"),
                    "format": s.get("format") or "webm",
                    "format_clean": s.get("format_clean") or "mp3",
                    "has_robot": len(ar) > 50,
                    "has_clean": len(ac) > 50,
                    "robot_arm": str(s.get("robot_arm") or ""),
                    "robot_loco": str(s.get("robot_loco") or ""),
                }
            )
        return lite

    def _default_run_sheet() -> dict:
        return {
            "policy": "Autonomia robot: circa 2 ore operative, poi 15–20 minuti di downtime per ricarica.",
            "rows": [
                {"fase": "WELCOME", "attivita": "Accredito — saluto con audio", "ora_inizio": "", "durata_stimata": "", "note": ""},
                {"fase": "WELCOME", "attivita": "Coffee — flussi ospiti", "ora_inizio": "", "durata_stimata": "", "note": ""},
                {"fase": "SALA_PRINCIPALE", "attivita": "Interazioni / coreografie fondo palco", "ora_inizio": "", "durata_stimata": "", "note": ""},
                {"fase": "SALA_PRINCIPALE", "attivita": "Messaggio posto / inizio evento", "ora_inizio": "", "durata_stimata": "", "note": ""},
                {"fase": "STAMPA_GREEN", "attivita": "Sala stampa / green room (TBD)", "ora_inizio": "", "durata_stimata": "", "note": ""},
                {"fase": "DEFLUSSO", "attivita": "Uscita accredito / gift / messaggio registrato", "ora_inizio": "", "durata_stimata": "", "note": ""},
            ],
        }

    def _load_run_sheet() -> dict:
        if not RUN_SHEET_PATH.exists():
            return _default_run_sheet()
        try:
            data = json.loads(RUN_SHEET_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _default_run_sheet()
            data.setdefault("policy", _default_run_sheet()["policy"])
            data.setdefault("rows", _default_run_sheet()["rows"])
            return data
        except Exception:
            return _default_run_sheet()

    @app.get("/api/soundboard")
    def api_get_soundboard(lite: bool = Query(False, description="Solo icona/testo/flag audio; niente base64 (leggero per telefono)")):
        if lite:
            fast = _read_soundboard_lite_fast()
            if fast is not None:
                return {"slots": fast, "slot_count": SOUNDBOARD_SLOT_COUNT, "text_max_len": SOUNDBOARD_TEXT_MAX_LEN}
        slots = _load_soundboard()
        if lite:
            lite_slots = _soundboard_slots_lite(slots)
            _write_soundboard_lite_sidecar(lite_slots)
            slots = lite_slots
        return {"slots": slots, "slot_count": SOUNDBOARD_SLOT_COUNT, "text_max_len": SOUNDBOARD_TEXT_MAX_LEN}

    @app.get("/api/soundboard-slot/{slot_idx}")
    def api_get_soundboard_slot(slot_idx: int):
        """Un solo slot con audio completo: per riproduzione su Browser o modifica slot."""
        if slot_idx < 0 or slot_idx >= SOUNDBOARD_SLOT_COUNT:
            raise HTTPException(400, "Slot non valido")
        slots = _load_soundboard()
        if slot_idx >= len(slots):
            raise HTTPException(400, "Slot non valido")
        s = slots[slot_idx]
        return {
            "icon": s.get("icon"),
            "text": s.get("text"),
            "audio_base64": s.get("audio_base64") or "",
            "format": s.get("format") or "webm",
            "audio_base64_clean": s.get("audio_base64_clean") or "",
            "format_clean": s.get("format_clean") or "mp3",
            "robot_arm": str(s.get("robot_arm") or ""),
            "robot_loco": str(s.get("robot_loco") or ""),
        }

    @app.get("/api/run-sheet")
    def api_get_run_sheet():
        return _load_run_sheet()

    @app.post("/api/run-sheet")
    def api_save_run_sheet(data: dict = Body(...)):
        policy = str(data.get("policy", "") or "").strip() or _default_run_sheet()["policy"]
        rows = data.get("rows")
        if not isinstance(rows, list):
            rows = _default_run_sheet()["rows"]
        clean_rows = []
        for r in rows[:40]:
            if not isinstance(r, dict):
                continue
            clean_rows.append(
                {
                    "fase": str(r.get("fase", ""))[:80],
                    "attivita": str(r.get("attivita", ""))[:240],
                    "ora_inizio": str(r.get("ora_inizio", ""))[:40],
                    "durata_stimata": str(r.get("durata_stimata", ""))[:40],
                    "note": str(r.get("note", ""))[:400],
                }
            )
        out = {"policy": policy[:500], "rows": clean_rows or _default_run_sheet()["rows"]}
        try:
            RUN_SHEET_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _apply_robot_effect(audio_b64: str, fmt: str = "webm") -> tuple[str, str]:
        """Applica effetto vocale robotico (ring mod + bitcrush + bandpass). Ritorna (base64, format)."""
        preset = getattr(settings, "robot_effect_preset", "robot_full") or "robot_full"
        return apply_robot_effect_base64(audio_b64, fmt, preset=preset)

    @app.post("/api/audio-to-robot-voice")
    def api_audio_to_robot_voice(data: dict = Body(...)):
        """Audio -> STT (trascrivi) -> TTS (risintetizza). Ritorna voce sintetica robotica."""
        audio_b64 = str(data.get("audio_base64", ""))
        fmt = str(data.get("format", "webm")) or "webm"
        if not audio_b64 or len(audio_b64) < 100:
            return {"ok": False, "error": "Audio mancante o troppo corto"}
        try:
            raw = base64.b64decode(audio_b64)
            stt, _, tts, _, _ = get_services()
            text = stt.transcribe(raw, format_hint=fmt, language="it")
            text = _apply_stt_fuzzy_correction(text or "")
            if not text or not text.strip():
                return {"ok": False, "error": "Nessun testo riconosciuto nell'audio"}
            audio_out = tts.synthesize(text.strip(), format="mp3")
            if not audio_out:
                return {"ok": False, "error": "TTS non ha generato audio"}
            return {"ok": True, "audio_base64": base64.b64encode(audio_out).decode(), "format": "mp3", "text": text.strip()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/soundboard")
    def api_save_soundboard(data: dict = Body(...)):
        """Salva slot: audio naturale (TTS/registrazione); audio_base64 e audio_base64_clean allineati."""
        global _soundboard_cache
        slot = int(data.get("slot", 0))
        if slot < 0 or slot >= SOUNDBOARD_SLOT_COUNT:
            return {"ok": False, "error": f"slot 0-{SOUNDBOARD_SLOT_COUNT - 1}"}
        audio_b64 = str(data.get("audio_base64", ""))
        fmt = str(data.get("format", "webm"))
        clean_b64 = str(data.get("audio_base64_clean", "")) if "audio_base64_clean" in data else None
        clean_fmt = str(data.get("format_clean", "mp3") or "mp3")
        slots = _load_soundboard()
        if clean_b64 is None:
            prev = slots[slot] if slot < len(slots) else {}
            clean_b64 = str(prev.get("audio_base64_clean") or "")
            clean_fmt = str(prev.get("format_clean") or "mp3")
        elif not clean_b64 and audio_b64:
            clean_b64 = audio_b64
            clean_fmt = fmt
        txt = str(data.get("text", "")).strip()[:SOUNDBOARD_TEXT_MAX_LEN] or f"Comando {slot+1}"
        prev = slots[slot] if slot < len(slots) else {}
        ra = data.get("robot_arm")
        rl = data.get("robot_loco")
        slots[slot] = {
            "icon": str(data.get("icon", "🎤"))[:4],
            "text": txt,
            "audio_base64": audio_b64,
            "format": fmt,
            "audio_base64_clean": clean_b64,
            "format_clean": clean_fmt if clean_b64 else "mp3",
            "robot_arm": str(ra if ra is not None else (prev.get("robot_arm") or "")),
            "robot_loco": str(rl if rl is not None else (prev.get("robot_loco") or "")),
        }
        try:
            SOUNDBOARD_PATH.write_text(json.dumps({"slots": slots}, indent=2), encoding="utf-8")
            _soundboard_cache = (SOUNDBOARD_PATH.stat().st_mtime_ns, slots[: SOUNDBOARD_SLOT_COUNT])
            _write_soundboard_lite_sidecar(_soundboard_slots_lite(slots[: SOUNDBOARD_SLOT_COUNT]))
            return {"ok": True}
        except Exception as e:
            _invalidate_soundboard_cache()
            return {"ok": False, "error": str(e)}

    def _soundboard_bytes_to_wav_playable(raw: bytes, fmt: str) -> bytes:
        """Converte qualsiasi formato supportato da ffmpeg in WAV PCM per sounddevice (dispositivo Jetson)."""
        if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            return raw
        fmt_l = (fmt or "mp3").lower().split(";")[0].strip()
        if "webm" in fmt_l:
            suf = ".webm"
        elif "wav" in fmt_l:
            return raw
        elif "mp3" in fmt_l or fmt_l == "mpeg" or fmt_l == "audio/mpeg":
            suf = ".mp3"
        else:
            suf = ".webm"
        with tempfile.NamedTemporaryFile(suffix=suf, delete=False) as f:
            f.write(raw)
            inp = Path(f.name)
        out = inp.with_suffix(".wav")
        try:
            r = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(inp),
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(out),
                ],
                capture_output=True,
                timeout=120,
            )
            if r.returncode != 0 or not out.exists():
                err = (r.stderr or b"").decode("utf-8", errors="replace")[-400:]
                raise RuntimeError(err or "ffmpeg decode failed")
            return out.read_bytes()
        except FileNotFoundError:
            raise HTTPException(503, "Installa ffmpeg sul Jetson: sudo apt install ffmpeg")
        finally:
            inp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    @app.post("/api/soundboard-play-local")
    def api_soundboard_play_local(data: dict = Body(...)):
        """Riproduce uno slot audio sulla cassa del Jetson (sounddevice + device_id dal setup)."""
        slot_idx = int(data.get("slot", -1))
        slots = _load_soundboard()
        if slot_idx < 0 or slot_idx >= len(slots):
            raise HTTPException(400, "Slot non valido")
        s = slots[slot_idx] or {}
        b64: Optional[str] = None
        fmt = "mp3"
        if s.get("audio_base64_clean") and len(str(s.get("audio_base64_clean", ""))) > 50:
            b64 = str(s["audio_base64_clean"])
            fmt = str(s.get("format_clean") or "mp3")
        elif s.get("audio_base64") and len(str(s.get("audio_base64", ""))) > 50:
            b64 = str(s["audio_base64"])
            fmt = str(s.get("format") or "mp3")
        if not b64:
            raise HTTPException(400, "Slot senza audio")
        cfg = load_device_config()
        spk = cfg.get("speaker") or {}
        if spk.get("type") != "local":
            raise HTTPException(
                400,
                "Apri https://<IP-Jetson>:8081/ e scegli Altoparlante «locale» (cassa sul robot), poi Salva.",
            )
        dev_id = spk.get("device_id")
        if dev_id is None:
            raise HTTPException(400, "Device ID altoparlante mancante nel setup")
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "Base64 non valido")
        if len(raw) < 80:
            raise HTTPException(400, "Audio troppo corto")
        arm = str(s.get("robot_arm") or "").strip()
        loco = str(s.get("robot_loco") or "").strip()
        if not arm and not loco:
            arm = "face_wave"
        if arm or loco:
            import threading as _thr

            def _sb_robot(a, l):
                try:
                    from talk_module.robot_actions import (
                        execute_g1_loco_command,
                        execute_robot_action,
                        loco_command_requires_confirm,
                    )
                    if a:
                        ok, msg = execute_robot_action(a)
                        print(f"[soundboard-play-local] arm={a!r} ok={ok} msg={msg}", flush=True)
                    if l and not loco_command_requires_confirm(l):
                        execute_g1_loco_command(l)
                except Exception as e:
                    print(f"[soundboard-play-local] robot: {e}", flush=True)

            _thr.Thread(target=_sb_robot, args=(arm, loco), daemon=True).start()
        from talk_module.audio import AudioPlayer

        try:
            wav_bytes = _soundboard_bytes_to_wav_playable(raw, fmt)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"Decodifica audio: {e!s}")
        p = AudioPlayer(device_id=int(dev_id))
        if not p.play_bytes(wav_bytes, format_hint="wav"):
            raise HTTPException(500, "Riproduzione fallita. Verifica PortAudio e il dispositivo in setup.")
        return {"ok": True}

    @app.post("/api/soundboard-synth")
    def api_soundboard_synth(data: dict = Body(...)):
        """TTS dal testo (voce naturale), per popolare uno slot senza registrare."""
        text = str(data.get("text", "")).strip()[:SOUNDBOARD_TEXT_MAX_LEN]
        if not text:
            return {"ok": False, "error": "Testo vuoto"}
        errs = settings.validate()
        if errs:
            return {"ok": False, "error": "; ".join(errs)}
        try:
            _, _, tts, _, _ = get_services()
            raw = tts.synthesize(text, format="wav")
            if not raw:
                return {"ok": False, "error": "TTS non ha prodotto audio"}
            b64_clean = base64.b64encode(raw).decode()
            return {
                "ok": True,
                "audio_base64": b64_clean,
                "format": "wav",
                "audio_base64_clean": b64_clean,
                "format_clean": "wav",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/robot-actions")
    def api_get_robot_actions():
        """Lista azioni robot (config/robot_actions.json + arm actions G1)."""
        try:
            from talk_module.robot_actions import _load_robot_actions, ROBOT_ACTIONS_PATH, get_arm_actions_list
            return {
                "path": str(ROBOT_ACTIONS_PATH),
                "entries": _load_robot_actions(),
                "arm_actions": get_arm_actions_list(),
            }
        except Exception as e:
            return {"path": "", "entries": {}, "arm_actions": [], "error": str(e)}

    @app.post("/api/robot-action")
    def api_execute_robot_action(data: dict = Body(...)):
        """Esegue azione braccio G1 (action_id int o nome)."""
        action_id = data.get("action_id")
        robot_ip = data.get("robot_ip") or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
        if action_id is None:
            raise HTTPException(400, "action_id richiesto")
        try:
            from talk_module.robot_actions import execute_robot_action
            ok, msg = execute_robot_action(action_id, robot_ip=robot_ip)
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.post("/api/robot-move")
    def api_robot_move(data: dict = Body(...)):
        """Comando movimento G1: vx, vy, vyaw."""
        vx = float(data.get("vx", 0))
        vy = float(data.get("vy", 0))
        vyaw = float(data.get("vyaw", 0))
        robot_ip = data.get("robot_ip") or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
        try:
            from talk_module.robot_actions import send_move_command
            ok, msg = send_move_command(vx, vy, vyaw, robot_ip=robot_ip)
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.post("/api/robot-loco")
    def api_robot_loco(data: dict = Body(...)):
        """Comandi locomozione G1: command = ready | walk | stop_walk | low_stand."""
        cmd = str(data.get("command", "") or "").strip()
        robot_ip = data.get("robot_ip") or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
        if not cmd:
            raise HTTPException(400, "command richiesto (es. ready, walk)")
        try:
            from talk_module.robot_actions import execute_g1_loco_command
            ok, msg = execute_g1_loco_command(cmd, robot_ip=robot_ip)
            return {"ok": ok, "message": msg}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.get("/api/config")
    def api_get_config():
        return load_device_config()

    _config_dirty = False

    @app.post("/api/config")
    def api_save_config(data: dict = Body(...)):
        global _player, _recorder, _config_dirty
        save_device_config(data)
        _config_dirty = True
        return {"ok": True}

    WAKE_WORDS = ("hey g1", "hey g 1", "ehi g1", "ehi g 1", "hey markone", "hey mark one", "ehi markone", "ehi mark one")
    # Default false: push-to-talk risponde sempre senza «Hey G1». La wake word resta attiva solo per
    # ascolto continuo (client invia skip_wake: false) o se imposti WAKE_WORD_REQUIRED=true.
    WAKE_WORD_REQUIRED = os.getenv("WAKE_WORD_REQUIRED", "false").lower() in ("1", "true", "yes")

    def _extract_prompt(text: str, skip_wake_word: bool = False, audio_size: int = 0):
        """Solo push-to-talk / test (skip_wake_word=True): (prompt, messaggio errore)."""
        if not text or not text.strip():
            debug = f" ({audio_size} byte inviati)" if audio_size else ""
            return None, f"Nessun testo riconosciuto{debug}. Parla piu a lungo (1-2 sec), vicino al microfono. Prova STT_PROVIDER=deepgram o groq in .env se persiste."
        t = text.strip()
        if any(h in t.lower() for h in ("sottotitoli", "amara.org", "amara ", "qtss", "subtitle", "created by", "a cura di")):
            return None, "Audio non chiaro. Riprova a parlare piu vicino al microfono."
        return t, ""

    def _run_robot_match_actions(rm) -> str:
        """Esegue arm/loco da RobotMatch (dataclass) in thread separato; ritorna testo per TTS senza bloccare."""
        import threading as _thr

        def _fire():
            try:
                from talk_module.robot_actions import (
                    execute_g1_loco_command,
                    execute_robot_action,
                    loco_command_requires_confirm,
                )
                arm = (rm.arm_action or "").strip()
                if arm:
                    ok, msg = execute_robot_action(arm)
                    print(f"[robot-match] arm={arm!r} ok={ok} msg={msg}", flush=True)
                loco = (rm.loco_command or "").strip()
                if loco and not loco_command_requires_confirm(loco):
                    ok, msg = execute_g1_loco_command(loco)
                    print(f"[robot-match] loco={loco!r} ok={ok} msg={msg}", flush=True)
            except Exception as e:
                print(f"[robot-match] error: {e}", flush=True)

        _thr.Thread(target=_fire, daemon=True).start()
        return ((rm.response or "").strip() or "Ok")

    def _stt_and_wake_check(audio_bytes: bytes, format_hint: str = "webm") -> dict:
        """Phase 1 (fast): STT + wake word detection. Used by WS two-phase pipeline."""
        t0 = time.perf_counter()
        _ms = lambda: int((time.perf_counter() - t0) * 1000)
        try:
            _save_sample_audio(audio_bytes)
            stt, _, tts_svc, _, _ = get_services()
            raw_text = stt.transcribe(audio_bytes, format_hint=format_hint, language="it")
            if not raw_text or not raw_text.strip():
                saved = _save_debug_audio(audio_bytes)
                if saved:
                    print(f"[Debug] Audio non trascritto salvato: {saved}")
                return {"text": raw_text or "", "response": "", "audio_base64": "", "message": "", "wake_miss": True, "duration_ms": _ms()}
            rest, wkind = _find_wake_and_rest(raw_text)
            print(f"[Wake] raw={raw_text!r} kind={wkind} rest={rest!r}", flush=True)
            if wkind == "miss":
                return {"text": raw_text, "response": "", "audio_base64": "", "message": "", "wake_miss": True, "duration_ms": _ms()}
            if wkind == "ack":
                ack_text = "Per parlarmi, di' Hey G1 seguito dalla tua domanda."
                ack_audio = b""
                try:
                    ack_audio = tts_svc.synthesize(ack_text, format="mp3")
                except Exception:
                    pass
                return {
                    "text": raw_text, "response": ack_text,
                    "audio_base64": base64.b64encode(ack_audio).decode() if ack_audio else "",
                    "message": "", "wake_ack": True, "duration_ms": _ms(),
                }
            prompt = _apply_stt_fuzzy_correction(rest or "")
            return {"_wake_ok": True, "_prompt": prompt, "_raw_text": raw_text, "_t0": t0}
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}", "duration_ms": _ms()}

    def _process_after_wake(prompt: str, raw_text: str, t0: float) -> dict:
        """Phase 2: robot action / knowledge / LLM + TTS after wake detected."""
        try:
            _, llm, tts, _, _ = get_services()
            if prompt == PROMPT_HEY_G1_ACK_ONLY:
                resp = (settings.hey_g1_ack_text or "").strip() or "Sì?"
            else:
                robot_match = None
                try:
                    from talk_module.robot_actions import check_robot_action
                    robot_match = check_robot_action(prompt)
                except Exception:
                    pass
                if robot_match:
                    resp = _run_robot_match_actions(robot_match)
                else:
                    resp = check_knowledge(prompt)
                    if not resp:
                        from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup, NOT_FOUND
                        if is_quick_lookup_question(prompt):
                            resp = quick_lookup(prompt)
                            if resp == NOT_FOUND:
                                resp = None
                    if not resp:
                        resp = llm.chat(prompt)
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            return {
                "text": raw_text,
                "response": resp or "",
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:
            err = str(e)
            el = err.lower()
            if "401" in err or "expired_api_key" in el or "invalid_api_key" in el or "invalid api key" in el or "incorrect api key" in el:
                err = "Chiave API non valida o scaduta. Aggiorna .env e riavvia. " + err
            elif "invalid file format" in el or ("supported formats" in el and ("flac" in el or "webm" in el or "mp3" in el)):
                err = "STT: formato rifiutato. pip install imageio-ffmpeg. " + err
            return {"text": raw_text, "response": "", "audio_base64": "", "message": f"Errore: {err}", "duration_ms": int((time.perf_counter() - t0) * 1000)}

    def _process_audio(audio_bytes: bytes, skip_wake_word: bool = False, format_hint: str = "webm") -> dict:
        """Pipeline: audio -> STT -> LLM -> TTS. skip_wake_word=True per pulsante Parla."""
        t0 = time.perf_counter()
        try:
            _save_sample_audio(audio_bytes)  # Salva ultimo campione per riuso (Test con campione)
            stt, llm, tts, _, _ = get_services()
            raw_text = stt.transcribe(audio_bytes, format_hint=format_hint, language="it")
            if not raw_text or not raw_text.strip():
                saved = _save_debug_audio(audio_bytes)
                if saved:
                    print(f"[Debug] Audio non trascritto salvato: {saved}")
                if not skip_wake_word:
                    return {
                        "text": raw_text or "",
                        "response": "",
                        "audio_base64": "",
                        "message": "",
                        "wake_miss": True,
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    }
                prompt, msg = _extract_prompt(raw_text or "", skip_wake_word=True, audio_size=len(audio_bytes))
                return {"text": raw_text or "", "response": "", "audio_base64": "", "message": msg or "", "duration_ms": int((time.perf_counter() - t0) * 1000)}
            if not skip_wake_word:
                rest, wkind = _find_wake_and_rest(raw_text)
                print(f"[Wake] raw={raw_text!r} kind={wkind} rest={rest!r}", flush=True)
                if wkind == "miss":
                    return {
                        "text": raw_text,
                        "response": "",
                        "audio_base64": "",
                        "message": "",
                        "wake_miss": True,
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    }
                if wkind == "ack":
                    return {
                        "text": raw_text,
                        "response": "",
                        "audio_base64": "",
                        "message": "",
                        "wake_ack": True,
                        "duration_ms": int((time.perf_counter() - t0) * 1000),
                    }
                prompt = _apply_stt_fuzzy_correction(rest or "")
                text = raw_text
            else:
                text = _apply_stt_fuzzy_correction(raw_text or "")
                prompt, msg = _extract_prompt(text or "", skip_wake_word=True, audio_size=len(audio_bytes))
                if msg:
                    return {"text": text or "", "response": "", "audio_base64": "", "message": msg, "duration_ms": int((time.perf_counter() - t0) * 1000)}
            if prompt == PROMPT_HEY_G1_ACK_ONLY:
                resp = (settings.hey_g1_ack_text or "").strip() or "Sì, ti ascolto. Come posso aiutarti?"
            else:
                # Routing azioni robot (dare la mano, saluta, teaching)
                robot_match = None
                try:
                    from talk_module.robot_actions import check_robot_action

                    robot_match = check_robot_action(prompt)
                except Exception:
                    pass
                if robot_match:
                    resp = _run_robot_match_actions(robot_match)
                else:
                    resp = check_knowledge(prompt)
                    if not resp:
                        from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup, NOT_FOUND
                        if is_quick_lookup_question(prompt):
                            resp = quick_lookup(prompt)
                            if resp == NOT_FOUND:
                                resp = None  # fallback a LLM
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
            err = str(e)
            el = err.lower()
            # Non confondere con STT: OpenAI usa invalid_request_error anche per 401 / chiave scaduta
            if (
                "401" in err
                or "expired_api_key" in el
                or "invalid_api_key" in el
                or "invalid api key" in el
                or "incorrect api key" in el
            ):
                err = (
                    "Chiave API OpenAI non valida o scaduta (Whisper STT, LLM e TTS usano OPENAI_API_KEY). "
                    "Aggiorna OPENAI_API_KEY nel file .env sul server e riavvia il servizio. "
                    "Nuova chiave: https://platform.openai.com/api-keys — Dettaglio: "
                    + err
                )
            elif "invalid file format" in el or (
                "supported formats" in el and ("flac" in el or "webm" in el or "mp3" in el)
            ):
                err = (
                    "STT: formato audio rifiutato dall'API. "
                    "Esegui nella cartella del progetto: pip install imageio-ffmpeg "
                    "(conversione WebM→WAV per OpenAI Whisper). Dettaglio: "
                    + err
                )
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {err}", "duration_ms": int((time.perf_counter() - t0) * 1000)}

    def _process_text(prompt: str) -> dict:
        """Pipeline: testo -> LLM -> TTS. Per domande scritte."""
        t0 = time.perf_counter()
        try:
            if not prompt or not prompt.strip():
                return {"text": "", "response": "", "audio_base64": "", "message": "Scrivi qualcosa.", "duration_ms": 0}
            stt, llm, tts, _, _ = get_services()
            robot_match = None
            try:
                from talk_module.robot_actions import check_robot_action

                robot_match = check_robot_action(prompt.strip())
            except Exception:
                pass
            if robot_match:
                resp = _run_robot_match_actions(robot_match)
            else:
                resp = check_knowledge(prompt)
                if not resp:
                    from talk_module.quick_lookup import is_quick_lookup_question, quick_lookup, NOT_FOUND
                    if is_quick_lookup_question(prompt.strip()):
                        resp = quick_lookup(prompt.strip())
                        if resp == NOT_FOUND:
                            resp = None  # fallback a LLM
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
        return _process_audio(audio_bytes, skip_wake_word=True)

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
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({"type": "error", "data": "JSON non valido"}))
                    continue
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
                            # MIME reale dal browser (webm vs mp4/Safari); STT usa magic bytes + conversione WAV
                            audio_fmt = str(data.get("format") or "audio/webm")
                            # skip_wake esplicito: true = push-to-talk senza «Hey G1»; false = solo wake (Alexa-style)
                            sk = data.get("skip_wake")
                            if sk is None:
                                skip_w = not WAKE_WORD_REQUIRED
                            else:
                                skip_w = bool(sk)
                            loop = asyncio.get_running_loop()
                            if not skip_w:
                                # Two-phase: STT+wake (fast) → chime → LLM+TTS
                                p1 = await loop.run_in_executor(
                                    _executor,
                                    partial(_stt_and_wake_check, audio_bytes, audio_fmt),
                                )
                                if p1.get("_wake_ok"):
                                    await ws.send_text(json.dumps({"type": "wake_chime"}))
                                    result = await loop.run_in_executor(
                                        _executor,
                                        partial(_process_after_wake, p1["_prompt"], p1["_raw_text"], p1["_t0"]),
                                    )
                                else:
                                    result = p1
                            else:
                                result = await loop.run_in_executor(
                                    _executor,
                                    partial(_process_audio, audio_bytes, True, audio_fmt),
                                )
                            try:
                                tw = (result.get("text") or "")[:80].replace("\n", " ")
                                print(
                                    f"[ws/audio] skip_wake={skip_w} bytes={len(audio_bytes)} "
                                    f"wake_miss={bool(result.get('wake_miss'))} wake_ack={bool(result.get('wake_ack'))} "
                                    f"text={tw!r} resp_len={len(result.get('response') or '')} "
                                    f"msg={bool(result.get('message'))} ms={result.get('duration_ms')}"
                                )
                            except Exception:
                                pass
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
        """Ascolto continuo: Hey G1 attiva, mic locale Jetson. Speaker locale o browser (audio_base64)."""
        await ws.accept()
        cfg = load_device_config()
        if not cfg.get("microphone") or cfg.get("microphone", {}).get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura microfono locale dal setup"}))
            await ws.close()
            return

        spk = cfg.get("speaker") or {}
        play_local = spk.get("type") == "local"

        listen_queue = queue.Queue()
        listen_stop = threading.Event()

        def _record_loop():
            try:
                _, _, _, _, recorder = get_services()
                if not recorder:
                    listen_queue.put({"error": "PortAudio non disponibile"})
                    return
                print(f"[Listen] Record loop started, device={recorder.device_id}, rate={recorder.sample_rate}", flush=True)
                chunk_count = 0
                for audio_bytes in recorder.record_until_silence(
                    silence_seconds=5,
                    chunk_duration=0.5,
                    silence_threshold=0.0035,
                    max_duration=60.0,
                    stop_check=lambda: listen_stop.is_set(),
                ):
                    if listen_stop.is_set():
                        break
                    if len(audio_bytes) > 500:
                        chunk_count += 1
                        print(f"[Listen] Yielded audio chunk #{chunk_count}, size={len(audio_bytes)} bytes", flush=True)
                        listen_queue.put(audio_bytes)
                print(f"[Listen] Record loop ended (chunks yielded: {chunk_count})", flush=True)
            except Exception as e:
                print(f"[Listen] Record loop ERROR: {e}", flush=True)
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
                if result.get("audio_base64") and play_local and player:
                    import base64 as b64
                    player.play_bytes(b64.b64decode(result["audio_base64"]), format_hint="mp3")
        except WebSocketDisconnect:
            pass
        finally:
            listen_stop.set()

    @app.websocket("/ws/parla")
    async def websocket_parla(ws: WebSocket):
        """Push-to-talk: registrazione dal mic PortAudio (Jetson); TTS su cassa locale o sul browser (telefono/BT)."""
        await ws.accept()
        cfg = load_device_config()
        mic = cfg.get("microphone") or {}
        spk = cfg.get("speaker") or {}
        if not mic or mic.get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Configura microfono locale (USB sulla Jetson) dal setup /"}))
            await ws.close()
            return
        if spk.get("type") not in ("local", "network"):
            await ws.send_text(
                json.dumps(
                    {
                        "type": "error",
                        "data": "Altoparlante: scegli Cassa Jetson (locale) oppure Rete/Browser per TTS sul telefono.",
                    }
                )
            )
            await ws.close()
            return

        ptt_stop = threading.Event()
        ptt_queue = queue.Queue()
        recording = False

        try:
            while True:
                msg = await ws.receive_text()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    await ws.send_text(json.dumps({"type": "error", "data": "JSON non valido"}))
                    continue
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
                    if result.get("audio_base64") and spk.get("type") == "local":
                        _, _, _, player, _ = get_services()
                        if player:
                            import base64 as b64
                            player.play_bytes(b64.b64decode(result["audio_base64"]), format_hint="mp3")
                    # type network: il browser (/local) riproduce audio_base64 (telefono -> cassa BT)
                    recording = False
        except WebSocketDisconnect:
            pass
        finally:
            ptt_stop.set()

    @app.websocket("/ws/mic-level")
    async def websocket_mic_level(ws: WebSocket):
        """Stream real-time mic audio levels to the browser for visual monitoring."""
        await ws.accept()
        cfg = load_device_config()
        mic = cfg.get("microphone") or {}
        if not mic or mic.get("type") != "local":
            await ws.send_text(json.dumps({"type": "error", "data": "Nessun microfono locale configurato"}))
            await ws.close()
            return

        level_stop = threading.Event()
        level_queue = queue.Queue()

        def _level_loop():
            try:
                import sounddevice as sd
                import numpy as np
                from talk_module.audio.device_utils import resolve_configured_microphone_index
                mic_id = resolve_configured_microphone_index(mic)
                if mic_id is None:
                    level_queue.put({"error": "Microfono non trovato"})
                    return
                dev_info = sd.query_devices(mic_id)
                rate = int(dev_info.get("default_samplerate", 44100))
                block = int(rate * 0.08)
                dev_name = dev_info.get("name", "?")
                level_queue.put({"type": "info", "name": dev_name, "rate": rate, "device": mic_id})
                with sd.InputStream(device=mic_id, channels=1, samplerate=rate,
                                    blocksize=block, dtype="float32") as stream:
                    while not level_stop.is_set():
                        data, _ = stream.read(block)
                        audio = data.squeeze()
                        rms = float(np.sqrt(np.mean(audio ** 2)))
                        peak = float(np.max(np.abs(audio)))
                        db = max(-60.0, 20 * np.log10(rms + 1e-10))
                        level_queue.put({"type": "level", "rms": round(rms, 5),
                                         "peak": round(peak, 5), "db": round(db, 1)})
            except Exception as e:
                level_queue.put({"error": str(e)})

        t = threading.Thread(target=_level_loop, daemon=True)
        t.start()
        try:
            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(_executor, lambda: level_queue.get(timeout=0.3))
                except queue.Empty:
                    continue
                if isinstance(item, dict) and "error" in item:
                    await ws.send_text(json.dumps({"type": "error", "data": item["error"]}))
                    break
                await ws.send_text(json.dumps(item))
        except WebSocketDisconnect:
            pass
        finally:
            level_stop.set()

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


# Launcher - Connetti a robot o AI Accelerator (per mobile/APK)
LAUNCHER_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#14b8a6">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>G1 Talk - Connetti</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: linear-gradient(160deg, #0c0e14 0%, #141922 100%); color: #e8eaed; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; }
    h1 { font-size: 1.5rem; margin-bottom: 8px; }
    .card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px; max-width: 360px; width: 100%; }
    label { display: block; font-size: 13px; color: #9ca3af; margin-bottom: 6px; }
    input[type="text"] { width: 100%; padding: 14px 16px; border-radius: 10px; border: 2px solid #3f3f46; background: #27272a; color: #fff; font-size: 16px; margin-bottom: 16px; }
    input:focus { outline: none; border-color: #14b8a6; }
    .presets { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
    .preset { padding: 10px 16px; background: rgba(20,184,166,0.15); color: #14b8a6; border: 1px solid rgba(20,184,166,0.3); border-radius: 8px; cursor: pointer; font-size: 13px; }
    .preset:hover { background: rgba(20,184,166,0.25); }
    .btn { width: 100%; padding: 16px; background: #14b8a6; color: #0c0e14; border: none; border-radius: 10px; font-size: 16px; font-weight: 600; cursor: pointer; }
    .btn:hover { background: #0d9488; }
    .hint { font-size: 12px; color: #71717a; margin-top: 16px; line-height: 1.5; }
    .chk { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
    .chk input { width: auto; margin: 0; }
  </style>
</head>
<body>
  <h1>G1 Talk</h1>
  <p style="color:#9ca3af;font-size:14px;margin-bottom:24px;">Connetti al robot o all&apos;AI Accelerator</p>
  <div class="card">
    <label>IP del server</label>
    <input type="text" id="ip" placeholder="192.168.123.161" value="{host}" />
    <div class="presets">
      <button type="button" class="preset" onclick="setIp('192.168.123.161')">Robot G1</button>
      <button type="button" class="preset" onclick="setIp('192.168.10.191')">AI Accelerator</button>
    </div>
    <div class="chk">
      <input type="checkbox" id="https" checked />
      <label for="https" style="margin:0;">Usa HTTPS (serve per microfono)</label>
    </div>
    <button type="button" class="btn" onclick="connect()">Connetti</button>
  </div>
  <details class="hint" style="margin-top:12px;max-width:360px;text-align:left;">
    <summary style="cursor:pointer;color:#14b8a6;font-weight:600;">Come funziona</summary>
    <p style="margin:10px 0 0;">Il <strong>server</strong> (Python) deve essere già avviato sul G1: <code>bash scripts/restart_server.sh</code>. Questa pagina apre solo l&apos;interfaccia <strong>/client</strong> sul robot.</p>
    <p style="margin:8px 0 0;">Stesso WiFi del robot → IP es. <code>192.168.123.161</code> (verifica con <code>hostname -I</code> sul G1). <strong>HTTPS</strong> attivo = microfono ok. <strong>Cassa Bluetooth:</strong> accoppia il telefono in Impostazioni.</p>
  </details>
  <p class="hint" style="margin-top:12px;">
    Collega il telefono al WiFi del robot o alla stessa rete dell&apos;AI Accelerator, inserisci l&apos;IP e Connetti.
  </p>
  <script>
    (function(){ var s = localStorage.getItem('g1_last_ip'); if(s) document.getElementById('ip').value = s; })();
    function setIp(ip){ document.getElementById('ip').value = ip; }
    function connect(){
      var ip = document.getElementById('ip').value.trim().replace(/^https?:\\/\\//,'').split('/')[0].split(':')[0];
      if(!ip){ alert('Inserisci l\\'IP'); return; }
      var useHttps = document.getElementById('https').checked;
      var proto = useHttps ? 'https' : 'http';
      var url = proto + '://' + ip + ':8081/client';
      localStorage.setItem('g1_last_ip', ip);
      window.location.href = url;
    }
  </script>
</body>
</html>
"""

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
    .device-count { font-size: 12px; color: #a1a1aa; margin-top: 8px; line-height: 1.4; }
    .device-summary { border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; font-size: 13px; line-height: 1.45; border: 1px solid rgba(255,255,255,0.1); background: rgba(255,255,255,0.03); }
    .device-summary.has-local { border-color: rgba(34,197,94,0.45); background: rgba(34,197,94,0.08); }
    .device-summary.none { border-color: rgba(245,158,11,0.35); background: rgba(245,158,11,0.06); }
    .device-summary .sum-title { display: block; color: #86efac; margin-bottom: 8px; font-size: 13px; }
    .device-summary.none .sum-warn { color: #fcd34d; font-size: 13px; }
    .device-summary .sum-list { margin: 0; padding-left: 18px; color: #e4e4e7; }
    .device-summary .sum-list li { margin: 4px 0; }
    select optgroup { font-weight: 600; color: #9ca3af; font-size: 12px; }
  </style>
</head>
<body>
  <h1>G1 Talk Module</h1>
  <p class="hint" style="margin:-4px 0 16px;font-size:13px;"><strong>Guida rapida:</strong> sul robot installa lo ZIP e avvia il server (<code>install.sh</code> → <code>restart_server.sh</code>). Da telefono apri <strong>/client</strong> (link verde sotto). Questa pagina <code>/</code> serve a scegliere mic/cuffie quando usi il PC connesso all’accelerator.</p>
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
    <div id="micSummary" class="device-summary" style="display:none;"></div>
    <select id="mic" aria-label="Seleziona microfono"><option value="web_wait">Rete WiFi: apri /client su telefono o G1</option></select>
    <p class="hint">Voci raggruppate: <strong>sul server</strong> (USB/Jetson) e <strong>rete</strong> (telefono/tablet). Sotto vedi l’elenco rilevato.</p>
    <p class="device-count" id="micCount"></p>
  </div>
  <div class="step">
    <h2>2. Altoparlante / Cuffie</h2>
    <p class="device-label">Scegli altoparlante o cuffie</p>
    <div id="spkSummary" class="device-summary" style="display:none;"></div>
    <select id="speaker" aria-label="Seleziona altoparlante o cuffie"><option value="web_wait">Rete WiFi: apri /client su telefono o G1</option></select>
    <p class="hint">Stesso raggruppamento: uscite <strong>sul server</strong> o audio <strong>in rete</strong>.</p>
    <p class="device-count" id="spkCount"></p>
  </div>
  <div class="step" id="btStep" style="display:none">
    <h2>Bluetooth (sul server Linux / Jetson)</h2>
    <p id="btList" class="hint"></p>
    <p class="hint">Dopo scan o Aggiorna, scegli un MAC e collega. Alcuni device chiedono conferma o PIN sul telefono/cassa.</p>
    <button type="button" class="btn-secondary" id="btScanBtn" onclick="scanBluetooth()">Cerca dispositivi Bluetooth (~10s)</button>
    <p id="btScanHint" class="hint" style="margin-top:8px;">Serve <code>bluetoothctl</code> e permessi (gruppo <code>bluetooth</code> per l&apos;utente che avvia il server).</p>
    <label class="device-label" style="margin-top:14px;display:block;">Dispositivo</label>
    <select id="btMacSelect" style="width:100%;max-width:480px;padding:12px 14px;margin-top:4px;border-radius:8px;border:2px solid #3f3f46;background:#27272a;color:#fff;font-size:14px;">
      <option value="">-- Scegli dopo scan / Aggiorna --</option>
    </select>
    <p class="hint" style="margin-top:8px;">Oppure incolla il MAC manualmente:</p>
    <input type="text" id="btMacManual" placeholder="AA:BB:CC:DD:EE:FF" autocomplete="off" style="width:100%;max-width:280px;padding:12px 14px;border-radius:8px;border:2px solid #3f3f46;background:#27272a;color:#e4e4e7;font-size:14px;" />
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
      <button type="button" class="btn-primary" onclick="btDo('pair_connect')">Accoppia e connetti</button>
      <button type="button" class="btn-secondary" onclick="btDo('connect')">Solo connetti</button>
      <button type="button" class="btn-secondary" onclick="btDo('disconnect')">Disconnetti</button>
    </div>
    <p id="btCtlHint" class="hint" style="margin-top:10px;color:#a1a1aa;"></p>
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
    <p style="margin:8px 0;"><a href="/robot-control" style="display:inline-block;padding:12px 20px;background:rgba(99,102,241,0.2);color:#a5b4fc;border-radius:8px;text-decoration:none;font-weight:600;">Robot Control</a> <span class="hint">— Joystick + gesti braccia G1 (192.168.123.161).</span></p>
  </div>

  <script>
    document.getElementById('clientUrl').textContent = location.origin + '/client';
    let mics = [], spks = [], showAll = false, lastBtDevices = [];
    function escOpt(s){ return String(s||'').replace(/&/g,'&amp;').replace(/\u003c/g,'&lt;').replace(/"/g,'&quot;'); }
    function isLocalDev(m){
      return m && (m.type === 'local' || (m.value && String(m.value).indexOf('local_') === 0));
    }
    function fillBtSelect(){
      const sel = document.getElementById('btMacSelect');
      if(!sel) return;
      const rows = lastBtDevices && lastBtDevices.length ? lastBtDevices : [];
      let html = '<option value="">-- Scegli MAC --</option>';
      rows.forEach(function(b){
        if(!b || !b.mac) return;
        html += '<option value="'+escOpt(b.mac)+'">'+escOpt(b.name||b.mac)+'</option>';
      });
      sel.innerHTML = html;
    }
    function setLoading(loading){
      document.getElementById('mainSpinner').style.display = loading ? 'inline-block' : 'none';
      document.getElementById('statusText').textContent = loading ? 'Ricerca in corso...' : '';
    }
    function buildGroupedSelect(arr){
      const loc = arr.filter(isLocalDev);
      const net = arr.filter(function(m){ return !isLocalDev(m); });
      let h = '';
      if(loc.length){
        h += '<optgroup label="── Sul server (Jetson / PC) — periferiche collegate">';
        loc.forEach(function(m){ h += '<option value="'+escOpt(m.value)+'">'+escOpt(m.name)+'</option>'; });
        h += '</optgroup>';
      }
      if(net.length){
        h += '<optgroup label="── Rete WiFi — telefono, tablet, altri client">';
        net.forEach(function(m){ h += '<option value="'+escOpt(m.value)+'">'+escOpt(m.name)+'</option>'; });
        h += '</optgroup>';
      }
      return h || '<option value="web_wait">Nessuna opzione</option>';
    }
    function updateDeviceSummaries(){
      const ml = mics.filter(isLocalDev);
      const sl = spks.filter(isLocalDev);
      const ms = document.getElementById('micSummary');
      const ss = document.getElementById('spkSummary');
      if(ms){
        ms.style.display = 'block';
        if(ml.length){
          ms.className = 'device-summary has-local';
          ms.innerHTML = '<strong class="sum-title">Microfoni rilevati sul server ('+ml.length+')</strong><ul class="sum-list">'+ml.map(function(m){return '<li>'+escOpt(m.name)+'</li>';}).join('')+'</ul>';
        } else {
          ms.className = 'device-summary none';
          ms.innerHTML = '<span class="sum-warn">Nessun microfono sul server (nessuna voce USB/scheda in elenco). Collega una periferica o usa il gruppo «Rete».</span>';
        }
      }
      if(ss){
        ss.style.display = 'block';
        if(sl.length){
          ss.className = 'device-summary has-local';
          ss.innerHTML = '<strong class="sum-title">Uscite audio rilevate sul server ('+sl.length+')</strong><ul class="sum-list">'+sl.map(function(m){return '<li>'+escOpt(m.name)+'</li>';}).join('')+'</ul>';
        } else {
          ss.className = 'device-summary none';
          ss.innerHTML = '<span class="sum-warn">Nessuna uscita locale evidenziata. Collega cuffie/cassa USB o usa il gruppo «Rete».</span>';
        }
      }
    }
    function renderSelects(){
      document.getElementById('mic').innerHTML = buildGroupedSelect(mics);
      document.getElementById('speaker').innerHTML = buildGroupedSelect(spks);
      updateDeviceSummaries();
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
        lastBtDevices = bt;
        fillBtSelect();
        if(bt.length){ document.getElementById('btStep').style.display='block'; document.getElementById('btList').textContent = bt.map(b=>b.name).join(', '); }
        else { document.getElementById('btStep').style.display='block'; document.getElementById('btList').textContent = 'Nessun BT in elenco. Usa «Cerca» (~10s) o incolla un MAC sotto.'; }
        renderSelects();
        setLoading(false);
        document.getElementById('showAllBtn').textContent = showAll ? 'Solo consigliati' : 'Mostra tutti i dispositivi';
        const micLocal = mics.filter(isLocalDev);
        const spkLocal = spks.filter(isLocalDev);
        const micNet = mics.filter(function(m){ return !isLocalDev(m); }).length;
        const spkNet = spks.filter(function(s){ return !isLocalDev(s); }).length;
        document.getElementById('micCount').textContent = micLocal.length + ' sul server · ' + micNet + ' in rete';
        document.getElementById('spkCount').textContent = spkLocal.length + ' sul server · ' + spkNet + ' in rete';
        return fetch('/api/config');
      }).then(r=>r&&r.json()).then(cfg=>{
        if(cfg&&cfg.microphone&&cfg.microphone.value) document.getElementById('mic').value = cfg.microphone.value;
        if(cfg&&cfg.speaker&&cfg.speaker.value) document.getElementById('speaker').value = cfg.speaker.value;
      }).catch(e=>{ setLoading(false); document.getElementById('statusText').textContent = 'Dispositivi locali non disponibili - usa Rete WiFi'; });
    }
    function toggleShowAll(){ showAll = !showAll; refreshDevices(); }
    function refreshDevices(){ loadDevices(); }
    function scanBluetooth(){
      const btn = document.getElementById('btScanBtn');
      const hint = document.getElementById('btScanHint');
      if(btn) btn.disabled = true;
      if(hint) hint.textContent = 'Scansione in corso (~10s), attendere...';
      fetch('/api/bluetooth-scan?seconds=10').then(r=>r.json()).then(d=>{
        if(btn) btn.disabled = false;
        if(hint) hint.textContent = d.ok ? 'Scan completato.' : ('Errore: '+(d.warning||''));
        const listEl = document.getElementById('btList');
        const step = document.getElementById('btStep');
        if(step) step.style.display = 'block';
        if(d.devices && d.devices.length){
          lastBtDevices = d.devices;
          fillBtSelect();
          if(listEl) listEl.textContent = d.devices.map(b=>b.name+(b.mac?' ['+b.mac+']':'')).join(', ');
        } else {
          if(listEl) listEl.textContent = (d.warning || 'Nessun dispositivo. Verifica adattatore BT e permessi sul server.');
        }
      }).catch(e=>{
        if(btn) btn.disabled = false;
        if(hint) hint.textContent = 'Errore rete o server.';
      });
    }
    function btMacChosen(){
      const manual = document.getElementById('btMacManual');
      const m = manual && manual.value.trim();
      if(m) return m;
      const s = document.getElementById('btMacSelect');
      return (s && s.value) ? s.value : '';
    }
    function btDo(action){
      const mac = btMacChosen();
      const hint = document.getElementById('btCtlHint');
      if(!mac){ alert('Scegli un dispositivo dalla lista o incolla il MAC (AA:BB:CC:DD:EE:FF)'); return; }
      if(hint) hint.textContent = 'Comando Bluetooth in corso (può richiedere 1–2 minuti)...';
      fetch('/api/bluetooth-control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:action,mac:mac})})
        .then(r=>r.json()).then(d=>{
          if(hint) hint.textContent = (d.ok ? 'OK — ' : '') + (d.message || '');
          if(hint) hint.style.color = d.ok ? '#22c55e' : '#f87171';
          if(d.ok) refreshDevices();
        }).catch(e=>{ if(hint) hint.textContent = e.message || String(e); });
    }
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
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#14b8a6">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <link rel="manifest" href="/manifest.json">
  <title>G1 Talk - Parla</title>
  <!-- Nessun font esterno: evita richieste cross-origin e avvisi «misto/non sicuro» su alcuni browser mobile. -->
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, Ubuntu, "Helvetica Neue", sans-serif;
      margin: 0;
      padding: 0;
      background: linear-gradient(160deg, #0c0e14 0%, #141922 50%, #0d0f14 100%);
      color: #e8eaed;
      min-height: 100vh;
      max-width: 420px;
      margin: 0 auto;
      position: relative;
    }
    .header {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 16px 20px;
      padding-top: calc(16px + env(safe-area-inset-top, 0px));
      border-bottom: 1px solid rgba(255,255,255,0.06);
      background: rgba(0,0,0,0.92);
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      margin: 0 auto;
      max-width: 420px;
      width: 100%;
      box-sizing: border-box;
      z-index: 10000;
      isolation: isolate;
      -webkit-backface-visibility: hidden;
      backface-visibility: hidden;
      touch-action: manipulation;
    }
    .hamburger {
      width: 40px;
      height: 40px;
      border: none;
      background: rgba(255,255,255,0.06);
      color: #e8eaed;
      font-size: 22px;
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: background 0.2s;
      position: relative;
      z-index: 2;
      flex-shrink: 0;
    }
    .hamburger:hover { background: rgba(20,184,166,0.2); }
    .header h1 { font-size: 1.25rem; font-weight: 600; margin: 0; flex: 1; }
    /* Chiusa: display:none = nessun layer nel hit-test (WebKit spesso ignora pointer-events/clip su fixed+transform). */
    .sidebar {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: min(280px, 85vw);
      max-width: 280px;
      height: 100vh;
      height: 100dvh;
      box-sizing: border-box;
      background: linear-gradient(180deg, #0f1117 0%, #141922 100%);
      border-right: 1px solid rgba(255,255,255,0.08);
      z-index: 200;
      padding: 20px 0;
      padding-top: max(20px, env(safe-area-inset-top));
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
    }
    .sidebar.open { display: block; }
    .sidebar nav a {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 20px;
      color: #a1a1aa;
      text-decoration: none;
      font-size: 15px;
      transition: color 0.15s, background 0.15s;
      border-left: 3px solid transparent;
    }
    .sidebar nav a:hover, .sidebar nav a.active { color: #14b8a6; background: rgba(20,184,166,0.08); border-left-color: #14b8a6; }
    .sidebar nav a .icon { font-size: 20px; }
    .overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.5);
      z-index: 150;
      display: none;
      opacity: 0;
      transition: opacity 0.25s;
      pointer-events: none;
    }
    .overlay.visible { display: block; opacity: 1; pointer-events: auto; }
    .overlay:not(.visible) {
      display: none !important;
      visibility: hidden !important;
      pointer-events: none !important;
    }
    #sidebar:not(.open) {
      display: none !important;
      visibility: hidden !important;
      pointer-events: none !important;
    }
    #sidebar.open {
      display: block !important;
      visibility: visible !important;
      pointer-events: auto !important;
    }
    /* pointer-events sul main rimosso: su alcuni WebView rompeva la griglia soundboard (slot invisibili / non cliccabili). */
    .main-content {
      padding: 24px;
      padding-top: calc(24px + 5.5rem + env(safe-area-inset-top, 0px));
      position: relative;
      touch-action: auto;
    }
    /* Niente translateZ/isolation qui: su WebKit mobile possono rompere tap su select/bottoni nel main. */
    .main-content select,
    .main-content button,
    .main-content .btn,
    .main-content summary,
    .main-content input[type="checkbox"],
    .main-content input[type="radio"],
    .main-content textarea {
      touch-action: auto !important;
      pointer-events: auto !important;
      position: relative;
      z-index: 2;
    }
    .section { display: none; }
    .section.active { display: block; }
    #section-soundboard.section.active { display: block !important; }
    #section-robot.section.active { display: flex !important; flex-direction: column; min-height: calc(100vh - 100px); }
    #robotControlFrame { flex: 1; width: 100%; min-height: 360px; border: 0; border-radius: 12px; background: #0f1115; }
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
    @media (max-width: 480px) {
      body { max-width: 100%; padding: 0 env(safe-area-inset-right) 0 env(safe-area-inset-left); padding-bottom: env(safe-area-inset-bottom); }
      .header {
        padding: 12px 16px;
        padding-left: max(16px, env(safe-area-inset-left));
        padding-right: max(16px, env(safe-area-inset-right));
        padding-top: calc(12px + env(safe-area-inset-top, 0px));
      }
      .hamburger { width: 44px; height: 44px; font-size: 24px; min-width: 44px; min-height: 44px; }
      .main-content { padding: 16px; padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); }
      .main-content select { font-size: 16px !important; min-height: 44px; }
      .btn { width: 160px; height: 160px; font-size: 15px; margin: 20px auto; }
      .btn-allow { padding: 16px 20px; min-height: 48px; font-size: 15px; }
      input[type="text"] { font-size: 16px; min-height: 44px; }
      .step { padding: 14px; margin: 10px 0; }
      #soundboardGrid { grid-template-columns: repeat(4, 1fr) !important; gap: 10px !important; }
      #soundboardScroll { min-height: 200px; }
      #sbModal > div { max-width: 100%; margin: env(safe-area-inset-top) 12px env(safe-area-inset-bottom); padding: 20px; }
      #sbModal button, #sbModal label { min-height: 44px; padding: 12px 16px; display: inline-flex; align-items: center; justify-content: center; }
      #sbModal input[type="range"] { min-height: 36px; }
      #textInput, #btnText { min-height: 48px !important; }
      .result { padding: 14px; font-size: 14px; }
    }
    @media (max-width: 360px) {
      #soundboardGrid { grid-template-columns: repeat(3, 1fr) !important; }
    }
    * { -webkit-tap-highlight-color: transparent; }
    /* auto sulla root: manipulation su html può rompere select e tap su contenuti dinamici (iOS/Android). */
    html { touch-action: auto; }
    /* select/summary: auto evita che iOS/WebKit non apra il menu nativo (tendine). No "label" qui: può coprire i select. */
    button, a, [role="button"], .sidebar nav a, .hamburger { touch-action: manipulation; cursor: pointer; }
    select, summary, input[type="checkbox"], input[type="radio"] { touch-action: auto; cursor: pointer; }
    @media (max-width: 480px) {
      input.wake-checkbox { width: auto !important; height: auto !important; min-width: 44px; min-height: 44px; }
    }
    button, .btn, .btn-allow, .hamburger, .client-tab { -webkit-user-select: none; user-select: none; }
    #soundboardScroll { max-height: min(62vh, 520px); overflow-y: auto; -webkit-overflow-scrolling: touch; touch-action: pan-y pinch-zoom; margin-bottom: 8px; }
    #soundboardScroll, #soundboardGrid, #soundboardGrid [role="button"] {
      pointer-events: auto !important;
    }
    #soundboardGrid [role="button"] { touch-action: manipulation; }
    .sb-slot-text { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.25; word-break: break-word; }
    #sbModalText { width: 100%; min-height: 72px; padding: 10px; margin-top: 4px; background: #27272a; border: 1px solid #3f3f46; border-radius: 8px; color: #fff; font-family: inherit; font-size: 13px; resize: vertical; }
    #runSheetTable { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }
    #runSheetTable th, #runSheetTable td { border: 1px solid rgba(255,255,255,0.1); padding: 8px; text-align: left; vertical-align: top; }
    #runSheetTable th { color: #9ca3af; font-weight: 600; }
    #runSheetTable input { width: 100%; box-sizing: border-box; padding: 6px 8px; background: #27272a; border: 1px solid #3f3f46; border-radius: 6px; color: #e4e4e7; font-size: 12px; }
    #runSheetPolicy { width: 100%; min-height: 56px; padding: 10px; background: #27272a; border: 1px solid #3f3f46; border-radius: 8px; color: #e4e4e7; font-size: 13px; font-family: inherit; }
    .quick-guide {
      background: rgba(20,184,166,0.09);
      border: 1px solid rgba(20,184,166,0.28);
      border-radius: 12px;
      padding: 10px 12px;
      margin-bottom: 14px;
      font-size: 12px;
      line-height: 1.5;
      color: #b4b4bc;
    }
    .quick-guide strong { color: #f4f4f5; font-weight: 600; }
    .quick-guide ul { margin: 6px 0 0; padding-left: 18px; }
    .quick-guide li { margin: 4px 0; }
    .quick-guide details { margin-top: 6px; }
    .quick-guide summary { cursor: pointer; color: #2dd4bf; font-weight: 600; font-size: 13px; list-style: none; }
    .quick-guide summary::-webkit-details-marker { display: none; }
    /* Telefono: tab nel flusso del main (no position:fixed → niente layer/hit-test rotti su WebKit). */
    .client-section-tabs {
      display: none;
      flex-wrap: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      gap: 6px;
      padding: 8px 0 12px;
      margin: 0 0 8px;
      border-bottom: 1px solid rgba(255,255,255,0.1);
      scrollbar-width: none;
    }
    .client-section-tabs::-webkit-scrollbar { display: none; }
    .client-tab {
      flex: 1 0 auto;
      min-width: 52px;
      max-width: 72px;
      margin: 0;
      border: none;
      background: rgba(255,255,255,0.06);
      color: #a1a1aa;
      font-size: 9px;
      font-weight: 600;
      font-family: inherit;
      padding: 8px 4px;
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 3px;
      line-height: 1.1;
      touch-action: manipulation;
      -webkit-tap-highlight-color: rgba(20,184,166,0.25);
    }
    .client-tab .ct-ic { font-size: 18px; line-height: 1; }
    .client-tab.active { color: #2dd4bf; background: rgba(20,184,166,0.15); border: 1px solid rgba(20,184,166,0.35); }
    @media (max-width: 480px) {
      /* Spazio sotto header fisso “G1 Talk” che altrimenti taglia la riga tab */
      .client-section-tabs { display: flex; margin-top: 40px; }
      .header .hamburger { display: none !important; }
    }
    @media (min-width: 481px) {
      .client-section-tabs { display: none !important; }
    }
  </style>
</head>
<body>
  <!-- overlay/sidebar poi <main>; header in fondo al body: ultimo sibling = hit-test corretto su WebKit mobile (hamburger). -->
  <div class="overlay" id="overlay"></div>
  <aside class="sidebar" id="sidebar">
    <nav>
      <a href="#" data-section="soundboard" class="active"><span class="icon">&#128266;</span> Soundboard</a>
      <a href="#" data-section="runsheet"><span class="icon">&#128197;</span> Tempi evento</a>
      <a href="#" data-section="parla"><span class="icon">&#127908;</span> Parla</a>
      <a href="#" data-section="knowledge"><span class="icon">&#128214;</span> Knowledge</a>
      <a href="#" data-section="devices"><span class="icon">&#128268;</span> Dispositivi</a>
      <a href="#" data-section="robot"><span class="icon">&#127918;</span> Robot (G1)</a>
      <a href="#" data-section="info"><span class="icon">&#8505;</span> Info</a>
    </nav>
  </aside>
  <main class="main-content">
    <nav class="client-section-tabs" id="clientSectionTabs" aria-label="Sezioni">
      <button type="button" class="client-tab active" data-section="soundboard" onclick="return window.g1ActivateClientSection('soundboard')"><span class="ct-ic" aria-hidden="true">&#128266;</span><span>Sound</span></button>
      <button type="button" class="client-tab" data-section="runsheet" onclick="return window.g1ActivateClientSection('runsheet')"><span class="ct-ic" aria-hidden="true">&#128197;</span><span>Tempi</span></button>
      <button type="button" class="client-tab" data-section="parla" onclick="return window.g1ActivateClientSection('parla')"><span class="ct-ic" aria-hidden="true">&#127908;</span><span>Parla</span></button>
      <button type="button" class="client-tab" data-section="knowledge" onclick="return window.g1ActivateClientSection('knowledge')"><span class="ct-ic" aria-hidden="true">&#128214;</span><span>Know</span></button>
      <button type="button" class="client-tab" data-section="devices" onclick="return window.g1ActivateClientSection('devices')"><span class="ct-ic" aria-hidden="true">&#128268;</span><span>I/O</span></button>
      <button type="button" class="client-tab" data-section="robot" onclick="return window.g1ActivateClientSection('robot')"><span class="ct-ic" aria-hidden="true">&#127918;</span><span>Robot</span></button>
      <button type="button" class="client-tab" data-section="info" onclick="return window.g1ActivateClientSection('info')"><span class="ct-ic" aria-hidden="true">&#8505;</span><span>Info</span></button>
    </nav>
    <div id="persistentMicLevel" style="padding:5px 12px;display:flex;align-items:center;gap:8px;background:rgba(20,184,166,0.05);border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:2px;">
      <span style="font-size:11px;color:#71717a;white-space:nowrap;">&#127908; Mic</span>
      <div style="flex:1;height:10px;background:#1e1e2e;border-radius:5px;overflow:hidden;border:1px solid rgba(255,255,255,0.08);">
        <div id="persistMicBar" style="width:0%;height:100%;background:#22c55e;transition:width 0.06s;border-radius:5px;"></div>
      </div>
      <span id="persistMicLabel" style="font-size:10px;color:#71717a;font-family:monospace;min-width:50px;">--</span>
    </div>
    <script>
    (function(){
      function closeDrawer(){
        var sb = document.getElementById('sidebar');
        var ov = document.getElementById('overlay');
        if (sb) sb.classList.remove('open');
        if (ov) ov.classList.remove('visible');
      }
      window.g1ActivateClientSection = function(sec){
        var nodes, i, el = document.getElementById('section-'+sec);
        if (!el) return false;
        closeDrawer();
        nodes = document.querySelectorAll('main.main-content .section');
        for (i = 0; i < nodes.length; i++) nodes[i].classList.remove('active');
        el.classList.add('active');
        nodes = document.querySelectorAll('#clientSectionTabs .client-tab');
        for (i = 0; i < nodes.length; i++) nodes[i].classList.toggle('active', nodes[i].getAttribute('data-section') === sec);
        nodes = document.querySelectorAll('#sidebar nav a');
        for (i = 0; i < nodes.length; i++) nodes[i].classList.toggle('active', nodes[i].getAttribute('data-section') === sec);
        try { window.scrollTo(0, 0); } catch (e) {}
        if (sec === 'robot') {
          var rf = document.getElementById('robotControlFrame');
          if (rf) { rf.src = location.origin + '/robot-control'; }
        }
        setTimeout(function(){
          if ((sec === 'soundboard' || sec === 'parla') && navigator.mediaDevices) {
            var o = document.getElementById('sbOutput');
            if (o && o.options && o.options.length <= 1 && typeof requestAndLoadDevices === 'function') requestAndLoadDevices();
          }
          if (sec === 'runsheet' && typeof loadRunSheet === 'function') loadRunSheet();
        }, 0);
        return false;
      };
    })();
    </script>
    <section id="section-soundboard" class="section active">
  <h2 style="font-size:1.2rem;margin:0 0 16px;">Soundboard</h2>
  <p class="hint" style="margin-bottom:8px;"><strong>Predefinito: cassa del robot</strong> (uscita audio sulla Jetson). Serve setup <code>/</code> con altoparlante <em>locale</em> salvato. «Browser» = audio su questo telefono/PC. <strong>Voce:</strong> solo TTS / registrazione naturale.</p>
  <div style="margin-bottom:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    <label style="font-size:12px;color:#9ca3af;">Uscita audio:</label>
    <select id="sbPlayDest" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;min-width:220px;">
      <option value="server" selected>Cassa sul Jetson (robot)</option>
      <option value="browser">Browser (PC / telefono)</option>
    </select>
    <label id="sbBrowserSinkLabel" style="font-size:12px;color:#9ca3af;">Riproduci su (solo se Browser):</label>
    <select id="sbOutput" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;min-width:180px;">
      <option value="default">Predefinito</option>
    </select>
    <button type="button" id="sbOutputRefresh" style="padding:6px 12px;font-size:12px;background:rgba(255,255,255,0.08);color:#9ca3af;border:1px solid rgba(255,255,255,0.1);border-radius:8px;cursor:pointer;">Aggiorna</button>
  </div>
  <p id="soundboardLoadErr" class="hint" style="display:none;margin:0 0 8px;color:#f87171;grid-column:1/-1;"></p>
  <div id="soundboardScroll">
  <div id="soundboardGrid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;"></div>
  </div>
  <p class="hint" style="margin-top:8px;font-size:11px;">Modifica (✏️): registra, importa, <b>Genera TTS dal testo</b>, icona. Audio sempre naturale.</p>
    <div class="quick-guide" id="quickGuide">
      <details>
        <summary>Come funziona (apri per la guida rapida)</summary>
        <p style="margin:8px 0 4px;"><strong>Due modi, un solo server</strong> — voce e IA girano sul <strong>PC del G1</strong> (o Linux sulla stessa rete). Questa pagina è il <strong>telecomando</strong> dal telefono o dal PC.</p>
        <ul>
          <li><strong>Browser (consigliato):</strong> stesso WiFi del robot → apri questo indirizzo (<code id="guideUrl">/client</code>). Per il microfono serve HTTPS; al primo accesso «Avanzate → Procedi».</li>
          <li><strong>APK Android:</strong> stessa schermata; inserisci l’IP del server nell’app launcher. Per la <strong>cassa Bluetooth</strong>: accoppia il telefono all’altoparlante in Impostazioni → l’audio esce lì.</li>
          <li><strong>Prima volta sul robot:</strong> copia lo zip d’installazione, <code>bash install.sh</code>, configura <code>.env</code>, poi <code>bash scripts/restart_server.sh</code>. Pacchetto Windows: <code>dist/G1_Pacchetto_Installazione_Completa.zip</code>.</li>
        </ul>
        <p class="hint" style="margin:6px 0 0;font-size:11px;">Dettagli in menu <strong>Info</strong> · Leggi anche <code>LEGGIMI.txt</code> nel pacchetto.</p>
      </details>
    </div>
  <div id="sbModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:15000;padding:20px;flex-direction:column;align-items:center;justify-content:center;overflow-y:auto;-webkit-overflow-scrolling:touch;">
    <div style="background:#1a1d24;border-radius:16px;padding:24px;max-width:400px;width:100%;border:1px solid rgba(255,255,255,0.1);margin:auto;">
      <h3 style="margin:0 0 16px;font-size:1.1rem;">Modifica slot <span id="sbModalSlot">0</span></h3>
      <div style="margin-bottom:12px;">
        <label style="font-size:12px;color:#9ca3af;">Icona</label>
        <input type="text" id="sbModalIcon" placeholder="🎤" style="width:60px;padding:8px;margin-left:8px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#fff;font-size:18px;" />
      </div>
      <div style="margin-bottom:12px;">
        <label style="font-size:12px;color:#9ca3af;">Testo (etichetta e per TTS)</label>
        <textarea id="sbModalText" placeholder="Frase da sintetizzare" maxlength="1000"></textarea>
        <p class="hint" style="margin:4px 0 0;font-size:11px;"><span id="sbModalCharCount">0</span> / <span id="sbModalCharMax">280</span></p>
      </div>
      <div style="margin-bottom:12px;padding:12px;background:rgba(255,255,255,0.04);border-radius:10px;border:1px solid rgba(255,255,255,0.06);">
        <div id="sbModalAudioStatus" style="font-size:13px;color:#9ca3af;margin-bottom:8px;">Nessun audio</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <button type="button" id="sbModalSynth" style="padding:10px 16px;background:#6366f1;color:#fff;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;">Genera TTS dal testo</button>
          <button type="button" id="sbModalRecord" style="padding:10px 16px;background:#14b8a6;color:#0c0e14;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;">Registra 3 sec</button>
          <label style="padding:10px 16px;background:rgba(255,255,255,0.1);color:#e8eaed;border-radius:8px;cursor:pointer;font-size:13px;">Importa file<input type="file" id="sbModalFile" accept="audio/*" style="display:none;" /></label>
          <button type="button" id="sbModalTts" style="padding:10px 16px;background:#8b5cf6;color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;" title="Riprocessa con TTS">Voce TTS da audio</button>
          <button type="button" id="sbModalClear" style="padding:10px 16px;background:rgba(239,68,68,0.3);color:#fca5a5;border:none;border-radius:8px;cursor:pointer;font-size:13px;">Rimuovi audio</button>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-top:16px;">
        <button type="button" id="sbModalSave" style="flex:1;padding:12px;background:#14b8a6;color:#0c0e14;border:none;border-radius:10px;cursor:pointer;font-weight:600;">Salva</button>
        <button type="button" id="sbModalCancel" style="padding:12px 20px;background:rgba(255,255,255,0.1);color:#e8eaed;border:none;border-radius:10px;cursor:pointer;">Annulla</button>
      </div>
    </div>
  </div>
    </section>
    <section id="section-runsheet" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 12px;">Run sheet / tempistica</h2>
  <p class="hint" style="margin-bottom:10px;"><strong>Uso:</strong> tabella di supporto per l’evento. Inserisci orari e note quando l’organizzatore te li comunica; <strong>Salva tempi</strong> memorizza sul server. Consultala mentre usi la soundboard o Parla.</p>
  <label style="font-size:12px;color:#9ca3af;display:block;margin-bottom:6px;">Policy autonomia robot</label>
  <textarea id="runSheetPolicy" style="margin-bottom:14px;"></textarea>
  <div style="overflow-x:auto;">
    <table id="runSheetTable">
      <thead><tr><th>Fase</th><th>Attività</th><th>Ora inizio</th><th>Durata stimata</th><th>Note</th></tr></thead>
      <tbody id="runSheetBody"></tbody>
    </table>
  </div>
  <button type="button" id="runSheetSave" style="margin-top:14px;padding:10px 20px;background:#14b8a6;color:#0c0e14;border:none;border-radius:10px;cursor:pointer;font-weight:600;">Salva tempi</button>
  <span id="runSheetStatus" class="hint" style="margin-left:12px;"></span>
    </section>
    <section id="section-parla" class="section">

  <!-- 1. ASCOLTO CONTINUO (Wake Word) -->
  <div style="margin-bottom:20px;padding:16px;border-radius:12px;background:rgba(20,184,166,0.06);border:1px solid rgba(20,184,166,0.2);">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
      <h2 style="font-size:1.15rem;margin:0;color:#2dd4bf;">Ascolto continuo</h2>
      <label for="wakeListenToggle" style="display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none;">
        <span style="font-size:12px;color:#9ca3af;" id="wakeToggleLabel">OFF</span>
        <input type="checkbox" id="wakeListenToggle" class="wake-checkbox" style="width:22px;height:22px;margin:0;accent-color:#14b8a6;" />
      </label>
    </div>
    <p id="wakeListenStatus" style="margin:0 0 6px;font-size:13px;color:#71717a;">Disattivato</p>
    <div id="wakeDebugLog" style="max-height:60px;overflow-y:auto;font-size:10px;font-family:monospace;color:#52525b;line-height:1.4;margin:0 0 8px;padding:4px 8px;background:rgba(0,0,0,0.2);border-radius:6px;display:none;"></div>
    <div id="recStatus" style="min-height:30px;">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
        <div style="flex:1;height:16px;background:#1e1e2e;border-radius:8px;overflow:hidden;border:1px solid rgba(255,255,255,0.06);">
          <div id="levelBar" style="width:0%;height:100%;background:#22c55e;transition:width 0.06s;border-radius:8px;"></div>
        </div>
        <span id="levelLabel" style="font-size:12px;color:#71717a;font-family:monospace;min-width:140px;">Livello: --</span>
      </div>
      <p class="hint" id="recDebug" style="font-size:11px;color:#71717a;min-height:16px;margin:0;"></p>
    </div>
    <div id="activeMicIndicator" style="margin:8px 0 0;padding:6px 10px;border-radius:6px;font-size:12px;display:flex;align-items:center;gap:8px;background:rgba(255,255,255,0.03);">
      <span id="activeMicDot" style="width:8px;height:8px;border-radius:50%;background:#71717a;flex-shrink:0;"></span>
      <span id="activeMicLabel" style="color:#9ca3af;">Microfono: caricamento...</span>
    </div>
    <details id="parlaMicPreviewPanel" style="margin:12px 0 0;border-radius:10px;background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.2);">
      <summary style="padding:10px 12px;cursor:pointer;font-size:12px;color:#93c5fd;font-weight:600;user-select:none;">Microfono e sensibilit&agrave;</summary>
      <div style="padding:0 12px 12px;">
      <p class="hint" id="parlaSetupHintTop" style="margin:0 0 8px;font-size:10px;color:#64748b;line-height:1.4;"><strong>Setup tipico:</strong> microfono su questo telefono (es. DJI Mic Mini), cassa <strong>Bluetooth</strong> accoppiata al telefono — in Soundboard scegli <strong>Browser</strong> e la cassa in <strong>Riproduci su</strong>. Gesti robot: tab Robot, IP <code>192.168.123.161</code> (salvato nel browser).</p>
      <div id="parlaPreviewDisabledMsg" style="display:none;font-size:11px;color:#f59e0b;margin-bottom:8px;">Seleziona un microfono <strong>Browser</strong> in Dispositivi e consenti l&apos;accesso per vedere il livello qui.</div>
      <div id="parlaPreviewMeterWrap" style="position:relative;">
        <div style="position:relative;height:20px;background:#1e1e2e;border-radius:8px;overflow:hidden;border:1px solid rgba(255,255,255,0.08);">
          <div id="parlaPreviewThresholdLine" style="position:absolute;top:0;bottom:0;width:3px;background:#f97316;z-index:3;opacity:0.9;left:4%;box-shadow:0 0 4px #f97316;"></div>
          <div id="parlaPreviewBarFill" style="position:relative;height:100%;width:0%;background:linear-gradient(90deg,#22c55e,#84cc16,#eab308);border-radius:8px;transition:width 0.06s linear;z-index:1;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;flex-wrap:wrap;gap:6px;">
          <span id="parlaPreviewStatus" style="font-size:11px;color:#a1a1aa;font-family:monospace;">Picco: — · soglia: —</span>
          <span id="parlaPreviewGate" style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:#27272a;color:#71717a;">—</span>
        </div>
      </div>
      <div style="margin-top:12px;">
        <label for="micWakeThresholdSlider" style="font-size:11px;color:#9ca3af;display:block;margin-bottom:4px;">Sensibilit&agrave; ascolto continuo (soglia invio voce): <strong id="wakeThDisplay">14</strong> <span style="color:#52525b;">(pi&ugrave; basso = pi&ugrave; sensibile)</span></label>
        <input type="range" id="micWakeThresholdSlider" min="1" max="80" value="14" style="width:100%;max-width:340px;accent-color:#3b82f6;" />
      </div>
      <div style="margin-top:10px;">
        <label for="micMonitorGainSlider" style="font-size:11px;color:#9ca3af;display:block;margin-bottom:4px;">Guadagno indicatore (solo barra, non cambia l&apos;audio registrato): <strong id="micGainDisplay">1.0</strong>×</label>
        <input type="range" id="micMonitorGainSlider" min="0.4" max="4" step="0.1" value="1" style="width:100%;max-width:340px;accent-color:#64748b;" />
      </div>
      </div>
    </details>
  </div>

  <!-- 2. PUSH TO TALK -->
  <div style="margin-bottom:20px;">
    <h2 style="font-size:1.15rem;margin:0 0 10px;color:#e4e4e7;">Tieni premuto e parla</h2>
    <button class="btn" id="btn">Tieni premuto e parla</button>
  </div>

  <!-- VOLUME CASSA -->
  <div style="margin-bottom:14px;padding:10px 14px;border-radius:10px;background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.18);display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
    <label for="parlaGainSlider" style="font-size:12px;color:#86efac;font-weight:600;white-space:nowrap;">Volume cassa</label>
    <input type="range" id="parlaGainSlider" min="0.5" max="10.0" step="0.1" style="flex:1;min-width:100px;accent-color:#22c55e;" />
    <span id="parlaGainLabel" style="font-size:12px;color:#a1a1aa;font-family:monospace;min-width:36px;">2.5x</span>
  </div>

  <!-- RISULTATO (condiviso) -->
  <div class="result" id="result"></div>

  <!-- 3. SCRIVI -->
  <div style="margin-top:16px;margin-bottom:16px;">
    <h2 style="font-size:1.15rem;margin:0 0 8px;color:#e4e4e7;">Scrivi una domanda</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <input type="text" id="textInput" placeholder="Es: Che ore sono?" style="flex:1;min-width:180px;padding:10px 14px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:14px;" />
      <button type="button" id="btnText" style="padding:12px 22px;background:#14b8a6;color:#0c0e14;border:none;border-radius:10px;cursor:pointer;font-weight:600;">Invia</button>
    </div>
    <p class="hint" id="textStatus" style="margin-top:6px;min-height:16px;"></p>
  </div>

  <!-- CONFIG -->
  <div id="secureContextWarn" class="step" style="display:none;border-color:rgba(251,191,36,0.4);background:rgba(251,191,36,0.08);padding:14px;">
    <p style="margin:0 0 10px;font-size:13px;color:#fcd34d;"><strong>Serve HTTPS per il microfono browser.</strong></p>
    <p style="margin:0 0 10px;font-size:13px;"><a id="secureHttpsLink" href="#" style="color:#5eead4;font-weight:600;">Apri versione HTTPS</a></p>
    <p style="margin:0;font-size:12px;color:#a1a1aa;"><a href="#" id="secureWarnMore" style="color:#14b8a6;">Dettagli</a></p>
    <details id="secureWarnDetails" style="display:none;margin-top:10px;font-size:12px;color:#9ca3af;">
      <div id="secureWarnMobile" style="display:none;"><p style="margin:0;">Avanzate, Procedi.</p></div>
      <div id="secureWarnDesktop" style="display:none;"><p style="margin:0;">Tunnel SSH poi localhost:8081/client</p></div>
    </details>
  </div>
  <div id="allowWrap" class="step" style="display:block;margin-bottom:8px;">
    <button type="button" class="btn-allow" id="btnAllow" style="font-size:12px;padding:8px 14px;">Consenti microfono</button>
    <p id="deviceStatus" style="font-size:10px;margin:4px 0 0;color:#52525b;">Clicca per caricare dispositivi.</p>
    <p class="hint" id="hintAccess" style="margin:4px 0 0;font-size:10px;">Per il microfono browser: consenti l'accesso.</p>
  </div>
  <details style="margin-bottom:10px;">
    <summary style="cursor:pointer;font-size:12px;color:#71717a;">Uscita audio (TTS)</summary>
    <div id="ttsOutputWrap" class="step" style="margin-top:8px;margin-bottom:0;padding:10px 12px;background:rgba(59,130,246,0.06);border-radius:8px;border:1px solid rgba(59,130,246,0.2);">
      <label style="display:block;margin-bottom:4px;color:#a1a1aa;font-size:12px;">Risposta vocale</label>
      <select id="ttsPlayDest" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;width:100%;max-width:300px;">
        <option value="server">Cassa robot (Jetson)</option>
        <option value="browser">Browser (telefono/PC)</option>
      </select>
      <p id="ttsServerHint" class="hint" style="margin:4px 0 0;font-size:10px;color:#52525b;"></p>
    </div>
  </details>
  <details style="margin-top:8px;">
    <summary style="cursor:pointer;font-size:12px;color:#52525b;">Test & debug</summary>
    <div style="margin-top:8px;">
      <button type="button" id="btnTest" style="padding:8px 14px;background:rgba(255,255,255,0.06);color:#9ca3af;border:1px solid rgba(255,255,255,0.08);border-radius:8px;cursor:pointer;font-size:12px;">Test pipeline</button>
      <button type="button" id="btnSample" style="padding:8px 14px;background:rgba(255,255,255,0.06);color:#9ca3af;border:1px solid rgba(255,255,255,0.08);border-radius:8px;cursor:pointer;font-size:12px;margin-left:8px;">Test campione</button>
      <button type="button" id="btnMicMonitor" style="padding:8px 14px;background:rgba(255,255,255,0.06);color:#9ca3af;border:1px solid rgba(255,255,255,0.08);border-radius:8px;cursor:pointer;font-size:12px;margin-left:8px;">Monitor mic</button>
      <span id="testStatus" style="font-size:12px;"></span>
      <div id="micMonitorBody" style="display:none;margin-top:8px;">
        <div style="flex:1;height:12px;background:#1e1e2e;border-radius:6px;overflow:hidden;border:1px solid rgba(255,255,255,0.06);margin-bottom:4px;">
          <div id="monLevelBar" style="width:0%;height:100%;background:#22c55e;transition:width 0.06s;border-radius:6px;"></div>
        </div>
        <div style="display:flex;justify-content:space-between;"><span id="monMicName" style="font-size:10px;color:#71717a;">--</span><span id="monLevelInfo" style="font-size:11px;color:#9ca3af;font-family:monospace;">RMS=-- Peak=-- dB=--</span></div>
      </div>
    </div>
  </details>
  <div id="micMonitorWrap" style="display:none;"></div>
    </section>
    <section id="section-knowledge" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 16px;">Knowledge</h2>
  <p class="hint" style="margin-bottom:10px;"><strong>Uso:</strong> frasi «chiave → risposta» per McKinsey host, curiosità, FAQ. Se l’utente (o tu in <strong>Parla</strong>) dice qualcosa che <strong>contiene</strong> il pattern, il robot risponde subito senza chiamare il modello GPT (più veloce e coerente col testo).</p>
  <details id="knowledgeWrap" class="step" style="margin-bottom:12px;border:1px solid rgba(255,255,255,0.06);">
    <summary style="cursor:pointer;color:#a1a1aa;">Pattern -&gt; risposta (modifica)</summary>
    <p class="hint" style="margin-top:8px;">Aggiungi righe e <strong>Salva su server</strong>. La corrispondenza è per sottostringa nel testo riconosciuto.</p>
    <div id="knowledgeList" style="margin-top:8px;"></div>
    <div style="margin-top:8px;">
      <input type="text" id="knowledgePattern" placeholder="Pattern" style="width:45%;padding:8px;margin-right:4px;" />
      <input type="text" id="knowledgeResponse" placeholder="Risposta" style="width:45%;padding:8px;margin-right:4px;" />
      <button type="button" id="knowledgeAdd" style="padding:8px 12px;background:#14b8a6;color:#0c0e14;border:none;border-radius:8px;cursor:pointer;font-size:12px;">Aggiungi</button>
    </div>
    <button type="button" id="knowledgeSave" style="margin-top:8px;padding:8px 16px;background:rgba(255,255,255,0.1);color:#e8eaed;border:1px solid rgba(255,255,255,0.2);border-radius:8px;cursor:pointer;font-size:12px;">Salva su server</button>
  </details>
    </section>
    <section id="section-devices" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 12px;">Dispositivi</h2>
  <div id="devicesWrap" class="step">
    <label style="display:block;margin-bottom:6px;">Microfono</label>
    <select id="mic"><option value="">Caricamento...</option></select>
    <label style="display:block;margin-top:12px;margin-bottom:6px;">Altoparlante / cassa</label>
    <select id="speaker"><option value="">Caricamento...</option></select>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px;">
      <button type="button" id="devicesRefresh" style="padding:8px 14px;background:rgba(255,255,255,0.08);color:#e8eaed;border:1px solid rgba(255,255,255,0.12);border-radius:8px;cursor:pointer;font-size:13px;">Aggiorna</button>
      <button type="button" id="devicesSave" style="padding:8px 14px;background:#14b8a6;color:#0c0e14;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;">Salva</button>
      <span id="devicesSaveStatus" class="hint" style="margin:0;"></span>
    </div>
    <div style="margin-top:14px;padding:12px;background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.2);border-radius:10px;">
      <label style="display:block;font-size:12px;color:#86efac;font-weight:600;margin-bottom:6px;">Volume risposta TTS</label>
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="range" id="ttsGainSlider" min="0.5" max="10.0" step="0.1" style="flex:1;accent-color:#22c55e;" />
        <span id="ttsGainLabel" style="font-size:12px;color:#a1a1aa;font-family:monospace;min-width:40px;">2.5x</span>
      </div>
      <p class="hint" style="margin:4px 0 0;font-size:10px;color:#52525b;">1.0 = originale, 2.5 = default. Max 10x. L&apos;audio viene anche normalizzato lato server (loudnorm).</p>
    </div>
    <details style="margin-top:12px;">
      <summary style="cursor:pointer;font-size:12px;color:#71717a;">Avanzate (hardware Jetson, nomi PortAudio)</summary>
      <div style="padding-top:8px;">
        <p class="hint" style="margin-bottom:8px;"><strong>Jetson:</strong> scansione PortAudio + ALSA + USB. Voci <em>Jetson (server)</em> = audio sul robot.</p>
        <pre id="hwProbe" style="margin:0 0 10px;padding:10px;background:#18181b;border-radius:8px;font-size:10px;line-height:1.35;max-height:180px;overflow:auto;color:#a1a1aa;white-space:pre-wrap;">—</pre>
        <button type="button" id="devicesLoadFull" style="padding:6px 12px;background:rgba(59,130,246,0.25);color:#93c5fd;border:1px solid rgba(59,130,246,0.4);border-radius:8px;cursor:pointer;font-size:11px;margin-bottom:8px;">Mostra tutti i nomi</button>
        <pre id="devicesFullDump" style="margin:0 0 10px;padding:10px;background:#0f172a;border-radius:8px;font-size:10px;line-height:1.35;max-height:min(50vh,420px);overflow:auto;color:#cbd5e1;white-space:pre-wrap;display:none;">—</pre>
      </div>
    </details>
  </div>
    </section>
    <section id="section-info" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 16px;">Info</h2>
  <div class="step">
    <p style="margin:0 0 8px;"><strong>Pacchetti pronti (PC Windows, cartella <code>dist/</code>):</strong></p>
    <ul class="hint" style="margin:0 0 14px;padding-left:18px;font-size:12px;line-height:1.5;">
      <li><code>G1_Pacchetto_Installazione_Completa.zip</code> — server + audio soundboard + APK launcher + <code>LEGGIMI_INSTALLAZIONE_COMPLETA.txt</code></li>
      <li><code>G1-TalkModule-OpenAiAPI.zip</code> — solo installazione Linux sul G1 (<code>install.sh</code>)</li>
    </ul>
    <p style="margin:0 0 8px;">G1 Talk Module — assistente vocale per Unitree G1.</p>
    <p class="hint" style="margin:0 0 16px;">Menu: <strong>Soundboard</strong>, <strong>Tempi</strong>, <strong>Parla</strong>, <strong>Knowledge</strong>, <strong>Dispositivi</strong>, <strong>Robot</strong> (joystick + gesti G1 verso <code>192.168.123.161</code> da <code>.env</code>). Guida in cima alla pagina.</p>
    <p style="margin:0 0 8px;font-size:14px;"><b>Da telefono (stessa rete WiFi del server):</b></p>
    <p class="hint" style="margin:0 0 8px;">Nessun bridge. Apri (HTTPS per microfono):</p>
    <a href="http://192.168.10.191:8080/client" style="display:inline-block;padding:12px 18px;background:#14b8a6;color:#0c0e14;border-radius:10px;text-decoration:none;font-weight:600;font-size:15px;margin-bottom:4px;">192.168.10.191:8080/client</a>
    <p class="hint" style="margin:0 0 12px;font-size:11px;">Reindirizza a HTTPS. Al primo accesso: Avanzate → Procedi.</p>
    <p style="margin:0 0 6px;font-size:13px;"><b>Da PC (rete diversa):</b></p>
    <p class="hint" style="margin:0;font-size:12px;">Tunnel SSH poi localhost:8081/client</p>
  </div>
    </section>
    <section id="section-robot" class="section">
      <h2 style="font-size:1.2rem;margin:0 0 8px;">Robot G1 — Sport mode</h2>
      <p class="hint" style="margin:0 0 12px;font-size:12px;">Joystick e gesti braccia: comandi al robot (default IP <code>192.168.123.161</code>, modificabile sotto). Il robot deve essere in sport mode (telecomando).</p>
      <iframe id="robotControlFrame" title="Robot control" src="about:blank"></iframe>
    </section>
  </main>
  <header class="header">
    <button type="button" class="hamburger" id="hamburger" aria-label="Menu">&#9776;</button>
    <h1>G1 Talk</h1>
  </header>

  <script>
    (function(){
      function syncMainHeaderPad(){
        var hdr = document.querySelector('.header');
        var main = document.querySelector('.main-content');
        if (!hdr || !main) return;
        var narrow = window.matchMedia('(max-width: 480px)').matches;
        var base = narrow ? 16 : 24;
        var h = hdr.getBoundingClientRect().height;
        if (h < 32) {
          requestAnimationFrame(syncMainHeaderPad);
          return;
        }
        var slack = narrow ? 12 : 4;
        main.style.paddingTop = (base + h + slack) + 'px';
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', syncMainHeaderPad);
      else syncMainHeaderPad();
      window.addEventListener('resize', syncMainHeaderPad);
      window.addEventListener('orientationchange', function(){ setTimeout(syncMainHeaderPad, 200); });
      if (window.visualViewport) window.visualViewport.addEventListener('resize', syncMainHeaderPad);
    })();
    function arrayBufferToBase64(buffer){
      const bytes = new Uint8Array(buffer);
      let binary = '';
      const chunk = 8192;
      for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
      return btoa(binary);
    }
    (function(){
      var sidebar = document.getElementById('sidebar');
      var overlay = document.getElementById('overlay');
      var hamburger = document.getElementById('hamburger');
      function openMenu(){ if(sidebar) sidebar.classList.add('open'); if(overlay) overlay.classList.add('visible'); }
      function closeMenu(){ if(sidebar) sidebar.classList.remove('open'); if(overlay) overlay.classList.remove('visible'); }
      function toggleMenu(){ if(sidebar && sidebar.classList.contains('open')) closeMenu(); else openMenu(); }
      if(hamburger){
        var lastHb = 0;
        hamburger.addEventListener('touchend', function(e){
          e.preventDefault();
          lastHb = Date.now();
          toggleMenu();
        }, {passive:false});
        hamburger.addEventListener('click', function(e){
          e.preventDefault();
          if(Date.now() - lastHb < 450) return;
          toggleMenu();
        });
      }
      if(overlay) overlay.addEventListener('click', function(e){ e.preventDefault(); closeMenu(); });
      var navLinks = document.querySelectorAll('.sidebar nav a');
      for (var ai = 0; ai < navLinks.length; ai++) {
        navLinks[ai].addEventListener('click', function(e){
          e.preventDefault();
          var sec = this.getAttribute('data-section');
          if (sec && typeof window.g1ActivateClientSection === 'function') window.g1ActivateClientSection(sec);
        });
      }
      closeMenu();
      document.addEventListener('visibilitychange', function(){ if (document.visibilityState === 'visible') closeMenu(); });
    })();
    (function(){
      const g = document.getElementById('guideUrl');
      if (g) g.textContent = location.origin + '/client';
    })();
    const wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    const wsParlaUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/parla';
    let wsParla = null;
    let recordingServerJetson = false;
    const MAX_REC_SEC = 20;
    /* PTT: un filo più lungo prima dell'invio (meno frasi troncate). */
    const MIN_REC_MS = 1200;
    let ws = null, mediaRecorder = null, chunks = [], recTimeout = null, lastPlayOn = 'browser', lastSinkId = null;
    let serverTtsDeviceId = null;
    let _serverDevicesCache = { microphones: [], speakers: [], hardware_probe: null };
    function escapeHtmlDevices(s){
      return String(s||'').replace(/&/g,'&amp;').replace(/\u003c/g,'&lt;').replace(/"/g,'&quot;');
    }
    function updateActiveMicIndicator(){
      const dot = document.getElementById('activeMicDot');
      const lbl = document.getElementById('activeMicLabel');
      if (!dot || !lbl) return;
      const micSel = document.getElementById('mic');
      const val = micSel ? micSel.value : '';
      const opt = micSel ? micSel.options[micSel.selectedIndex] : null;
      const name = opt ? opt.textContent : '';
      const isLocal = val && String(val).indexOf('local_') === 0;
      const isBrowser = val && String(val).indexOf('webmic_') === 0;
      const wt = document.getElementById('wakeListenToggle');
      const listening = wt && wt.checked;
      if (isLocal) {
        dot.style.background = listening ? '#22c55e' : '#14b8a6';
        dot.style.boxShadow = listening ? '0 0 6px #22c55e' : 'none';
        lbl.innerHTML = '<strong style="color:#2dd4bf;">Jetson USB</strong> — ' + escapeHtmlDevices(name) + (listening ? ' <span style="color:#22c55e;">(ascolto attivo)</span>' : '');
      } else if (isBrowser) {
        dot.style.background = listening ? '#22c55e' : '#3b82f6';
        dot.style.boxShadow = listening ? '0 0 6px #22c55e' : 'none';
        lbl.innerHTML = '<strong style="color:#60a5fa;">Browser</strong> — ' + escapeHtmlDevices(name) + (listening ? ' <span style="color:#22c55e;">(ascolto attivo)</span>' : '');
      } else {
        dot.style.background = '#71717a';
        dot.style.boxShadow = 'none';
        lbl.innerHTML = '<span style="color:#71717a;">Nessun microfono selezionato</span>';
      }
    }
    function micForBrowserCapture(){
      const v = document.getElementById('mic') && document.getElementById('mic').value;
      if (!v || v === 'web_wait') return null;
      if (String(v).indexOf('local_') === 0) return null;
      if (String(v).indexOf('net_') === 0) return null;
      if (String(v).indexOf('webmic_') === 0) {
        try { return decodeURIComponent(v.slice(7)); } catch(_) { return null; }
      }
      return String(v).length > 5 ? v : null;
    }
    function buildMicCfgFromSelect(val){
      if (!val || val === 'web_wait') return { type: 'network', value: 'web_wait', name: '', device_id: '' };
      if (val.indexOf('local_') === 0) {
        const id = parseInt(val.split('_')[1], 10);
        const m = (_serverDevicesCache.microphones || []).find(function(x){ return x && x.value === val; });
        return { type: 'local', device_id: id, value: val, name: (m && m.name) || '' };
      }
      if (val.indexOf('net_') === 0) {
        const m = (_serverDevicesCache.microphones || []).find(function(x){ return x && x.value === val; });
        return { type: 'network', device_id: val.replace(/^net_/, ''), value: val, name: (m && m.name) || '' };
      }
      if (val.indexOf('webmic_') === 0) {
        try {
          const id = decodeURIComponent(val.slice(7));
          return { type: 'network', device_id: id, value: id, name: 'Browser' };
        } catch(_) { return { type: 'network', value: 'web_wait', name: '', device_id: '' }; }
      }
      return { type: 'network', device_id: val, value: val, name: 'Browser' };
    }
    function buildSpkCfgFromSelect(val){
      if (!val) return { type: 'network', value: 'web_wait', name: 'Browser' };
      if (val.indexOf('local_') === 0) {
        const id = parseInt(val.split('_')[1], 10);
        const s = (_serverDevicesCache.speakers || []).find(function(x){ return x && x.value === val; });
        return { type: 'local', device_id: id, value: val, name: (s && s.name) || '' };
      }
      if (val.indexOf('net_') === 0) {
        const s = (_serverDevicesCache.speakers || []).find(function(x){ return x && x.value === val; });
        return { type: 'network', device_id: val.replace(/^net_/, ''), value: val, name: (s && s.name) || '' };
      }
      if (val.indexOf('browser_') === 0) {
        const rest = val.slice(8);
        if (rest === 'default') return { type: 'network', value: 'web_wait', name: 'Browser predefinito' };
        return { type: 'network', device_id: rest, value: rest, name: 'Browser' };
      }
      return { type: 'network', value: 'web_wait', name: 'Browser' };
    }
    function updateHwProbe(hp){
      const el = document.getElementById('hwProbe');
      if (!el) return;
      if (!hp) { el.textContent = '(nessun dato - server non Linux?)'; return; }
      const bits = [];
      if (hp.arecord_l) bits.push('=== arecord -l (ingressi ALSA) ===\\n' + hp.arecord_l);
      if (hp.aplay_l) bits.push('=== aplay -l (uscite ALSA) ===\\n' + hp.aplay_l);
      if (hp.lsusb) bits.push('=== lsusb (audio/USB) ===\\n' + hp.lsusb);
      if (hp.asound_cards) bits.push('=== /proc/asound/cards ===\\n' + hp.asound_cards);
      el.textContent = bits.length ? bits.join('\\n\\n') : '(vuoto)';
    }
    let recStartTime = 0, recDurationInterval = null, levelInterval = null, analyserNode = null, audioCtx = null;
    let isRecording = false, pendingStop = false, currentStream = null;
    let wakeStream = null, wakeRawStream = null, wakeListenPending = false;
    let wakeListenActive = false, wakeMimeType = '', wakeActiveMr = null;
    let wsListenServer = null, wakeServerMode = false;
    let wakeCommandMode = false, wakeCommandIdleTimer = null;
    let wakeAudioInFlight = false, wakeQueuedBlob = null;
    let wakeLevelCtx = null, wakeAnalyser = null, wakeLevelSampleInterval = null;
    let wakeSlicePeak = 0;
    /** Default soglia voce (0-255 FFT); override con slider e localStorage g1_wake_voice_threshold. */
    const WAKE_VOICE_THRESHOLD_DEFAULT = 14;
    function getWakeVoiceThreshold() {
      try {
        var raw = localStorage.getItem('g1_wake_voice_threshold');
        if (raw != null && raw !== '') {
          var v = parseInt(raw, 10);
          if (!isNaN(v) && v >= 1 && v <= 80) return v;
        }
      } catch (_) {}
      return WAKE_VOICE_THRESHOLD_DEFAULT;
    }
    let parlaPreviewTimer = null;
    let parlaPreviewCtx = null;
    let parlaPreviewAnalyser = null;
    let parlaPreviewStream = null;
    function getParlaMonitorGain() {
      try {
        var g = parseFloat(localStorage.getItem('g1_mic_monitor_gain'));
        if (!isNaN(g) && g >= 0.4 && g <= 4) return g;
      } catch (_) {}
      return 1;
    }
    function stopParlaMicPreview() {
      if (parlaPreviewTimer) { clearInterval(parlaPreviewTimer); parlaPreviewTimer = null; }
      if (parlaPreviewCtx) {
        try { parlaPreviewCtx.close(); } catch (_) {}
        parlaPreviewCtx = null;
      }
      parlaPreviewAnalyser = null;
      if (parlaPreviewStream) {
        try { parlaPreviewStream.getTracks().forEach(function(t){ try { t.stop(); } catch(_){} }); } catch (_) {}
        parlaPreviewStream = null;
      }
    }
    function updateParlaThresholdLine() {
      var line = document.getElementById('parlaPreviewThresholdLine');
      if (!line) return;
      var th = getWakeVoiceThreshold();
      var pct = Math.max(0, Math.min(100, (th / 255) * 100));
      line.style.left = 'calc(' + pct + '% - 1px)';
    }
    (function initParlaMicControls(){
      var wTh = document.getElementById('micWakeThresholdSlider');
      var wDisp = document.getElementById('wakeThDisplay');
      var gSl = document.getElementById('micMonitorGainSlider');
      var gDisp = document.getElementById('micGainDisplay');
      if (wTh) {
        wTh.value = String(getWakeVoiceThreshold());
        if (wDisp) wDisp.textContent = wTh.value;
        wTh.addEventListener('input', function(){
          var v = parseInt(wTh.value, 10);
          if (isNaN(v)) v = WAKE_VOICE_THRESHOLD_DEFAULT;
          v = Math.max(1, Math.min(80, v));
          try { localStorage.setItem('g1_wake_voice_threshold', String(v)); } catch (_) {}
          if (wDisp) wDisp.textContent = String(v);
          updateParlaThresholdLine();
        });
      }
      if (gSl) {
        var gv = getParlaMonitorGain();
        gSl.value = String(gv);
        if (gDisp) gDisp.textContent = gv.toFixed(1);
        gSl.addEventListener('input', function(){
          var g = parseFloat(gSl.value);
          if (isNaN(g)) g = 1;
          g = Math.max(0.4, Math.min(4, g));
          try { localStorage.setItem('g1_mic_monitor_gain', String(g)); } catch (_) {}
          if (gDisp) gDisp.textContent = g.toFixed(1);
        });
      }
      updateParlaThresholdLine();
    })();
    function startParlaMicPreviewIfEligible() {
      var sec = document.getElementById('section-parla');
      if (!sec || !sec.classList.contains('active')) return;
      stopParlaMicPreview();
      var micEl = document.getElementById('mic');
      var micVal = micEl ? micEl.value : '';
      var wrap = document.getElementById('parlaPreviewMeterWrap');
      var msg = document.getElementById('parlaPreviewDisabledMsg');
      var isBrowserMic = micVal && micVal.indexOf('webmic_') === 0;
      if (!isBrowserMic) {
        if (wrap) wrap.style.display = 'none';
        if (msg) {
          msg.style.display = 'block';
          msg.innerHTML = 'Microfono attuale: Jetson o rete — il livello qui vale solo per il microfono <strong>Browser</strong> (telefono / DJI Mic).';
        }
        return;
      }
      if (wrap) wrap.style.display = '';
      if (msg) msg.style.display = 'none';
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
      navigator.mediaDevices.getUserMedia(buildAudioCaptureConstraints(micForBrowserCapture())).then(function(stream){
        parlaPreviewStream = stream;
        var Ctx = window.AudioContext || window.webkitAudioContext;
        parlaPreviewCtx = new Ctx();
        var src = parlaPreviewCtx.createMediaStreamSource(stream);
        parlaPreviewAnalyser = parlaPreviewCtx.createAnalyser();
        parlaPreviewAnalyser.fftSize = 512;
        parlaPreviewAnalyser.smoothingTimeConstant = 0.35;
        src.connect(parlaPreviewAnalyser);
        if (parlaPreviewCtx.resume) parlaPreviewCtx.resume();
        var buf = new Uint8Array(parlaPreviewAnalyser.frequencyBinCount);
        updateParlaThresholdLine();
        parlaPreviewTimer = setInterval(function(){
          if (!parlaPreviewAnalyser) return;
          var secEl = document.getElementById('section-parla');
          if (!secEl || !secEl.classList.contains('active')) return;
          if (isRecording) return;
          parlaPreviewAnalyser.getByteFrequencyData(buf);
          var peak = 0;
          for (var i = 0; i < buf.length; i++) if (buf[i] > peak) peak = buf[i];
          var th = getWakeVoiceThreshold();
          var gain = getParlaMonitorGain();
          var barW = Math.min(100, peak * gain * (100 / 255));
          var fill = document.getElementById('parlaPreviewBarFill');
          var st = document.getElementById('parlaPreviewStatus');
          var gate = document.getElementById('parlaPreviewGate');
          if (fill) fill.style.width = barW.toFixed(1) + '%';
          if (st) st.textContent = 'Picco: ' + peak + ' / 255 · soglia invio: ' + th;
          if (gate) {
            if (peak >= th) { gate.textContent = 'SOPRA SOGLIA'; gate.style.background = 'rgba(34,197,94,0.25)'; gate.style.color = '#4ade80'; }
            else { gate.textContent = 'Sotto soglia'; gate.style.background = '#27272a'; gate.style.color = '#71717a'; }
          }
          updateParlaThresholdLine();
        }, 55);
      }).catch(function(){
        var m = document.getElementById('parlaPreviewDisabledMsg');
        if (m) { m.style.display = 'block'; m.textContent = 'Microfono non disponibile: consenti l\\'accesso (Dispositivi) e riprova.'; }
      });
    }
    /** Dopo «Hey G1»: se nessun turno utile per questo tempo, torna solo ad ascoltare la wake word. */
    const WAKE_COMMAND_SILENCE_MS = 12000;
    /** Dopo startWakeRecorder: programma la prossima slice solo quando il round (risposta+TTS) è finito. */
    let scheduleNextWakeSliceIfListening = function(){};
    const WAKE_SLICE_MS = 6000;
    /** Coda riproduzione TTS: evita che due risposte MP3 si sovrappongano. */
    let ttsPlaybackQueue = [];
    let ttsPlaybackBusy = false;
    const TTS_BEFORE_PLAY_GAP_MS = 180;
    /**
     * Uscita browser: solo se esplicita (Soundboard «Riproduci su» o altoparlante Browser non-Predefinito).
     * Se null → niente setSinkId: il sistema sceglie (su Android spesso la cassa BT se è l’uscita media predefinita).
     */
    function resolveBrowserPlaybackSinkIdLikeSoundboard() {
      var sbOut = document.getElementById('sbOutput');
      if (sbOut && sbOut.value && sbOut.value !== 'default') return sbOut.value;
      var spk = document.getElementById('speaker');
      if (spk) {
        var v = spk.value;
        if (v && v.indexOf('browser_') === 0 && v !== 'browser_default')
          return v.replace(/^browser_/, '');
      }
      return null;
    }
    function applySinkThenPlay(audio, sinkId) {
      var p = Promise.resolve();
      if (sinkId && audio.setSinkId) {
        p = audio.setSinkId(sinkId).catch(function() { return Promise.resolve(); });
      }
      return p.then(function() { return audio.play(); });
    }
    function sbFireSlotRobotIfConfigured(sd) {
      if (!sd) return;
      var arm = (sd.robot_arm && String(sd.robot_arm).trim()) || '';
      var loco = (sd.robot_loco && String(sd.robot_loco).trim()) || '';
      if (!arm && !loco) arm = 'face_wave';
      var ip = '192.168.123.161';
      try {
        var ls = localStorage.getItem('g1_robot_ip');
        if (ls && ls.trim()) ip = ls.trim();
      } catch(_) {}
      if (arm) {
        fetch('/api/robot-action', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ action_id: arm, robot_ip: ip }) }).catch(function(){});
      }
      if (loco) {
        fetch('/api/robot-loco', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ command: loco, robot_ip: ip }) }).catch(function(){});
      }
    }
    function syncSbOutputFromSpeaker() {
      var spk = document.getElementById('speaker');
      var sbOut = document.getElementById('sbOutput');
      if (!spk || !sbOut) return;
      var v = spk.value;
      if (v && v.indexOf('browser_') === 0 && v !== 'browser_default') {
        var id = v.replace(/^browser_/, '');
        for (var i = 0; i < sbOut.options.length; i++) {
          if (sbOut.options[i].value === id) { sbOut.selectedIndex = i; return; }
        }
      }
      try { sbOut.value = 'default'; } catch(_){}
    }
    function syncSpeakerFromSbOutput() {
      var spk = document.getElementById('speaker');
      var sbOut = document.getElementById('sbOutput');
      if (!spk || !sbOut) return;
      var id = sbOut.value;
      if (!id || id === 'default') {
        lastSinkId = null;
        var cur = spk.value;
        if (cur && cur.indexOf('browser_') === 0 && cur !== 'browser_default') {
          for (var k = 0; k < spk.options.length; k++) {
            if (spk.options[k].value === 'browser_default') { spk.selectedIndex = k; return; }
          }
        }
        return;
      }
      lastSinkId = id;
      var want = 'browser_' + id;
      for (var j = 0; j < spk.options.length; j++) {
        if (spk.options[j].value === want) { spk.selectedIndex = j; return; }
      }
    }
    function enqueueTtsPlayback(b64, onPlaybackFullyEnded) {
      if (!b64 || String(b64).length < 30) {
        if (onPlaybackFullyEnded) onPlaybackFullyEnded();
        return;
      }
      ttsPlaybackQueue.push({ b64: String(b64), onEnded: onPlaybackFullyEnded });
      pumpTtsPlaybackQueue();
    }
    var _ttsGainValue = parseFloat(localStorage.getItem('g1_tts_gain') || '2.5');
    function getTtsGain() { return _ttsGainValue; }
    function setTtsGain(v) { _ttsGainValue = v; localStorage.setItem('g1_tts_gain', String(v)); }
    function pumpTtsPlaybackQueue() {
      if (ttsPlaybackBusy) return;
      if (ttsPlaybackQueue.length === 0) return;
      ttsPlaybackBusy = true;
      const item = ttsPlaybackQueue.shift();
      setTimeout(function() {
        try {
          var gain = getTtsGain();
          var ttsSink = resolveBrowserPlaybackSinkIdLikeSoundboard();
          if (gain > 1.05 && window.AudioContext) {
            var raw = atob(item.b64);
            var buf = new Uint8Array(raw.length);
            for (var i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
            var ctxOpts = {};
            if (ttsSink) { try { ctxOpts.sinkId = ttsSink; } catch(_){} }
            var ctx;
            try { ctx = new AudioContext(ctxOpts); } catch(_) { ctx = new AudioContext(); }
            ctx.decodeAudioData(buf.buffer, function(decoded) {
              var src = ctx.createBufferSource();
              src.buffer = decoded;
              var gn = ctx.createGain();
              gn.gain.value = gain;
              src.connect(gn);
              gn.connect(ctx.destination);
              src.onended = function() {
                ctx.close().catch(function(){});
                ttsPlaybackBusy = false;
                if (item.onEnded) item.onEnded();
                pumpTtsPlaybackQueue();
              };
              src.start(0);
            }, function() {
              _pumpTtsFallback(item, ttsSink);
            });
          } else {
            _pumpTtsFallback(item, ttsSink);
          }
        } catch(_) {
          ttsPlaybackBusy = false;
          if (item.onEnded) item.onEnded();
          pumpTtsPlaybackQueue();
        }
      }, TTS_BEFORE_PLAY_GAP_MS);
    }
    function _pumpTtsFallback(item, sinkId) {
      try {
        const audio = new Audio('data:audio/mpeg;base64,' + item.b64);
        audio.volume = 1.0;
        audio.onended = function() {
          ttsPlaybackBusy = false;
          if (item.onEnded) item.onEnded();
          pumpTtsPlaybackQueue();
        };
        audio.onerror = function() {
          ttsPlaybackBusy = false;
          if (item.onEnded) item.onEnded();
          pumpTtsPlaybackQueue();
        };
        applySinkThenPlay(audio, sinkId).catch(function() {
          ttsPlaybackBusy = false;
          if (item.onEnded) item.onEnded();
          pumpTtsPlaybackQueue();
        });
      } catch(_) {
        ttsPlaybackBusy = false;
        if (item.onEnded) item.onEnded();
        pumpTtsPlaybackQueue();
      }
    }
    function clearTtsPlaybackQueue() {
      ttsPlaybackQueue = [];
      ttsPlaybackBusy = false;
    }
    /** Stesso codec/bitrate del push-to-talk; MIME allineato a mediaRecorder.mimeType lato PTT. */
    function preferredRecorderMime() {
      return MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
    }
    /** Vincoli microfono: soppressione rumore browser + mono + AGC. Su Chromium si rafforzano i flag legacy se presenti. */
    function buildAudioCaptureConstraints(deviceIdExact) {
      const a = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      };
      if (deviceIdExact && String(deviceIdExact).length > 5) {
        a.deviceId = { exact: deviceIdExact };
      }
      try {
        var ua = typeof navigator !== 'undefined' ? (navigator.userAgent || '') : '';
        if (/Chrome|Chromium|Edg/i.test(ua) && !/OPR|Opera/i.test(ua)) {
          a.googEchoCancellation = true;
          a.googNoiseSuppression = true;
          a.googAutoGainControl = true;
          a.googHighpassFilter = true;
        }
      } catch(_){}
      return { audio: a };
    }
    /** Soglia uguale al controllo su /ws (audio troppo corto) e a sendAudio. */
    const WS_AUDIO_MIN_BYTES = 2000;
    /** Costruisce e invia lo stesso messaggio usato da Parla (PTT). */
    function sendAudioOverWs(b64, mime, opts) {
      opts = opts || {};
      const playOn = opts.playOn || 'browser';
      const msg = {
        type: 'audio',
        data: b64,
        play_on: playOn,
        skip_wake: opts.skipWake !== undefined ? opts.skipWake : true,
        format: mime || preferredRecorderMime()
      };
      if (playOn === 'server' && opts.deviceId != null) msg.device_id = opts.deviceId;
      ws.send(JSON.stringify(msg));
    }
    let thinkingInterval = null, thinkingAudioCtx = null;
    function startThinkingFeedback(showRecDebug){
      stopThinkingFeedback();
      if (showRecDebug !== false) {
        const el = document.getElementById('recDebug');
        if (el) { el.textContent = 'Sto elaborando (trascrizione + IA)…'; el.style.color = '#3b82f6'; }
      }
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        thinkingAudioCtx = new Ctx();
        thinkingInterval = setInterval(function(){
          if (!thinkingAudioCtx) return;
          const o = thinkingAudioCtx.createOscillator();
          const g = thinkingAudioCtx.createGain();
          o.frequency.value = 392;
          g.gain.setValueAtTime(0.04, thinkingAudioCtx.currentTime);
          g.gain.exponentialRampToValueAtTime(0.001, thinkingAudioCtx.currentTime + 0.11);
          o.connect(g); g.connect(thinkingAudioCtx.destination);
          o.start();
          o.stop(thinkingAudioCtx.currentTime + 0.11);
          thinkingAudioCtx.resume && thinkingAudioCtx.resume();
        }, 800);
      } catch(_){}
    }
    function stopThinkingFeedback(){
      if (thinkingInterval) { clearInterval(thinkingInterval); thinkingInterval = null; }
      if (thinkingAudioCtx) { try { thinkingAudioCtx.close(); } catch(_){} thinkingAudioCtx = null; }
    }
    var _listenHumCtx = null, _listenHumOsc = null, _listenHumGain = null;
    function startListeningHum(){
      stopListeningHum();
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        _listenHumCtx = new Ctx();
        if (_listenHumCtx.resume) _listenHumCtx.resume();
        _listenHumOsc = _listenHumCtx.createOscillator();
        _listenHumOsc.type = 'sine';
        _listenHumOsc.frequency.value = 440;
        _listenHumGain = _listenHumCtx.createGain();
        _listenHumGain.gain.value = 0.06;
        var lfo = _listenHumCtx.createOscillator();
        lfo.type = 'sine';
        lfo.frequency.value = 2.5;
        var lfoGain = _listenHumCtx.createGain();
        lfoGain.gain.value = 0.03;
        lfo.connect(lfoGain);
        lfoGain.connect(_listenHumGain.gain);
        lfo.start();
        _listenHumOsc.connect(_listenHumGain);
        _listenHumGain.connect(_listenHumCtx.destination);
        _listenHumOsc.start();
      } catch(_) { stopListeningHum(); }
    }
    function stopListeningHum(){
      if (_listenHumOsc) { try { _listenHumOsc.stop(); } catch(_){} _listenHumOsc = null; }
      if (_listenHumCtx) { try { _listenHumCtx.close(); } catch(_){} _listenHumCtx = null; }
      _listenHumGain = null;
    }
    function playStopChime(){
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        var ctx = new Ctx();
        if (ctx.resume) ctx.resume();
        var o = ctx.createOscillator(); var g = ctx.createGain();
        o.type = 'sine'; o.frequency.setValueAtTime(660, ctx.currentTime);
        o.frequency.exponentialRampToValueAtTime(330, ctx.currentTime + 0.2);
        g.gain.setValueAtTime(0.2, ctx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.2);
        o.connect(g); g.connect(ctx.destination);
        o.start(); o.stop(ctx.currentTime + 0.2);
        setTimeout(function(){ ctx.close().catch(function(){}); }, 300);
      } catch(_){}
    }
    function playWakeChime(){
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        const ctx = new Ctx();
        if (ctx.resume) ctx.resume();
        var vol = 0.35;
        var o1 = ctx.createOscillator(); var g1 = ctx.createGain();
        o1.type = 'sine'; o1.frequency.value = 660;
        g1.gain.setValueAtTime(vol, ctx.currentTime);
        g1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.15);
        o1.connect(g1); g1.connect(ctx.destination);
        o1.start(ctx.currentTime); o1.stop(ctx.currentTime + 0.15);
        var o2 = ctx.createOscillator(); var g2 = ctx.createGain();
        o2.type = 'sine'; o2.frequency.value = 880;
        g2.gain.setValueAtTime(0.01, ctx.currentTime);
        g2.gain.setValueAtTime(vol, ctx.currentTime + 0.12);
        g2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.35);
        o2.connect(g2); g2.connect(ctx.destination);
        o2.start(ctx.currentTime + 0.12); o2.stop(ctx.currentTime + 0.35);
        setTimeout(function(){ ctx.close().catch(function(){}); }, 500);
      } catch(_){}
    }
    function resetWakeCommandMode(){ /* no-op, one-shot mode only */ }
    function startWakeCommandIdleTimer(){ /* no-op */ }
    function stopWakeLevelMeter(){
      if (wakeLevelSampleInterval) { clearInterval(wakeLevelSampleInterval); wakeLevelSampleInterval = null; }
      if (wakeLevelCtx) { try { wakeLevelCtx.close(); } catch(_){} wakeLevelCtx = null; }
      wakeAnalyser = null;
      wakeSlicePeak = 0;
    }
    /** High-pass (taglia rimbombo/gravi) + compressore leggero → voce più stabile nel brusio; stream processato per MediaRecorder. */
    function startWakeSpeechEnhancer(){
      stopWakeLevelMeter();
      if (!wakeRawStream) return;
      try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        wakeLevelCtx = new Ctx();
        const src = wakeLevelCtx.createMediaStreamSource(wakeRawStream);
        const hp = wakeLevelCtx.createBiquadFilter();
        hp.type = 'highpass';
        hp.frequency.value = 100;
        hp.Q.value = 0.707;
        const comp = wakeLevelCtx.createDynamicsCompressor();
        comp.threshold.value = -28;
        comp.knee.value = 20;
        comp.ratio.value = 3.5;
        comp.attack.value = 0.003;
        comp.release.value = 0.12;
        wakeAnalyser = wakeLevelCtx.createAnalyser();
        wakeAnalyser.fftSize = 512;
        wakeAnalyser.smoothingTimeConstant = 0.35;
        const dest = wakeLevelCtx.createMediaStreamDestination();
        src.connect(hp);
        hp.connect(comp);
        comp.connect(wakeAnalyser);
        comp.connect(dest);
        wakeStream = dest.stream;
        wakeLevelCtx.resume && wakeLevelCtx.resume();
      } catch(_) {
        wakeStream = wakeRawStream;
        try {
          const Ctx = window.AudioContext || window.webkitAudioContext;
          wakeLevelCtx = new Ctx();
          wakeAnalyser = wakeLevelCtx.createAnalyser();
          wakeAnalyser.fftSize = 512;
          wakeAnalyser.smoothingTimeConstant = 0.35;
          wakeLevelCtx.createMediaStreamSource(wakeRawStream).connect(wakeAnalyser);
          wakeLevelCtx.resume && wakeLevelCtx.resume();
        } catch(__){ wakeAnalyser = null; }
      }
    }
    function onWakeResponseDone(){
      if (wakeResponseTimeout) { clearTimeout(wakeResponseTimeout); wakeResponseTimeout = null; }
      wakeAudioInFlight = false;
      if (wakeQueuedBlob && document.getElementById('wakeListenToggle') && document.getElementById('wakeListenToggle').checked) {
        const b = wakeQueuedBlob;
        wakeQueuedBlob = null;
        trySendWakeChunk(b);
      }
      scheduleNextWakeSliceIfListening();
    }
    let wakeResponseTimeout = null;
    function trySendWakeChunk(blob){
      if (!blob || blob.size < WS_AUDIO_MIN_BYTES) { scheduleNextWakeSliceIfListening(); return; }
      if (!document.getElementById('wakeListenToggle').checked) return;
      if (isRecording) { scheduleNextWakeSliceIfListening(); return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) { scheduleNextWakeSliceIfListening(); return; }
      if (wakeAudioInFlight) {
        wakeQueuedBlob = blob;
        return;
      }
      wakeAudioInFlight = true;
      wakeListenPending = true;
      if (wakeResponseTimeout) clearTimeout(wakeResponseTimeout);
      wakeResponseTimeout = setTimeout(function(){
        if (wakeAudioInFlight) {
          wakeLog('Timeout risposta server, riprovo...', '#ef4444');
          wakeAudioInFlight = false;
          wakeListenPending = false;
          stopThinkingFeedback();
          scheduleNextWakeSliceIfListening();
        }
      }, 45000);
      const fr = new FileReader();
      fr.onload = function(){
        const b64 = arrayBufferToBase64(fr.result);
        try {
          startThinkingFeedback();
          sendAudioOverWs(b64, wakeMimeType, { playOn: 'browser', skipWake: false });
        } catch(_){
          wakeListenPending = false;
          wakeAudioInFlight = false;
          stopThinkingFeedback();
          scheduleNextWakeSliceIfListening();
        }
      };
      fr.onerror = function(){
        wakeListenPending = false;
        wakeAudioInFlight = false;
        scheduleNextWakeSliceIfListening();
      };
      fr.readAsArrayBuffer(blob);
    }
    function stopWakeRecorder(){
      wakeListenActive = false;
      scheduleNextWakeSliceIfListening = function(){};
      stopWakeLevelMeter();
      clearTtsPlaybackQueue();
      stopWakeServerListener();
      try {
        if (wakeActiveMr && wakeActiveMr.state !== 'inactive') wakeActiveMr.stop();
      } catch(_){}
      wakeActiveMr = null;
      if (wakeRawStream) {
        try { wakeRawStream.getTracks().forEach(function(t){ try { t.stop(); } catch(_){} }); } catch(_){}
        wakeRawStream = null;
      }
      wakeStream = null;
      const st = document.getElementById('wakeListenStatus');
      if (st && !document.getElementById('wakeListenToggle').checked) st.textContent = 'Disattivato';
      resetWakeCommandMode();
      wakeQueuedBlob = null;
      wakeAudioInFlight = false;
      updateActiveMicIndicator();
    }
    var wsLevelMonitor = null;
    function startLevelMonitor(){
      if (wsLevelMonitor) return;
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      wsLevelMonitor = new WebSocket(proto + '//' + location.host + '/ws/mic-level');
      var bar = document.getElementById('levelBar');
      var lbl = document.getElementById('levelLabel');
      var dbg = document.getElementById('recDebug');
      wsLevelMonitor.onmessage = function(ev){
        try {
          var d = JSON.parse(ev.data);
          if (d.type === 'info') {
            if (dbg) { dbg.textContent = 'Mic Jetson: ' + d.name; dbg.style.color = '#14b8a6'; }
          } else if (d.type === 'level') {
            var pct = Math.max(0, Math.min(100, ((d.db + 60) / 60) * 100));
            if (bar) {
              bar.style.width = pct.toFixed(1) + '%';
              bar.style.background = d.peak > 0.5 ? '#ef4444' : d.rms > 0.02 ? '#22c55e' : d.rms > 0.005 ? '#eab308' : '#52525b';
            }
            if (lbl) lbl.textContent = d.rms > 0.01 ? 'Audio: ' + (pct|0) + '% (RMS ' + d.rms.toFixed(3) + ')' : 'Silenzio (RMS ' + d.rms.toFixed(4) + ')';
          } else if (d.type === 'error') {
            if (lbl) lbl.textContent = 'Errore mic: ' + (d.data || '?');
          }
        } catch(_){}
      };
      wsLevelMonitor.onclose = function(){ wsLevelMonitor = null; if (bar) bar.style.width = '0%'; if (lbl) lbl.textContent = 'Livello: --'; };
      wsLevelMonitor.onerror = function(){ try { wsLevelMonitor.close(); } catch(_){} wsLevelMonitor = null; };
    }
    function stopLevelMonitor(){
      if (wsLevelMonitor) { try { wsLevelMonitor.close(); } catch(_){} wsLevelMonitor = null; }
      var bar = document.getElementById('levelBar');
      var lbl = document.getElementById('levelLabel');
      if (bar) bar.style.width = '0%';
      if (lbl) lbl.textContent = 'Livello: --';
    }
    function stopWakeServerListener(){
      wakeServerMode = false;
      if (wsListenServer) {
        try { wsListenServer.close(); } catch(_){}
        wsListenServer = null;
      }
      stopLevelMonitor();
    }
    function startWakeServerListener(){
      wakeServerMode = true;
      var wsListenUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/listen';
      wsListenServer = new WebSocket(wsListenUrl);
      var st = document.getElementById('wakeListenStatus');
      wsListenServer.onopen = function(){
        if (st) st.textContent = 'In ascolto per «Hey G1» (mic Jetson)…';
        updateActiveMicIndicator();
        startLevelMonitor();
      };
      wsListenServer.onmessage = function(ev){
        try {
          var msg = JSON.parse(ev.data);
          if (msg.type === 'error') {
            if (st) st.textContent = 'Errore server: ' + (msg.data || '?');
            stopWakeServerListener();
            var el = document.getElementById('wakeListenToggle');
            if (el) el.checked = false;
            return;
          }
          if (msg.type === 'status') {
            if (st) st.textContent = msg.data || 'In ascolto…';
            return;
          }
          if (msg.type === 'response' && msg.data) {
            var d = msg.data;
            if (d.wake_miss) return;
            if (d.wake_ack) {
              playWakeChime();
              if (st) st.textContent = 'Dì Hey G1 + domanda';
              if (d.audio_base64) { enqueueTtsPlayback(d.audio_base64, function(){ if (st) st.textContent = 'In ascolto per «Hey G1» (mic Jetson)…'; }); }
              return;
            }
            if (d.response) {
              var resEl = document.getElementById('result');
              if (resEl) resEl.innerHTML = '<div class="ok"><strong>Tu:</strong> ' + (d.text||'').replace(/</g,'&lt;') + '<br><strong>G1:</strong> ' + (d.response||'').replace(/</g,'&lt;') + '</div>';
            }
            if (d.audio_base64) {
              enqueueTtsPlayback(d.audio_base64, function(){ if (st) st.textContent = 'In ascolto per «Hey G1» (mic Jetson)…'; });
            }
          }
        } catch(_){}
      };
      wsListenServer.onclose = function(){
        wakeServerMode = false;
        wsListenServer = null;
        stopLevelMonitor();
        var el = document.getElementById('wakeListenToggle');
        if (el && el.checked) {
          if (st) st.textContent = 'Connessione persa. Riattiva per riprovare.';
          el.checked = false;
        }
        updateActiveMicIndicator();
      };
      wsListenServer.onerror = function(){
        if (st) st.textContent = 'Errore connessione WebSocket listen.';
        stopWakeServerListener();
        var el = document.getElementById('wakeListenToggle');
        if (el) el.checked = false;
        updateActiveMicIndicator();
      };
    }
    async function startWakeRecorder(){
      const el = document.getElementById('wakeListenToggle');
      if (!el || !el.checked) return;
      if (isRecording) return;
      stopWakeRecorder();
      const micId = document.getElementById('mic') ? document.getElementById('mic').value : '';
      if (micId && String(micId).indexOf('local_') === 0) {
        startWakeServerListener();
        return;
      }
      if (!navigator.mediaDevices) {
        const st = document.getElementById('wakeListenStatus');
        if (st) st.textContent = 'MediaDevices non disponibile (serve HTTPS).';
        el.checked = false;
        return;
      }
      try {
        stopParlaMicPreview();
        wakeRawStream = await navigator.mediaDevices.getUserMedia(buildAudioCaptureConstraints(micForBrowserCapture()));
        startWakeSpeechEnhancer();
      } catch(e) {
        const st = document.getElementById('wakeListenStatus');
        if (st) st.textContent = 'Microfono non disponibile per ascolto continuo.';
        el.checked = false;
        return;
      }
      await new Promise(function(r){ setTimeout(r, 150); });
      wakeMimeType = preferredRecorderMime();
      wakeListenActive = true;
      scheduleNextWakeSliceIfListening = function(){
        if (!wakeListenActive) return;
        const tg = document.getElementById('wakeListenToggle');
        if (!tg || !tg.checked) return;
        if (isRecording) return;
        setTimeout(runWakeSlice, 120);
      };
      function runWakeSlice(){
        if (!wakeListenActive || !document.getElementById('wakeListenToggle').checked) return;
        if (isRecording) { setTimeout(runWakeSlice, 350); return; }
        if (!wakeStream) return;
        const mr = new MediaRecorder(wakeStream, { mimeType: wakeMimeType, audioBitsPerSecond: 128000 });
        wakeActiveMr = mr;
        const ch = [];
        wakeSlicePeak = 0;
        let voiceDetected = false, lastVoiceTs = 0;
        const SILENCE_AFTER_VOICE_MS = 1500;
        let sliceInterval = null;
        function stopMrEarly(){
          if (sliceInterval) { clearInterval(sliceInterval); sliceInterval = null; }
          try { if (mr.state !== 'inactive') { if (typeof mr.requestData === 'function') mr.requestData(); mr.stop(); } } catch(_){}
        }
        if (wakeAnalyser) {
          if (wakeLevelSampleInterval) clearInterval(wakeLevelSampleInterval);
          sliceInterval = setInterval(function(){
            if (!wakeAnalyser) return;
            const buf = new Uint8Array(wakeAnalyser.frequencyBinCount);
            wakeAnalyser.getByteFrequencyData(buf);
            let s = 0;
            for (let i = 0; i < buf.length; i++) if (buf[i] > s) s = buf[i];
            if (s > wakeSlicePeak) wakeSlicePeak = s;
            const th = getWakeVoiceThreshold();
            if (s >= th) { voiceDetected = true; lastVoiceTs = Date.now(); }
            else if (voiceDetected && lastVoiceTs > 0 && (Date.now() - lastVoiceTs >= SILENCE_AFTER_VOICE_MS)) {
              stopMrEarly();
            }
          }, 50);
          wakeLevelSampleInterval = sliceInterval;
        }
        mr.ondataavailable = function(ev){ if (ev.data && ev.data.size) ch.push(ev.data); };
        mr.onstop = function(){
          wakeActiveMr = null;
          if (sliceInterval) { clearInterval(sliceInterval); }
          sliceInterval = null;
          wakeLevelSampleInterval = null;
          if (!wakeListenActive) return;
          const blob = new Blob(ch, { type: wakeMimeType });
          const voiced = !wakeAnalyser || wakeSlicePeak >= getWakeVoiceThreshold();
          if (blob.size >= WS_AUDIO_MIN_BYTES && voiced) trySendWakeChunk(blob);
          else scheduleNextWakeSliceIfListening();
        };
        mr.start();
        setTimeout(function(){ stopMrEarly(); }, WAKE_SLICE_MS);
      }
      runWakeSlice();
      const st = document.getElementById('wakeListenStatus');
      if (st) st.textContent = 'In ascolto \u2014 di \u00abHey G1\u00bb + comando';
    }
    const wakeListenToggleEl = document.getElementById('wakeListenToggle');
    if (wakeListenToggleEl) {
      wakeListenToggleEl.onchange = function(){
        if (wakeListenToggleEl.checked) {
          const st = document.getElementById('wakeListenStatus');
          if (st) st.textContent = 'Avvio ascolto…';
          startWakeRecorder();
        } else {
          stopWakeRecorder();
          resetWakeCommandMode();
          const st = document.getElementById('wakeListenStatus');
          if (st) st.textContent = 'Disattivato';
          setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 300);
        }
        var wtl = document.getElementById('wakeToggleLabel');
        if (wtl) wtl.textContent = wakeListenToggleEl.checked ? 'ON' : 'OFF';
      };
    }

    const isLocalhost = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
    const isSecure = location.protocol === 'https:';
    const isMobile = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
    if (!isLocalhost && !isSecure) {
      document.getElementById('secureContextWarn').style.display = 'block';
      const shl = document.getElementById('secureHttpsLink');
      if (shl) {
        const u = 'https://' + location.hostname + ':8081' + location.pathname + location.search;
        shl.href = u;
        shl.textContent = u;
      }
      document.getElementById('secureWarnMobile').style.display = isMobile ? 'block' : 'none';
      document.getElementById('secureWarnDesktop').style.display = isMobile ? 'none' : 'block';
      document.getElementById('hintAccess').style.display = 'none';
      document.getElementById('allowWrap').style.display = 'none';
      document.getElementById('devicesWrap').style.display = 'none';
      document.getElementById('secureWarnMore').onclick = (e)=>{ e.preventDefault(); const d=document.getElementById('secureWarnDetails'); d.style.display = d.style.display==='none' ? 'block' : 'none'; };
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


    function wakeLog(msg, color) {
      var el = document.getElementById('wakeDebugLog');
      if (!el) return;
      el.style.display = '';
      var d = document.createElement('div');
      d.style.color = color || '#71717a';
      var t = new Date(); var ts = t.toLocaleTimeString('it-IT',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      d.textContent = ts + ' ' + msg;
      el.appendChild(d);
      if (el.children.length > 20) el.removeChild(el.firstChild);
      el.scrollTop = el.scrollHeight;
    }
    function onWsPipelineMessage(e){
      let d;
      try { d = JSON.parse(e.data); } catch(_) { document.getElementById('result').innerHTML = '<div class="warn">Errore risposta server</div>'; return; }
      if(d.type==='response'){
        stopThinkingFeedback();
        const r = d.data;
        let deferWakeDone = false;
        try {
          if (wakeListenPending) {
            wakeListenPending = false;
            if (r.wake_miss) {
              var sttTxt = String(r.text||'').trim();
              wakeLog(sttTxt ? 'STT: "'+sttTxt+'" \u2192 miss (no wake word)' : 'silenzio / no speech', '#71717a');
              btn.disabled = false;
              return;
            }
            if (r.wake_ack) {
              wakeLog('Solo "Hey G1". Dì Hey G1 + domanda.', '#f59e0b');
              playWakeChime();
              btn.disabled = false;
              var ackB64 = r.audio_base64 && String(r.audio_base64).length > 50 ? r.audio_base64 : null;
              if (ackB64 && lastPlayOn === 'browser') {
                deferWakeDone = true;
                document.getElementById('result').innerHTML = '<div class="warn">'+(r.response||'Dì Hey G1 seguito dalla domanda.')+'</div>';
                enqueueTtsPlayback(ackB64, onWakeResponseDone);
              } else {
                document.getElementById('result').innerHTML = '<div class="warn">'+(r.response||'Dì Hey G1 seguito dalla domanda.')+'</div>';
                onWakeResponseDone();
              }
              return;
            }
            if (!r.response && r.message) {
              wakeLog('msg: '+r.message, '#f59e0b');
              const wst = document.getElementById('wakeListenStatus');
              if (wst) wst.textContent = 'In ascolto \u2014 di \u00abHey G1\u00bb + comando';
              document.getElementById('result').innerHTML = '<div class="warn">'+r.message+'</div>';
              btn.disabled = false;
              return;
            }
            if (r.text) wakeLog('CMD: "'+String(r.text||'')+'" \u2192 risposta', '#14b8a6');
          }
          btn.disabled = false;
          recordingServerJetson = false;
          document.getElementById('recDebug').textContent = r.text ? '' : (r.message || '');
          document.getElementById('recDebug').style.color = r.message ? '#f59e0b' : '#71717a';
          const msg = r.message ? '<div class="warn">'+r.message+'</div>' : '';
          const dur = r.duration_ms ? ' <span style="color:#71717a;font-size:12px;">('+r.duration_ms+' ms)</span>' : '';
          document.getElementById('result').innerHTML = msg + '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+dur+'</div>';
          const hasTts = lastPlayOn === 'browser' && r.audio_base64 && String(r.audio_base64).length > 50;
          if (hasTts) {
            deferWakeDone = true;
            enqueueTtsPlayback(r.audio_base64, onWakeResponseDone);
          }
          wakeLog('Comando completato, torno in ascolto wake', '#71717a');
          const wst = document.getElementById('wakeListenStatus');
          if (wst && document.getElementById('wakeListenToggle') && document.getElementById('wakeListenToggle').checked) wst.textContent = 'In ascolto \u2014 di \u00abHey G1\u00bb + comando';
        } finally {
          if (!deferWakeDone) onWakeResponseDone();
        }
      } else if(d.type==='wake_chime'){
        playWakeChime();
        wakeLog('Hey G1 rilevato, elaboro...', '#22c55e');
      } else if(d.type==='error'){
        stopThinkingFeedback();
        clearTtsPlaybackQueue();
        wakeAudioInFlight = false;
        wakeQueuedBlob = null;
        btn.disabled = false;
        recordingServerJetson = false;
        document.getElementById('result').innerHTML = '<div class="warn">Errore: '+ (d.data || '')+'</div>';
      } else if(d.type==='play' && d.data){
        enqueueTtsPlayback(d.data, null);
      }
    }
    function connect(){
      ws = new WebSocket(wsUrl);
      ws.onopen = () => {
        document.getElementById('result').innerHTML = '<div class="ok">Connesso al server. Tieni premuto e parla.</div>';
        document.getElementById('recDebug').textContent = 'WebSocket OK';
      };
      ws.onclose = () => { setTimeout(connect, 3000); document.getElementById('result').innerHTML = '<div class="warn">Riconnessione...</div>'; document.getElementById('recDebug').textContent = 'WebSocket disconnesso'; };
      ws.onmessage = onWsPipelineMessage;
    }
    function ensureParlaWs(){
      return new Promise(function(resolve, reject){
        if (wsParla && wsParla.readyState === WebSocket.OPEN) return resolve();
        try {
          wsParla = new WebSocket(wsParlaUrl);
          wsParla.onmessage = onWsPipelineMessage;
          wsParla.onerror = function(){ reject(new Error('ws parla')); };
          const to = setTimeout(function(){ reject(new Error('timeout ws parla')); }, 10000);
          wsParla.onopen = function(){ clearTimeout(to); resolve(); };
        } catch(err) { reject(err); }
      });
    }
    async function startRecServerPtt(){
      if (isRecording) return;
      wakeListenPending = false;
      stopWakeRecorder();
      const spkVal = document.getElementById('speaker') ? document.getElementById('speaker').value : '';
      const ttsEl = document.getElementById('ttsPlayDest');
      const wantServerTts = ttsEl && ttsEl.value === 'server';
      if (wantServerTts) {
        lastPlayOn = 'server';
        lastSinkId = null;
      } else {
        lastPlayOn = 'browser';
        lastSinkId = (spkVal && spkVal.startsWith('browser_') && spkVal !== 'browser_default') ? spkVal.replace('browser_','') : null;
        syncSbOutputFromSpeaker();
      }
      try {
        await ensureParlaWs();
      } catch(e) {
        document.getElementById('result').innerHTML = '<div class="warn">Connessione WebSocket «Parla robot» fallita. Verifica microfono locale in <a href="/" style="color:#14b8a6">setup</a> e ricarica.</div>';
        return;
      }
      recordingServerJetson = true;
      isRecording = true;
      pendingStop = false;
      btn.classList.add('recording');
      document.getElementById('levelBar').style.width = '60%';
      document.getElementById('levelBar').style.background = '#14b8a6';
      document.getElementById('levelLabel').textContent = 'Ingresso: Jetson USB (mic sul robot)';
      document.getElementById('recDebug').textContent = 'Registrazione dal microfono sul robot…';
      document.getElementById('recDebug').style.color = '#22c55e';
      updateActiveMicIndicator();
      recStartTime = Date.now();
      var pulseTick = 0;
      recDurationInterval = setInterval(function(){
        const s = ((Date.now()-recStartTime)/1000).toFixed(1);
        document.getElementById('recDebug').textContent = 'Registrazione Jetson: '+s+' sec';
        pulseTick++;
        var w = 40 + 30 * Math.abs(Math.sin(pulseTick * 0.25));
        document.getElementById('levelBar').style.width = w.toFixed(0)+'%';
      }, 150);
      recTimeout = setTimeout(function(){ stopRec(); }, MAX_REC_SEC * 1000);
      try {
        wsParla.send(JSON.stringify({type:'start'}));
      } catch(err) {
        recordingServerJetson = false;
        isRecording = false;
        btn.classList.remove('recording');
        clearAllIntervals();
        document.getElementById('result').innerHTML = '<div class="warn">Invio start fallito.</div>';
      }
    }
    connect();
    (function loadServerTtsConfig(){
      fetch('/api/config').then(function(r){ return r.json(); }).then(function(cfg){
        var sp = cfg && cfg.speaker;
        var hasLocalSpk = sp && sp.type === 'local' && (sp.device_id !== undefined && sp.device_id !== null && sp.device_id !== '');
        if (hasLocalSpk) {
          serverTtsDeviceId = parseInt(sp.device_id, 10);
          var tts = document.getElementById('ttsPlayDest');
          if (tts && !isNaN(serverTtsDeviceId)) tts.value = 'server';
          var sb = document.getElementById('sbPlayDest');
          if (sb && !isNaN(serverTtsDeviceId)) sb.value = 'server';
        } else {
          /* Senza cassa Jetson salvata in setup, la play API server fallisce: default su browser. */
          var sb0 = document.getElementById('sbPlayDest');
          if (sb0) { sb0.value = 'browser'; }
        }
        var wrap = document.getElementById('ttsOutputWrap');
        if (wrap) wrap.style.display = 'block';
        updateSbBrowserRowVisibility();
      }).catch(function(){});
    })();

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
      let serverData = { microphones: [], speakers: [], hardware_probe: null };
      try {
        const r = await fetch('/api/devices?all=1');
        if (r.ok) serverData = await r.json();
        _serverDevicesCache.microphones = serverData.microphones || [];
        _serverDevicesCache.speakers = serverData.speakers || [];
        _serverDevicesCache.hardware_probe = serverData.hardware_probe || null;
        updateHwProbe(serverData.hardware_probe);
      } catch(_) { updateHwProbe(null); }
      try {
        const devs = await navigator.mediaDevices.enumerateDevices();
        const mics = devs.filter(function(d){ return d.kind === 'audioinput'; });
        const spks = devs.filter(function(d){ return d.kind === 'audiooutput'; });
        const sm = (serverData.microphones || []).filter(function(m){
          return m && (m.type === 'local' || (m.value && String(m.value).indexOf('local_') === 0));
        });
        const netm = (serverData.microphones || []).filter(function(m){
          return m && m.type === 'network' && m.value && m.value !== 'web_wait';
        });
        let micHtml = '';
        function attrEsc(v){ return String(v||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }
        if (sm.length) {
          micHtml += '<optgroup label="Jetson - server (PortAudio)">';
          sm.forEach(function(m){ micHtml += '<option value="'+attrEsc(m.value)+'">'+escapeHtmlDevices(m.name)+'</option>'; });
          micHtml += '</optgroup>';
        }
        if (netm.length) {
          micHtml += '<optgroup label="Client rete">';
          netm.forEach(function(m){ micHtml += '<option value="'+attrEsc(m.value)+'">'+escapeHtmlDevices(m.name)+'</option>'; });
          micHtml += '</optgroup>';
        }
        micHtml += '<optgroup label="Browser - questo dispositivo">';
        if (mics.length === 0) micHtml += '<option value="">Nessun microfono browser</option>';
        else mics.forEach(function(m,i){
          const lab = m.label || ('Microfono '+(i+1));
          micHtml += '<option value="webmic_'+encodeURIComponent(m.deviceId)+'">'+escapeHtmlDevices(lab)+'</option>';
        });
        micHtml += '</optgroup>';
        micSel.innerHTML = micHtml;
        micSel.onchange = function(){
          updateActiveMicIndicator();
          setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 80);
        };

        const ss = (serverData.speakers || []).filter(function(s){
          return s && (s.type === 'local' || (s.value && String(s.value).indexOf('local_') === 0));
        });
        const nets = (serverData.speakers || []).filter(function(s){
          return s && s.type === 'network' && s.value && s.value !== 'web_wait';
        });
        spkSel.innerHTML = '';
        function optLabel(s, fb){ var n = (s && s.name) ? String(s.name) : ''; return n.trim() ? n : (fb || (s && s.value) || '?'); }
        if (ss.length) {
          const og = document.createElement('optgroup');
          og.label = 'Jetson - server (cassa robot)';
          ss.forEach(function(s){ og.appendChild(new Option(optLabel(s, 'Cassa Jetson'), s.value)); });
          spkSel.appendChild(og);
        }
        if (nets.length) {
          const og2 = document.createElement('optgroup');
          og2.label = 'Client rete';
          nets.forEach(function(s){ og2.appendChild(new Option(optLabel(s, 'Client rete'), s.value)); });
          spkSel.appendChild(og2);
        }
        const ogB = document.createElement('optgroup');
        ogB.label = 'Browser - telefono/PC';
        if (spks.length === 0) ogB.appendChild(new Option('Predefinito', 'browser_default'));
        else spks.forEach(function(s,i){ ogB.appendChild(new Option(s.label || ('Output '+(i+1)), 'browser_'+s.deviceId)); });
        spkSel.appendChild(ogB);
        spkSel.onchange = function(){
          const v = spkSel.value;
          lastSinkId = (v && v.indexOf('browser_') === 0 && v !== 'browser_default') ? v.replace(/^browser_/, '') : null;
          syncSbOutputFromSpeaker();
          updateActiveMicIndicator();
        };
        const sbOut = document.getElementById('sbOutput');
        if (sbOut) {
          sbOut.innerHTML = '<option value="default">Predefinito</option>' + spks.map(function(s,i){
            return '<option value="'+s.deviceId+'">'+escapeHtmlDevices(s.label || ('Output '+(i+1)))+'</option>';
          }).join('');
          sbOut.onchange = function(){ syncSpeakerFromSbOutput(); updateActiveMicIndicator(); };
        }
        const nJet = sm.length + ss.length;
        statusEl.textContent = nJet ? ('Jetson: '+sm.length+' mic, '+ss.length+' uscite · Browser: '+mics.length+'/'+spks.length) : ('Browser: '+mics.length+' mic · Server: nessun locale (controlla PortAudio sulla Jetson)');
        fetch('/api/config').then(function(r){ return r.json(); }).then(function(cfg){
          if (!cfg || !micSel) return;
          if (cfg.microphone && cfg.microphone.value) {
            var mv = cfg.microphone.value;
            if (cfg.microphone.type === 'network' && mv && mv !== 'web_wait' && mv.indexOf('local_') !== 0 && mv.indexOf('net_') !== 0) {
              var found = false;
              for (var i = 0; i < micSel.options.length; i++) {
                var o = micSel.options[i];
                if (o.value.indexOf('webmic_') === 0 && decodeURIComponent(o.value.slice(7)) === mv) { micSel.selectedIndex = i; found = true; break; }
              }
              if (!found) { try { micSel.value = 'webmic_'+encodeURIComponent(mv); } catch(_){} }
            } else if (mv) { try { micSel.value = mv; } catch(_){} }
          }
          if (cfg.speaker && cfg.speaker.value) {
            try { spkSel.value = cfg.speaker.value; } catch(_){}
          }
          var vsp = spkSel.value;
          lastSinkId = (vsp && vsp.indexOf('browser_') === 0 && vsp !== 'browser_default') ? vsp.replace(/^browser_/, '') : null;
          syncSbOutputFromSpeaker();
          updateActiveMicIndicator();
        }).catch(function(){ updateActiveMicIndicator(); });
      } catch(e) {
        micSel.innerHTML = '<option value="">Errore: '+escapeHtmlDevices(e.message)+'</option>';
        spkSel.innerHTML = '<option value="browser_default">Riproduci qui</option>';
        const sbOut = document.getElementById('sbOutput');
        if (sbOut) {
          sbOut.innerHTML = '<option value="default">Predefinito</option>';
          sbOut.onchange = function(){ syncSpeakerFromSbOutput(); updateActiveMicIndicator(); };
        }
        statusEl.textContent = 'Errore lettura dispositivi.';
      }
    }

    document.getElementById('btnAllow').onclick = () => { requestAndLoadDevices(); };
    (function bindDevicesPanel(){
      const dlf = document.getElementById('devicesLoadFull');
      if (dlf) dlf.onclick = function(){
        const pre = document.getElementById('devicesFullDump');
        const st = document.getElementById('devicesSaveStatus');
        if (pre) { pre.style.display = 'block'; pre.textContent = 'Caricamento…'; }
        fetch('/api/devices-detailed').then(function(r){ return r.json(); }).then(function(d){
          if (pre) pre.textContent = JSON.stringify(d, null, 2);
          if (st) st.textContent = d.ok ? ('OK: '+d.portaudio_count+' device PortAudio') : '';
        }).catch(function(e){
          if (pre) pre.textContent = 'Errore: '+(e.message||String(e));
        });
      };
      const dr = document.getElementById('devicesRefresh');
      const ds = document.getElementById('devicesSave');
      if (dr) dr.onclick = function(){ loadDevices(); };
      if (ds) ds.onclick = function(){
        const st = document.getElementById('devicesSaveStatus');
        const micVal = document.getElementById('mic').value;
        const spkVal = document.getElementById('speaker').value;
        const body = { microphone: buildMicCfgFromSelect(micVal), speaker: buildSpkCfgFromSelect(spkVal) };
        if (st) st.textContent = 'Salvataggio…';
        fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
          .then(function(r){ if (!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
          .then(function(){
            if (st) st.textContent = 'Salvato.';
            fetch('/api/config').then(function(r){ return r.json(); }).then(function(cfg){
              var sp = cfg && cfg.speaker;
              if (sp && sp.type === 'local' && sp.device_id != null && sp.device_id !== '') {
                serverTtsDeviceId = parseInt(sp.device_id, 10);
                var tts = document.getElementById('ttsPlayDest');
                if (tts) tts.value = 'server';
                var sb = document.getElementById('sbPlayDest');
                if (sb) sb.value = 'server';
              }
              if (typeof updateSbBrowserRowVisibility === 'function') updateSbBrowserRowVisibility();
            }).catch(function(){});
          })
          .catch(function(e){ if (st) st.textContent = 'Errore: '+(e.message||String(e)); });
      };
    })();
    (function(){
      var sl = document.getElementById('ttsGainSlider');
      var lb = document.getElementById('ttsGainLabel');
      var sl2 = document.getElementById('parlaGainSlider');
      var lb2 = document.getElementById('parlaGainLabel');
      function syncAll(v){
        setTtsGain(v);
        if (sl) { sl.value = v; }
        if (lb) { lb.textContent = v.toFixed(1) + 'x'; }
        if (sl2) { sl2.value = v; }
        if (lb2) { lb2.textContent = v.toFixed(1) + 'x'; }
      }
      syncAll(getTtsGain());
      if (sl) sl.addEventListener('input', function(){ syncAll(parseFloat(sl.value)); });
      if (sl2) sl2.addEventListener('input', function(){ syncAll(parseFloat(sl2.value)); });
    })();
    if (navigator.mediaDevices) {
      if (isLocalhost) {
        loadDevices();
        requestAndLoadDevices();
      } else if (isSecure) {
        loadDevices();
        requestAndLoadDevices();
      } else {
        loadDevices();
      }
    } else {
      loadDevices();
    }

    let knowledgeEntries = {};
    function renderKnowledge(){
      const el = document.getElementById('knowledgeList');
      if (!el) return;
      el.innerHTML = Object.entries(knowledgeEntries).map(([k,v])=>'<div style="display:flex;align-items:center;gap:6px;margin:4px 0;font-size:12px;"><span style="color:#9ca3af;min-width:120px;">'+k.replace(/\u003c/g,'&lt;').replace(/&/g,'&amp;')+'</span><span style="color:#e8eaed;">'+(v.substring(0,40)+(v.length>40?'...':'')).replace(/\u003c/g,'&lt;').replace(/&/g,'&amp;')+'</span><button type="button" data-key="'+encodeURIComponent(k)+'" class="knowledgeDel" style="margin-left:auto;padding:2px 8px;background:rgba(239,68,68,0.3);color:#fca5a5;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Elimina</button></div>').join('') || '<span style="color:#71717a;">(vuoto)</span>';
      el.querySelectorAll('.knowledgeDel').forEach(btn=>{ btn.onclick=()=>{ delete knowledgeEntries[decodeURIComponent(btn.dataset.key||'')]; renderKnowledge(); }; });
    }
    fetch('/api/knowledge').then(r=>r.json()).then(d=>{ knowledgeEntries = d.entries || {}; renderKnowledge(); }).catch(()=>{});
    document.getElementById('knowledgeAdd').onclick = ()=>{
      const p = (document.getElementById('knowledgePattern').value||'').trim();
      const r = (document.getElementById('knowledgeResponse').value||'').trim();
      if (p && r) { knowledgeEntries[p] = r; document.getElementById('knowledgePattern').value=''; document.getElementById('knowledgeResponse').value=''; renderKnowledge(); }
    };
    document.getElementById('knowledgeSave').onclick = ()=>{
      fetch('/api/knowledge/save', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({entries:knowledgeEntries})})
        .then(r=>r.json()).then(d=>{ if(d.ok) document.getElementById('knowledgeSave').textContent='Salvato!'; else alert(d.error||'Errore'); })
        .catch(e=>alert('Errore: '+e.message));
    };

    let soundboardSlots = [];
    let sbTextMax = 280;
    let sbEditIdx = -1, sbEditAudio = null, sbEditFmt = '', sbEditAudioRaw = null;
    let sbEditAudioClean = null, sbEditFmtClean = 'mp3';
    function sbMimeForFmt(fmt){
      const f = (fmt||'webm').toLowerCase();
      if(f==='mp3') return 'audio/mpeg';
      if(f==='wav') return 'audio/wav';
      return 'audio/'+f;
    }
    function updateSbBrowserRowVisibility(){
      const destEl = document.getElementById('sbPlayDest');
      const dest = (destEl && destEl.value) || 'server';
      const show = dest === 'browser';
      ['sbBrowserSinkLabel','sbOutput','sbOutputRefresh'].forEach(function(id){
        const el = document.getElementById(id);
        if (el) el.style.display = show ? '' : 'none';
      });
    }
    function sbPlaySlot(s, slotIndex){
      const destEl = document.getElementById('sbPlayDest');
      const dest = (destEl && destEl.value) || 'server';
      if (dest === 'server' && typeof slotIndex === 'number') {
        fetch('/api/soundboard-play-local', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slot: slotIndex })
        }).then(async function(r){
          const d = await r.json().catch(function(){ return {}; });
          if (!r.ok) {
            const msg = (d.detail && (typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail))) || d.message || ('HTTP '+r.status);
            alert('Cassa robot: ' + msg);
          }
        }).catch(function(e){ alert('Cassa robot: ' + (e.message || String(e))); });
        return;
      }
      function playFromData(sd){
        let b64 = null, fmt = 'mp3';
        if(sd.audio_base64_clean && sd.audio_base64_clean.length>50){ b64 = sd.audio_base64_clean; fmt = sd.format_clean||'mp3'; }
        else if(sd.audio_base64 && sd.audio_base64.length>50){ b64 = sd.audio_base64; fmt = sd.format||'mp3'; }
        if(!b64) return;
        sbFireSlotRobotIfConfigured(sd);
        const a = new Audio('data:'+sbMimeForFmt(fmt)+';base64,'+b64);
        const sinkId = resolveBrowserPlaybackSinkIdLikeSoundboard();
        applySinkThenPlay(a, sinkId).catch(function(){});
      }
      if ((s.audio_base64 && s.audio_base64.length>50) || (s.audio_base64_clean && s.audio_base64_clean.length>50)) {
        var arm0 = (s.robot_arm && String(s.robot_arm).trim()) || '';
        var loco0 = (s.robot_loco && String(s.robot_loco).trim()) || '';
        if ((!arm0 && !loco0) && typeof slotIndex === 'number') {
          fetch('/api/soundboard-slot/'+slotIndex).then(function(r){
            if (!r.ok) return Promise.resolve(s);
            return r.json();
          }).then(function(full){
            var merged = Object.assign({}, s, {
              robot_arm: (full && full.robot_arm) ? String(full.robot_arm) : '',
              robot_loco: (full && full.robot_loco) ? String(full.robot_loco) : ''
            });
            playFromData(merged);
          }).catch(function(){ playFromData(s); });
          return;
        }
        playFromData(s);
        return;
      }
      if (typeof slotIndex !== 'number') return;
      fetch('/api/soundboard-slot/'+slotIndex).then(function(r){
        if (!r.ok) return Promise.reject(new Error('HTTP '+r.status));
        return r.json();
      }).then(playFromData).catch(function(e){ alert('Soundboard browser: '+(e.message||String(e))); });
    }
    const sbDefaultIcons = ['🎤','🔊','📢','🎵','🎶','🎧','🎭','🚀','⭐','💡','🤝','☕','🎬','📷','🚪','🎁','✨','🏢','👋','🙏'];
    function sbIconAt(i){ return sbDefaultIcons[i % sbDefaultIcons.length]; }
    function updateSbCharCount(){
      const ta = document.getElementById('sbModalText');
      const n = (ta && ta.value) ? ta.value.length : 0;
      const el = document.getElementById('sbModalCharCount');
      if(el) el.textContent = n;
    }
    function renderSoundboard(){
      const grid = document.getElementById('soundboardGrid');
      if (!grid) return;
      grid.innerHTML = soundboardSlots.map((s,i)=>{
        const hasR = (typeof s.has_robot === 'boolean') ? s.has_robot : !!(s.audio_base64 && s.audio_base64.length > 50);
        const hasN = (typeof s.has_clean === 'boolean') ? s.has_clean : !!(s.audio_base64_clean && s.audio_base64_clean.length > 50);
        const hasAudio = hasR || hasN;
        const border = hasAudio ? '2px solid #14b8a6' : '1px solid rgba(255,255,255,0.08)';
        const bg = hasAudio ? 'rgba(20,184,166,0.08)' : 'rgba(255,255,255,0.05)';
        let badgeTitle = 'Vuoto', badgeHtml = '&#8212;';
        if(hasAudio){ badgeTitle = 'Audio'; badgeHtml = '&#9654;'; }
        const badge = hasAudio ? '<span style="position:absolute;top:4px;right:4px;font-size:9px;font-weight:700;color:#14b8a6;" title="'+badgeTitle+'">'+badgeHtml+'</span>' : '<span style="position:absolute;top:4px;right:4px;font-size:10px;color:#71717a;" title="Vuoto">&#8212;</span>';
        const label = (s.text||'Comando '+(i+1)).replace(/\u003c/g,'&lt;').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
        return '<div id="sb'+i+'" role="button" tabindex="0" aria-label="Riproduci slot '+(i+1)+'" style="position:relative;display:flex;flex-direction:column;align-items:center;padding:8px;background:'+bg+';border-radius:10px;cursor:pointer;border:'+border+';min-height:88px;touch-action:manipulation;-webkit-tap-highlight-color:rgba(20,184,166,0.2);">'+badge+'<span style="font-size:22px;margin-bottom:4px;pointer-events:none;">'+(s.icon||sbIconAt(i))+'</span><span class="sb-slot-text" style="font-size:10px;color:#9ca3af;text-align:center;max-width:100%;pointer-events:none;">'+label+'</span><button type="button" onclick="event.stopPropagation();editSoundboard('+i+')" style="margin-top:4px;padding:8px 10px;font-size:10px;background:rgba(255,255,255,0.1);color:#9ca3af;border:none;border-radius:4px;cursor:pointer;touch-action:manipulation;">✏️</button></div>';
      }).join('');
      soundboardSlots.forEach((s,i)=>{
        const el = document.getElementById('sb'+i);
        if (!el) return;
        const playIfNotBtn = (ev) => {
          const t = ev.target;
          if (t && t.closest && t.closest('button')) return;
          sbPlaySlot(s, i);
        };
        if (window.PointerEvent) {
          el.addEventListener('pointerup', playIfNotBtn);
        } else {
          el.onclick = playIfNotBtn;
        }
        el.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); sbPlaySlot(s, i); }
        });
      });
    }
    window.renderSoundboard = renderSoundboard;
    function sbSetLoadErr(msg){
      const e = document.getElementById('soundboardLoadErr');
      if (!e) return;
      if (msg) { e.style.display = 'block'; e.textContent = msg; }
      else { e.style.display = 'none'; e.textContent = ''; }
    }
    function sbApplyLitePayload(d){
      sbSetLoadErr('');
      if (d && d.slots && d.slots.length) { soundboardSlots = d.slots; }
      if (typeof d.text_max_len === 'number' && d.text_max_len > 0) { sbTextMax = d.text_max_len; const mx = document.getElementById('sbModalCharMax'); if(mx) mx.textContent = sbTextMax; }
      renderSoundboard();
      const sbpd = document.getElementById('sbPlayDest');
      if (sbpd && !sbpd._sbVisBound) { sbpd._sbVisBound = true; sbpd.addEventListener('change', updateSbBrowserRowVisibility); }
      updateSbBrowserRowVisibility();
    }
    function sbLoadLiteSlots(){
      return fetch('/api/soundboard?lite=1').then(function(r){
        if (!r.ok) return Promise.reject(new Error('HTTP '+r.status));
        return r.json();
      }).then(sbApplyLitePayload).catch(function(err){
        sbSetLoadErr('Elenco slot dal server non disponibile ('+(err && err.message ? err.message : 'rete')+'). I pulsanti sotto restano usabili; torna su Sound o ricarica per riprovare.');
      });
    }
    soundboardSlots = Array.from({length: 20}, function(_, i){
      return { icon: sbIconAt(i), text: 'Comando '+(i+1), has_robot: false, has_clean: false };
    });
    renderSoundboard();
    sbLoadLiteSlots();
    (function(){
      var prev = window.g1ActivateClientSection;
      if (typeof prev !== 'function') return;
      window.g1ActivateClientSection = function(sec){
        prev(sec);
        if (sec === 'soundboard') {
          setTimeout(function(){
            if (!soundboardSlots.length) { sbLoadLiteSlots(); }
            else if (typeof window.renderSoundboard === 'function') { window.renderSoundboard(); }
          }, 0);
        }
        if (sec === 'parla') {
          setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 120);
        } else {
          if (typeof stopParlaMicPreview === 'function') stopParlaMicPreview();
        }
        return false;
      };
    })();
    function updateSbModalStatus(){
      const st = document.getElementById('sbModalAudioStatus');
      if(!st) return;
      const kb = sbEditAudio ? Math.round((sbEditAudio.length||0)/1024) : 0;
      st.innerHTML = sbEditAudio ? '&#128266; <span style="color:#14b8a6;">Audio naturale</span> '+kb+' KB' : '&#128266; <span style="color:#71717a;">Nessun audio</span>';
    }
    function editSoundboard(idx){
      sbEditIdx = idx;
      const s = soundboardSlots[idx] || {};
      document.getElementById('sbModalSlot').textContent = idx+1;
      document.getElementById('sbModalIcon').value = s.icon || sbIconAt(idx);
      document.getElementById('sbModalText').value = s.text || 'Comando '+(idx+1);
      document.getElementById('sbModalText').setAttribute('maxlength', String(sbTextMax));
      sbEditAudioRaw = null;
      function applyFull(full){
        sbEditAudio = full.audio_base64 || null;
        sbEditFmt = full.format || 'webm';
        sbEditAudioClean = full.audio_base64_clean || null;
        sbEditFmtClean = full.format_clean || 'mp3';
        updateSbModalStatus();
        updateSbCharCount();
      }
      document.getElementById('sbModal').style.display = 'flex';
      if ((s.audio_base64 && s.audio_base64.length>50) || (s.audio_base64_clean && s.audio_base64_clean.length>50)) {
        applyFull(s);
        return;
      }
      var st = document.getElementById('sbModalAudioStatus');
      if (st) st.innerHTML = 'Caricamento audio…';
      sbEditAudio = null; sbEditFmt = 'webm'; sbEditAudioClean = null; sbEditFmtClean = 'mp3';
      fetch('/api/soundboard-slot/'+idx).then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }).then(applyFull).catch(function(e){
        if (st) st.innerHTML = 'Errore: '+(e.message||String(e));
      });
    }
    const sbModalTextEl = document.getElementById('sbModalText');
    if (sbModalTextEl) { sbModalTextEl.oninput = updateSbCharCount; }
    function closeSbModal(){ document.getElementById('sbModal').style.display = 'none'; sbEditIdx = -1; sbEditAudio = null; sbEditAudioClean = null; sbEditFmtClean = 'mp3'; }
    document.getElementById('sbModalCancel').onclick = closeSbModal;
    document.getElementById('sbModalSave').onclick = ()=>{
      if (sbEditIdx < 0) return;
      const icon = (document.getElementById('sbModalIcon').value || '🎤').trim().substring(0,4);
      const text = (document.getElementById('sbModalText').value || 'Comando '+(sbEditIdx+1)).trim().substring(0, sbTextMax);
      fetch('/api/soundboard', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({slot:sbEditIdx, icon, text, audio_base64: sbEditAudio||'', format: sbEditFmt||'wav', audio_base64_clean: sbEditAudioClean||'', format_clean: sbEditFmtClean||'mp3'})}).then(r=>r.json()).then(()=>{ sbLoadLiteSlots(); });
      closeSbModal();
    };
    document.getElementById('sbModalSynth').onclick = async ()=>{
      const text = (document.getElementById('sbModalText').value||'').trim().substring(0, sbTextMax);
      if (!text) { alert('Scrivi il testo da sintetizzare'); return; }
      const btn = document.getElementById('sbModalSynth');
      btn.disabled = true;
      document.getElementById('sbModalAudioStatus').innerHTML = 'Generazione TTS...';
      try {
        const r = await fetch('/api/soundboard-synth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
        const d = await r.json();
        if (d.ok) {
          sbEditAudio = d.audio_base64; sbEditFmt = d.format||'wav';
          sbEditAudioClean = d.audio_base64_clean || d.audio_base64; sbEditFmtClean = d.format_clean || d.format || 'wav';
          sbEditAudioRaw = null; updateSbModalStatus();
        }
        else alert(d.error || 'Errore TTS');
      } catch(e) { alert('Errore: '+e.message); }
      btn.disabled = false;
    };
    document.getElementById('sbModalRecord').onclick = ()=>{
      if (!navigator.mediaDevices) { alert('Microfono non disponibile'); return; }
      document.getElementById('sbModalRecord').disabled = true;
      document.getElementById('sbModalAudioStatus').innerHTML = 'Registrazione 3 sec...';
      navigator.mediaDevices.getUserMedia({audio:true}).then(stream=>{
        const mr = new MediaRecorder(stream, {mimeType: MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm'});
        const chunks = [];
        mr.ondataavailable = e=>{ if(e.data.size) chunks.push(e.data); };
        mr.onstop = ()=>{
          stream.getTracks().forEach(t=>t.stop());
          const blob = new Blob(chunks, {type: mr.mimeType});
          const fr = new FileReader();
          fr.onload = async ()=>{
            const b64 = arrayBufferToBase64(fr.result);
            document.getElementById('sbModalAudioStatus').innerHTML = 'Registrazione pronta';
            sbEditAudio = b64; sbEditFmt = 'webm';
            sbEditAudioClean = b64; sbEditFmtClean = 'webm';
            sbEditAudioRaw = null;
            updateSbModalStatus();
            document.getElementById('sbModalRecord').disabled = false;
          };
          fr.readAsArrayBuffer(blob);
        };
        mr.start(); setTimeout(()=>mr.stop(), 3000);
      }).catch(()=>{ alert('Microfono non disponibile'); document.getElementById('sbModalRecord').disabled = false; });
    };
    document.getElementById('sbModalFile').onchange = async (e)=>{
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const ext = (f.name.split('.').pop()||'').toLowerCase();
      const mime = {mp3:'audio/mpeg',wav:'audio/wav',ogg:'audio/ogg',webm:'audio/webm',m4a:'audio/mp4'}[ext] || f.type || 'audio/mpeg';
      const buf = await f.arrayBuffer();
      const b64 = arrayBufferToBase64(buf);
      document.getElementById('sbModalAudioStatus').innerHTML = 'File caricato';
      sbEditAudio = b64; sbEditFmt = ext || 'mp3';
      sbEditAudioClean = b64; sbEditFmtClean = ext || 'mp3';
      sbEditAudioRaw = null;
      updateSbModalStatus();
      e.target.value = '';
    };
    document.getElementById('sbModalClear').onclick = ()=>{ sbEditAudio = null; sbEditFmt = ''; sbEditAudioClean = null; sbEditFmtClean = 'mp3'; sbEditAudioRaw = null; updateSbModalStatus(); };
    document.getElementById('sbModalTts').onclick = async ()=>{
      if (!sbEditAudio || sbEditAudio.length < 100) { alert('Serve prima un audio (registra o importa)'); return; }
      const btn = document.getElementById('sbModalTts');
      btn.disabled = true;
      document.getElementById('sbModalAudioStatus').innerHTML = 'Riprocessamento con TTS...';
      try {
        const r = await fetch('/api/audio-to-robot-voice', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({audio_base64: sbEditAudio, format: sbEditFmt||'wav'})});
        const d = await r.json();
        if (d.ok) {
          sbEditAudio = d.audio_base64;
          sbEditFmt = 'mp3';
          sbEditAudioClean = d.audio_base64;
          sbEditFmtClean = 'mp3';
          updateSbModalStatus();
        } else alert(d.error || 'Errore');
      } catch(e) { alert('Errore: '+e.message); }
      btn.disabled = false;
    };
    var sbOutRef = document.getElementById('sbOutputRefresh');
    if (sbOutRef) sbOutRef.onclick = () => { if (typeof requestAndLoadDevices === 'function') requestAndLoadDevices(); };
    function escAttr(s){ return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/\u003c/g,'&lt;'); }
    function loadRunSheet(){
      fetch('/api/run-sheet').then(r=>r.json()).then(d=>{
        const pol = document.getElementById('runSheetPolicy');
        if(pol) pol.value = d.policy || '';
        const tb = document.getElementById('runSheetBody');
        if(!tb) return;
        const rows = d.rows || [];
        tb.innerHTML = rows.map((row,i)=>'<tr><td><input type="text" class="rs-fase" data-i="'+i+'" value="'+escAttr(row.fase)+'" /></td><td><input type="text" class="rs-att" value="'+escAttr(row.attivita)+'" /></td><td><input type="text" class="rs-ora" value="'+escAttr(row.ora_inizio)+'" /></td><td><input type="text" class="rs-dur" value="'+escAttr(row.durata_stimata)+'" /></td><td><input type="text" class="rs-note" value="'+escAttr(row.note)+'" /></td></tr>').join('');
      }).catch(()=>{});
    }
    document.getElementById('runSheetSave').onclick = ()=>{
      const policy = (document.getElementById('runSheetPolicy').value||'').trim();
      const rows = [];
      document.querySelectorAll('#runSheetBody tr').forEach(tr=>{
        rows.push({
          fase: (tr.querySelector('.rs-fase')&&tr.querySelector('.rs-fase').value)||'',
          attivita: (tr.querySelector('.rs-att')&&tr.querySelector('.rs-att').value)||'',
          ora_inizio: (tr.querySelector('.rs-ora')&&tr.querySelector('.rs-ora').value)||'',
          durata_stimata: (tr.querySelector('.rs-dur')&&tr.querySelector('.rs-dur').value)||'',
          note: (tr.querySelector('.rs-note')&&tr.querySelector('.rs-note').value)||''
        });
      });
      const st = document.getElementById('runSheetStatus');
      fetch('/api/run-sheet', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({policy, rows})})
        .then(r=>r.json()).then(d=>{ if(st) st.textContent = d.ok ? 'Salvato.' : (d.error||'Errore'); if(st) st.style.color = d.ok ? '#22c55e' : '#f87171'; })
        .catch(e=>{ if(st) st.textContent = e.message; if(st) st.style.color = '#f87171'; });
    };
    loadRunSheet();

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
      status.textContent = ' Elaborazione (IA)…';
      status.style.color = '#a1a1aa';
      startThinkingFeedback(false);
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
            applySinkThenPlay(a, resolveBrowserPlaybackSinkIdLikeSoundboard()).catch(function(){});
          }
        }
      } catch (e) {
        status.textContent = ' Errore: ' + (e.message || String(e));
        status.style.color = '#dc2626';
      }
      stopThinkingFeedback();
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
            applySinkThenPlay(a, resolveBrowserPlaybackSinkIdLikeSoundboard()).catch(function(){});
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
    function onRecStart(e){
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      e.preventDefault();
      if (!isRecording) startRec();
      try {
        if (e.pointerId != null && isRecording) btn.setPointerCapture(e.pointerId);
      } catch (_) {}
    }
    function onRecStop(e){
      if (!isRecording) return;
      e.preventDefault();
      stopRec();
      try {
        if (e.pointerId != null) btn.releasePointerCapture(e.pointerId);
      } catch (_) {}
    }
    /* Pointer Events + setPointerCapture: niente listener su document (non bloccano select/tendine sul resto della pagina). */
    if (window.PointerEvent) {
      btn.addEventListener('pointerdown', onRecStart);
      btn.addEventListener('pointerup', onRecStop);
      btn.addEventListener('pointercancel', onRecStop);
    } else {
      function onRecStartLegacy(ev){ ev.preventDefault(); if(!isRecording) startRec(); }
      function onRecStopLegacy(ev){ if(!isRecording) return; ev.preventDefault(); stopRec(); }
      btn.onmousedown = btn.ontouchstart = onRecStartLegacy;
      btn.onmouseup = btn.ontouchend = btn.ontouchcancel = onRecStopLegacy;
      /* Niente listener su document: touchend/mouseup globali con passive:false possono interferire con select/tap altrove (browser vecchi). */
    }

    async function startRec(){
      if(isRecording) return;
      wakeListenPending = false;
      stopWakeRecorder();
      const micSelVal = document.getElementById('mic').value;
      if (micSelVal && micSelVal.indexOf('local_') === 0) {
        await startRecServerPtt();
        return;
      }
      isRecording = true;
      pendingStop = false;
      const spkVal = document.getElementById('speaker').value;
      const ttsEl = document.getElementById('ttsPlayDest');
      const wantServerTts = ttsEl && ttsEl.value === 'server';
      if (wantServerTts && (serverTtsDeviceId === null || isNaN(serverTtsDeviceId))) {
        isRecording = false;
        document.getElementById('result').innerHTML = '<div class="warn">Per sentire la risposta sulla cassa: sul Jetson apri il setup (<strong>/</strong>), scegli un altoparlante <strong>locale</strong>, Salva, poi ricarica questa pagina.</div>';
        return;
      }
      if (wantServerTts) {
        lastPlayOn = 'server';
        lastSinkId = null;
      } else {
        lastPlayOn = 'browser';
        lastSinkId = (spkVal && spkVal.startsWith('browser_') && spkVal !== 'browser_default') ? spkVal.replace('browser_','') : null;
        syncSbOutputFromSpeaker();
      }
      const deviceId = wantServerTts ? serverTtsDeviceId : null;
      try {
        stopParlaMicPreview();
        const stream = await navigator.mediaDevices.getUserMedia(buildAudioCaptureConstraints(micForBrowserCapture()));
        if(pendingStop){ stream.getTracks().forEach(t=>t.stop()); isRecording=false; return; }
        currentStream = stream;
        await new Promise(r => setTimeout(r, 150));
        if(pendingStop){ stream.getTracks().forEach(t=>t.stop()); isRecording=false; return; }
        const mimeType = preferredRecorderMime();
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
            setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 200);
            return;
          }
          setTimeout(function(){
            sendAudio(lastPlayOn, deviceId);
            if (document.getElementById('wakeListenToggle') && document.getElementById('wakeListenToggle').checked) setTimeout(function(){ startWakeRecorder(); }, 400);
            setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 350);
          }, 80);
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
      if (recordingServerJetson) {
        clearAllIntervals();
        btn.classList.remove('recording');
        recordingServerJetson = false;
        isRecording = false;
        if (wsParla && wsParla.readyState === WebSocket.OPEN) {
          try { wsParla.send(JSON.stringify({type:'stop'})); } catch(_){}
        }
        document.getElementById('recDebug').textContent = 'Elaborazione (audio dal robot)…';
        document.getElementById('recDebug').style.color = '#3b82f6';
        startThinkingFeedback();
        return;
      }
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
      wakeListenPending = false;
      wakeQueuedBlob = null;
      wakeAudioInFlight = false;
      if(!chunks.length || !ws || ws.readyState !== WebSocket.OPEN){
        document.getElementById('recDebug').textContent = 'Errore: '+(!chunks.length ? 'nessun audio' : 'WebSocket chiuso');
        document.getElementById('recDebug').style.color = '#dc2626';
        return;
      }
      const recMime = mediaRecorder && mediaRecorder.mimeType ? mediaRecorder.mimeType : preferredRecorderMime();
      const blob = new Blob(chunks, {type: recMime});
      if(blob.size < WS_AUDIO_MIN_BYTES){
        document.getElementById('recDebug').textContent = 'Audio troppo corto ('+(blob.size/1024).toFixed(1)+' KB). Tieni premuto 1-2 secondi.';
        document.getElementById('recDebug').style.color = '#f59e0b';
        btn.disabled = false;
        return;
      }
      const sizeKb = (blob.size/1024).toFixed(1);
      document.getElementById('recDebug').textContent = 'Invio '+sizeKb+' KB...';
      document.getElementById('recDebug').style.color = '#3b82f6';
      document.getElementById('result').innerHTML = '<div style="color:#3b82f6;">Elaborazione…</div>';
      btn.disabled = true;
      startThinkingFeedback();
      const fr = new FileReader();
      fr.onload = () => {
        const b64 = arrayBufferToBase64(fr.result);
        sendAudioOverWs(b64, recMime, { playOn: playOn, skipWake: true, deviceId: outDeviceId });
        chunks = [];
      };
      fr.readAsArrayBuffer(blob);
    }
    /* Disinstalla eventuali SW vecchi: su mobile possono servire HTML/JS in cache e UI che «non clicca». */
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.getRegistrations().then(function(regs){ regs.forEach(function(r){ r.unregister(); }); }).catch(function(){});
    }

    /* ---- Live Mic Monitor (WebSocket /ws/mic-level) ---- */
    (function(){
      var monWs = null;
      var monActive = false;
      var btnMon = document.getElementById('btnMicMonitor');
      var monBody = document.getElementById('micMonitorBody');
      var monBar = document.getElementById('monLevelBar');
      var monInfo = document.getElementById('monLevelInfo');
      var monName = document.getElementById('monMicName');
      if (!btnMon) return;

      function startMonitor() {
        if (monWs) { try { monWs.close(); } catch(_){} }
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        monWs = new WebSocket(proto + '//' + location.host + '/ws/mic-level');
        monActive = true;
        btnMon.textContent = 'Stop';
        btnMon.style.background = '#ef4444';
        monBody.style.display = 'block';
        monInfo.textContent = 'Connessione...';

        monWs.onmessage = function(ev) {
          try {
            var d = JSON.parse(ev.data);
            if (d.type === 'info') {
              monName.textContent = 'Mic: [' + d.device + '] ' + d.name + '  rate=' + d.rate;
            } else if (d.type === 'level') {
              var pct = Math.max(0, Math.min(100, ((d.db + 60) / 60) * 100));
              monBar.style.width = pct.toFixed(1) + '%';
              if (d.peak > 0.5) {
                monBar.style.background = '#ef4444';
              } else if (d.rms > 0.02) {
                monBar.style.background = '#22c55e';
              } else if (d.rms > 0.005) {
                monBar.style.background = '#eab308';
              } else {
                monBar.style.background = '#52525b';
              }
              monInfo.textContent = 'RMS=' + d.rms.toFixed(4) + '  Peak=' + d.peak.toFixed(4) + '  dB=' + d.db.toFixed(1);
            } else if (d.type === 'error') {
              monInfo.textContent = 'Errore: ' + d.data;
              monBar.style.width = '0%';
            }
          } catch(_){}
        };
        monWs.onclose = function() {
          monActive = false;
          btnMon.textContent = 'Avvia';
          btnMon.style.background = '#3b82f6';
        };
        monWs.onerror = function() {
          monInfo.textContent = 'Errore connessione';
        };
      }

      function stopMonitor() {
        monActive = false;
        if (monWs) { try { monWs.close(); } catch(_){} monWs = null; }
        btnMon.textContent = 'Avvia';
        btnMon.style.background = '#3b82f6';
        monBar.style.width = '0%';
        monInfo.textContent = 'RMS=-- Peak=-- dB=--';
        monBody.style.display = 'none';
      }

      btnMon.onclick = function() {
        if (monActive) stopMonitor();
        else startMonitor();
      };
    })();

    /* ---- Persistent Mic Level (always visible above tabs) ---- */
    (function(){
      var _pmWs = null;
      var _pmTimer = null;
      var _pmSource = null;

      function pmUpdateFromBrowser() {
        var bar = document.getElementById('persistMicBar');
        var lbl = document.getElementById('persistMicLabel');
        if (!bar) return;
        var an = analyserNode || parlaPreviewAnalyser || wakeAnalyser || null;
        if (!an) {
          bar.style.width = '0%';
          if (lbl) lbl.textContent = '--';
          return;
        }
        var buf = new Uint8Array(an.frequencyBinCount);
        an.getByteFrequencyData(buf);
        var peak = 0;
        for (var i = 0; i < buf.length; i++) if (buf[i] > peak) peak = buf[i];
        var gain = typeof getParlaMonitorGain === 'function' ? getParlaMonitorGain() : 1;
        var pct = Math.min(100, peak * gain * (100 / 255));
        bar.style.width = pct.toFixed(1) + '%';
        bar.style.background = peak > 128 ? '#ef4444' : peak > 50 ? '#22c55e' : peak > 13 ? '#eab308' : '#52525b';
        if (lbl) lbl.textContent = peak > 5 ? (pct|0) + '%' : '--';
      }

      function pmStartServerWs() {
        if (_pmWs && _pmWs.readyState <= 1) return;
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        _pmWs = new WebSocket(proto + '//' + location.host + '/ws/mic-level');
        _pmWs.onmessage = function(ev) {
          try {
            var d = JSON.parse(ev.data);
            if (d.type === 'level') {
              var bar = document.getElementById('persistMicBar');
              var lbl = document.getElementById('persistMicLabel');
              if (bar) {
                var pct = Math.max(0, Math.min(100, ((d.db + 60) / 60) * 100));
                bar.style.width = pct.toFixed(1) + '%';
                bar.style.background = d.peak > 0.5 ? '#ef4444' : d.rms > 0.02 ? '#22c55e' : d.rms > 0.005 ? '#eab308' : '#52525b';
              }
              if (lbl) lbl.textContent = d.rms > 0.01 ? (pct|0) + '%' : '--';
            }
          } catch(_){}
        };
        _pmWs.onclose = function() { _pmWs = null; _pmSource = null; };
        _pmWs.onerror = function() { try { _pmWs.close(); } catch(_){} _pmWs = null; _pmSource = null; };
      }

      function pmStopServerWs() {
        if (_pmWs) { try { _pmWs.close(); } catch(_){} _pmWs = null; }
        _pmSource = null;
      }

      _pmTimer = setInterval(function() {
        var micEl = document.getElementById('mic');
        var micVal = micEl ? micEl.value : '';
        var isLocal = micVal && micVal.indexOf('local_') === 0;

        if (isLocal) {
          if (_pmSource !== 'server') {
            _pmSource = 'server';
            pmStartServerWs();
          } else if (!_pmWs || _pmWs.readyState > 1) {
            pmStartServerWs();
          }
          return;
        }
        if (_pmSource === 'server') { pmStopServerWs(); }
        _pmSource = 'browser';
        pmUpdateFromBrowser();
      }, 60);
    })();
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
  <p style="color:#71717a;">Tieni premuto: registra dal <strong>microfono USB sulla Jetson</strong> (come in setup). Rilascia per inviare.</p>
  <p style="color:#a78bfa;font-size:12px;max-width:420px;">Per sentire la risposta sulla <strong>cassa Bluetooth del telefono</strong>: in setup scegli altoparlante <strong>Rete / Browser</strong>, salva, poi apri questa pagina dal telefono. Il TTS esce dal browser (accoppia il telefono alla cassa BT).</p>
  <p style="color:#71717a;font-size:11px;">La barra livello sotto usa il mic del telefono solo come indicatore; l&apos;audio inviato all&apos;IA è sempre quello della Jetson se il mic è locale.</p>
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
          if (r.audio_base64 && String(r.audio_base64).length > 80) {
            try {
              var ap = new Audio('data:audio/mpeg;base64,' + r.audio_base64);
              ap.play();
            } catch(e) {}
          }
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
  <p style="color:#71717a;">Di &laquo;Hey G1&raquo; + domanda in un&apos;unica frase. Slice 6 sec. Non toccare nulla.</p>
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


ROBOT_CONTROL_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>G1 Robot Control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body{min-height:100%;background:#0c0e14;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;overflow-x:hidden;overflow-y:auto;-webkit-overflow-scrolling:touch;touch-action:pan-y;}
body{padding-bottom:max(24px,env(safe-area-inset-bottom));}
.top-bar{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:#18181b;border-bottom:1px solid #27272a;min-height:44px;position:sticky;top:0;z-index:20;flex-shrink:0;}
.top-bar h1{font-size:15px;font-weight:700;color:#14b8a6;}
.top-bar .status{font-size:11px;color:#71717a;}
.top-bar .status.ok{color:#22c55e;}
.top-bar .status.err{color:#ef4444;}
.top-bar a{color:#5eead4;text-decoration:none;font-size:12px;}
.container{display:block;padding:8px 8px 20px;}
.actions-area{padding:0 0 8px;}
.section-label{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#71717a;margin:8px 0 6px 4px;font-weight:600;}
.gesture-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:10px;}
.gesture-btn{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:10px 4px;background:#1e1e2e;border:1px solid #27272a;border-radius:10px;color:#e4e4e7;font-size:10px;text-align:center;cursor:pointer;transition:background 0.15s,border-color 0.15s;min-height:64px;-webkit-tap-highlight-color:transparent;}
.gesture-btn:active,.gesture-btn.active{background:#14b8a620;border-color:#14b8a6;}
.gesture-btn .g-icon{font-size:22px;line-height:1;}
.gesture-btn .g-label{font-size:9px;line-height:1.2;color:#a1a1aa;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.gesture-btn.release{border-color:#ef444440;background:#ef444410;}
.gesture-btn.release:active{background:#ef444430;border-color:#ef4444;}
.gesture-btn.special{border-color:#6366f140;background:#6366f110;}
.gesture-btn.special:active{background:#6366f130;border-color:#6366f1;}
.joystick-area{display:flex;align-items:flex-start;justify-content:center;gap:12px;flex-wrap:nowrap;padding:14px 8px 20px;margin-top:8px;background:#111318;border:1px solid #27272a;border-radius:12px;touch-action:manipulation;}
.joy-col{display:flex;flex-direction:column;align-items:center;gap:6px;}
.joy-col-label{font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:#71717a;font-weight:600;}
.joy-wrap{position:relative;width:140px;height:140px;flex-shrink:0;touch-action:none;}
.joy-base{position:absolute;inset:0;border-radius:50%;background:radial-gradient(circle,#1e1e2e 0%,#18181b 100%);border:2px solid #27272a;}
.joy-stick{position:absolute;width:52px;height:52px;border-radius:50%;background:radial-gradient(circle,#2dd4bf 0%,#14b8a6 100%);box-shadow:0 0 12px #14b8a640;left:50%;top:50%;transform:translate(-50%,-50%);transition:none;pointer-events:none;}
.joy-stick.rot{background:radial-gradient(circle,#a78bfa 0%,#7c3aed 100%);box-shadow:0 0 12px #7c3aed40;}
.joy-info{display:flex;flex-direction:column;gap:4px;font-size:11px;color:#71717a;font-family:monospace;text-align:center;}
.joy-info span{color:#e4e4e7;}
.speed-wrap{display:flex;flex-direction:column;gap:4px;align-items:center;}
.speed-wrap label{font-size:10px;color:#71717a;}
.speed-wrap input[type=range]{width:110px;accent-color:#14b8a6;}
.joy-settings{display:flex;flex-direction:column;gap:8px;align-items:center;justify-content:center;min-width:80px;}
.quick-btns{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;}
.quick-btn{padding:8px 14px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:12px;cursor:pointer;-webkit-tap-highlight-color:transparent;}
.quick-btn:active{background:#14b8a630;border-color:#14b8a6;}
.quick-btn.loco{background:rgba(34,197,94,0.12);border-color:rgba(34,197,94,0.35);color:#86efac;}
.quick-btn.loco:active{background:rgba(34,197,94,0.28);}
#robotIp{width:140px;padding:6px 8px;background:#1e1e2e;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:12px;font-family:monospace;}
.log-area{max-height:60px;overflow-y:auto;padding:4px 8px;font-size:10px;color:#52525b;font-family:monospace;line-height:1.4;}
.log-area .ok{color:#22c55e;}.log-area .err{color:#ef4444;}
</style>
</head>
<body>
<div class="top-bar">
  <h1>G1 Robot Control</h1>
  <div style="display:flex;align-items:center;gap:8px;">
    <label style="font-size:10px;color:#71717a;">IP:</label>
    <input id="robotIp" value="192.168.123.161" />
    <span id="connStatus" class="status">--</span>
    <a href="/client">Client</a>
  </div>
</div>
<div class="container">
  <div class="actions-area">
    <div class="section-label">Gesti braccia</div>
    <div class="gesture-grid" id="gestureGrid"></div>
    <div class="section-label">Locomozione (sport mode)</div>
    <div class="quick-btns">
      <button type="button" class="quick-btn loco" onclick="sendLoco('ready')">Ready</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('walk')">Walk</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('stop_walk')">Stop walk</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('low_stand')">Low stand</button>
    </div>
    <div class="section-label">Braccia / sicurezza</div>
    <div class="quick-btns">
      <button type="button" class="quick-btn" onclick="sendAction(99)">Rilascia braccia</button>
      <button type="button" class="quick-btn" onclick="sendMove(0,0,0)">STOP vel.</button>
    </div>
    <div class="log-area" id="logArea"></div>
  </div>
  <div class="joystick-area">
    <div class="joy-col">
      <div class="joy-col-label">Movimento</div>
      <div class="joy-wrap" id="joyMoveWrap">
        <div class="joy-base"></div>
        <div class="joy-stick" id="joyMoveStick"></div>
      </div>
      <div class="joy-info">
        <div>vx: <span id="jVx">0.00</span> &nbsp; vy: <span id="jVy">0.00</span></div>
      </div>
    </div>
    <div class="joy-settings">
      <div class="speed-wrap">
        <label>Vel. max</label>
        <input type="range" id="speedMax" min="0.1" max="1.5" step="0.1" value="0.5" />
        <span style="font-size:11px;color:#a1a1aa;" id="speedLabel">0.5</span>
      </div>
      <div class="speed-wrap">
        <label>Rot. max</label>
        <input type="range" id="yawMax" min="0.2" max="2.0" step="0.1" value="0.8" />
        <span style="font-size:11px;color:#a1a1aa;" id="yawLabel">0.8</span>
      </div>
    </div>
    <div class="joy-col">
      <div class="joy-col-label">Rotazione</div>
      <div class="joy-wrap" id="joyRotWrap">
        <div class="joy-base"></div>
        <div class="joy-stick rot" id="joyRotStick"></div>
      </div>
      <div class="joy-info">
        <div>vyaw: <span id="jVyaw">0.00</span></div>
      </div>
    </div>
  </div>
</div>
<script>
(function(){
  var ROBOT_IP_KEY = 'g1_robot_ip';
  var ipEl = document.getElementById('robotIp');
  var saved = localStorage.getItem(ROBOT_IP_KEY);
  if (saved) ipEl.value = saved;
  ipEl.addEventListener('change', function(){ localStorage.setItem(ROBOT_IP_KEY, ipEl.value.trim()); });

  var ARM_ACTIONS = [
    {id:27, icon:'\\u{1F91D}', label:'Stretta di mano'},
    {id:26, icon:'\\u{1F596}', label:'Saluto alto'},
    {id:25, icon:'\\u{1F44B}', label:'Ciao (viso)'},
    {id:15, icon:'\\u{1F64C}', label:'Mani in alto'},
    {id:23, icon:'\\u261D\\uFE0F',  label:'Mano dx su'},
    {id:18, icon:'\\u270B',   label:'High Five'},
    {id:19, icon:'\\u{1F917}', label:'Abbraccio'},
    {id:17, icon:'\\u{1F44F}', label:'Applauso'},
    {id:20, icon:'\\u2764\\uFE0F',  label:'Cuore'},
    {id:21, icon:'\\u{1F49C}', label:'Cuore dx'},
    {id:22, icon:'\\u{1F645}', label:'No / Rifiuto'},
    {id:24, icon:'\\u274C',   label:'Braccia X'},
    {id:11, icon:'\\u{1F48B}', label:'Bacio (2 mani)'},
    {id:12, icon:'\\u{1F618}', label:'Bacio'},
  ];

  var grid = document.getElementById('gestureGrid');
  ARM_ACTIONS.forEach(function(a){
    var btn = document.createElement('button');
    btn.className = 'gesture-btn';
    btn.innerHTML = '<span class="g-icon">'+a.icon+'</span><span class="g-label">'+a.label+'</span>';
    btn.addEventListener('click', function(){ sendAction(a.id); });
    grid.appendChild(btn);
  });

  var logEl = document.getElementById('logArea');
  function log(msg, cls){
    var d = document.createElement('div');
    d.className = cls || '';
    d.textContent = new Date().toLocaleTimeString() + ' ' + msg;
    logEl.appendChild(d);
    if (logEl.children.length > 30) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  }

  window.sendAction = function(actionId){
    var ip = ipEl.value.trim();
    log('Action ' + actionId + ' -> ' + ip);
    fetch('/api/robot-action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action_id: actionId, robot_ip: ip})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) { log('OK: ' + (d.message||''), 'ok'); setStatus('ok'); }
      else { log('ERRORE: ' + (d.message||''), 'err'); setStatus('err'); }
    }).catch(function(e){ log('Rete: ' + e, 'err'); setStatus('err'); });
  };

  window.sendMove = function(vx, vy, vyaw){
    var ip = ipEl.value.trim();
    fetch('/api/robot-move', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({vx:vx, vy:vy, vyaw:vyaw, robot_ip:ip})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) setStatus('ok');
      else setStatus('err');
    }).catch(function(e){ setStatus('err'); });
  };

  window.sendLoco = function(command){
    var ip = ipEl.value.trim();
    log('Loco ' + command + ' -> ' + ip);
    fetch('/api/robot-loco', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: command, robot_ip: ip})
    }).then(function(r){ return r.json(); }).then(function(d){
      if (d.ok) { log('Loco OK: ' + (d.message||''), 'ok'); setStatus('ok'); }
      else { log('Loco ERR: ' + (d.message||''), 'err'); setStatus('err'); }
    }).catch(function(e){ log('Loco rete: ' + e, 'err'); setStatus('err'); });
  };

  function setStatus(s){
    var el = document.getElementById('connStatus');
    el.className = 'status ' + s;
    el.textContent = s === 'ok' ? 'OK' : s === 'err' ? 'ERR' : '--';
  }

  var speedSlider = document.getElementById('speedMax');
  var yawSlider = document.getElementById('yawMax');
  var speedLabel = document.getElementById('speedLabel');
  var yawLabel = document.getElementById('yawLabel');
  speedSlider.addEventListener('input', function(){ speedLabel.textContent = parseFloat(this.value).toFixed(1); });
  yawSlider.addEventListener('input', function(){ yawLabel.textContent = parseFloat(this.value).toFixed(1); });

  var jVx = document.getElementById('jVx');
  var jVy = document.getElementById('jVy');
  var jVyaw = document.getElementById('jVyaw');
  var curVx = 0, curVy = 0, curVyaw = 0;
  var moveInterval = null;

  function startSending(){
    if (moveInterval) return;
    moveInterval = setInterval(function(){
      if (Math.abs(curVx) > 0.01 || Math.abs(curVy) > 0.01 || Math.abs(curVyaw) > 0.01) {
        sendMove(curVx, curVy, curVyaw);
      }
    }, 200);
  }
  function stopSendingIfIdle(){
    if (Math.abs(curVx) < 0.01 && Math.abs(curVy) < 0.01 && Math.abs(curVyaw) < 0.01) {
      if (moveInterval) { clearInterval(moveInterval); moveInterval = null; }
      sendMove(0, 0, 0);
    }
  }

  function makeJoystick(wrapId, stickId, onUpdate, onRelease) {
    var wrap = document.getElementById(wrapId);
    var stick = document.getElementById(stickId);
    var touchId = null, active = false;
    function center() {
      var r = wrap.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2, radius: r.width / 2 - 26 };
    }
    function move(cx, cy) {
      var c = center();
      var dx = cx - c.x, dy = cy - c.y;
      var dist = Math.sqrt(dx * dx + dy * dy);
      var maxR = c.radius;
      if (dist > maxR) { dx = dx / dist * maxR; dy = dy / dist * maxR; }
      stick.style.transform = 'translate(calc(-50% + ' + dx + 'px), calc(-50% + ' + dy + 'px))';
      onUpdate(dx / maxR, -dy / maxR);
    }
    function reset() {
      stick.style.transform = 'translate(-50%,-50%)';
      onRelease();
    }
    wrap.addEventListener('touchstart', function(e) {
      e.preventDefault();
      var t = e.changedTouches[0];
      touchId = t.identifier; active = true;
      move(t.clientX, t.clientY); startSending();
    }, { passive: false });
    wrap.addEventListener('touchmove', function(e) {
      e.preventDefault();
      for (var i = 0; i < e.changedTouches.length; i++) {
        if (e.changedTouches[i].identifier === touchId) { move(e.changedTouches[i].clientX, e.changedTouches[i].clientY); break; }
      }
    }, { passive: false });
    wrap.addEventListener('touchend', function(e) {
      for (var i = 0; i < e.changedTouches.length; i++) {
        if (e.changedTouches[i].identifier === touchId) { active = false; touchId = null; reset(); break; }
      }
    });
    wrap.addEventListener('touchcancel', function() { active = false; touchId = null; reset(); });
    var md = false;
    wrap.addEventListener('mousedown', function(e) { md = true; move(e.clientX, e.clientY); startSending(); e.preventDefault(); });
    document.addEventListener('mousemove', function(e) { if (md) move(e.clientX, e.clientY); });
    document.addEventListener('mouseup', function() { if (md) { md = false; reset(); } });
  }

  makeJoystick('joyMoveWrap', 'joyMoveStick', function(nx, ny) {
    var sMax = parseFloat(speedSlider.value);
    curVx = Math.round(ny * sMax * 100) / 100;
    curVy = Math.round(-nx * sMax * 100) / 100;
    jVx.textContent = curVx.toFixed(2);
    jVy.textContent = curVy.toFixed(2);
  }, function() {
    curVx = 0; curVy = 0;
    jVx.textContent = '0.00'; jVy.textContent = '0.00';
    stopSendingIfIdle();
  });

  makeJoystick('joyRotWrap', 'joyRotStick', function(nx, ny) {
    var yMax = parseFloat(yawSlider.value);
    curVyaw = Math.round(-nx * yMax * 100) / 100;
    jVyaw.textContent = curVyaw.toFixed(2);
  }, function() {
    curVyaw = 0;
    jVyaw.textContent = '0.00';
    stopSendingIfIdle();
  });
})();
</script>
</body>
</html>
"""


def run(host: str = "0.0.0.0", port: int = 8081, skip_audio_check: bool = False, ssl_keyfile: str | None = None, ssl_certfile: str | None = None):
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
    try:
        from talk_module.audio.device_utils import probe_system_audio_hardware

        hw = probe_system_audio_hardware()
        if hw.get("arecord_l") or hw.get("aplay_l"):
            print("[Audio HW] Scansione ALSA (arecord/aplay -l) — vedi anche GET /api/devices → hardware_probe")
            for ln in (hw.get("arecord_l") or "").splitlines()[:6]:
                print("  [cap] " + ln[:140])
            for ln in (hw.get("aplay_l") or "").splitlines()[:6]:
                print("  [pb]  " + ln[:140])
    except Exception:
        pass
    import uvicorn
    try:
        from talk_module.robot_actions import _dds_interface_for_init

        print(
            f"[Robot] DDS iface (effective): {_dds_interface_for_init()!r}  "
            f"UNITREE_ROBOT_IP={os.getenv('UNITREE_ROBOT_IP', '192.168.123.161')!r}",
            flush=True,
        )
    except Exception:
        pass
    proto = "https" if ssl_keyfile else "http"
    print(f"G1 Talk Module - {proto}://{host}:{port}")
    print("  Setup dispositivi Jetson: /setup")
    print("  Ascolto Hey G1: /listen")
    print("  Parla (push): /local")
    print("  Client rete: /client")
    print("  Robot Control (joystick+gesti): /robot-control")
    if ssl_keyfile:
        print("  Da telefono (stessa rete): https://192.168.10.191:8081/client")
    uvicorn.run(app, host=host, port=port, ssl_keyfile=ssl_keyfile, ssl_certfile=ssl_certfile)


if __name__ == "__main__":
    import argparse
    _cert_dir = Path(__file__).resolve().parent.parent / "config" / "certs"
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--no-audio-check", action="store_true", help="Avvia anche senza PortAudio (solo dispositivi di rete)")
    p.add_argument("--ssl", action="store_true", help="Usa HTTPS (config/certs/). Per microfono da telefono.")
    args = p.parse_args()
    keyfile = certfile = None
    if args.ssl:
        keyfile = _cert_dir / "key.pem"
        certfile = _cert_dir / "cert.pem"
        if not keyfile.exists() or not certfile.exists():
            print("Certificati non trovati. Esegui: bash scripts/generate_ssl_cert.sh")
            keyfile = certfile = None
    run(args.host, args.port, skip_audio_check=args.no_audio_check, ssl_keyfile=str(keyfile) if keyfile else None, ssl_certfile=str(certfile) if certfile else None)
