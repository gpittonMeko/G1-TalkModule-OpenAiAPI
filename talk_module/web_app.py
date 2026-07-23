"""
Web App G1 Talk Module - AI Accelerator.
Wizard setup: seleziona microfono e altoparlante (locale o client web nella rete).
Tutto gira sulla macchina AI Accelerator.
"""

import asyncio
import base64
import json
import os
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

# Pool ampio: STT/LLM/TTS sono bloccanti; con 2 worker le richieste si accodavano e «gelavano» il server.
_executor = ThreadPoolExecutor(max_workers=min(32, (os.cpu_count() or 4) * 4))

# FastAPI + WebSocket
try:
    from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body, Request, Query
    from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from talk_module.config import settings
from talk_module.wake import WAKE_STT_PROMPT, find_wake_and_rest
from talk_module.audio_robot_effect import apply_robot_effect_base64
from talk_module.network_discovery import (
    register_web_client,
    unregister_web_client,
    list_network_clients,
)

# Config file per scelte utente
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "audio_devices.json"
from talk_module.knowledge_store import (
    check_knowledge,
    knowledge_path as KNOWLEDGE_PATH,
    load_knowledge,
    load_knowledge_groups,
    reload_knowledge,
    save_knowledge_entries,
    save_knowledge_groups,
)
SOUNDBOARD_PATH = Path(__file__).resolve().parent.parent / "config" / "soundboard.json"
SOUNDBOARD_LITE_PATH = Path(__file__).resolve().parent.parent / "config" / "soundboard_lite.json"
RUN_SHEET_PATH = Path(__file__).resolve().parent.parent / "config" / "run_sheet.json"


def _soundboard_slot_count() -> int:
    try:
        n = int((os.getenv("G1_SOUNDBOARD_SLOTS") or "100").strip())
    except ValueError:
        n = 100
    return max(1, min(n, 100))


SOUNDBOARD_AUDIO_ZONE_COUNT = 50
SOUNDBOARD_SLOT_COUNT = _soundboard_slot_count()
SOUNDBOARD_TEXT_MAX_LEN = 280
# soundboard.json può essere ~20MB: una sola lettura parse in RAM finché il file non cambia (mtime).
_soundboard_cache: tuple[int, list[dict]] | None = None
# Una sola riproduzione soundboard sulla cassa Jetson alla volta (evita sovrapposizioni).
_soundboard_local_play_lock = threading.Lock()


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


_WAKE_DEBUG_DIR = settings.audio_dir / "wake_debug"
_WAKE_DEBUG_MAX = 50


def _save_wake_debug_audio(audio_bytes: bytes, stt_text: str, kind: str) -> str | None:
    """Salva ogni slice wake con timestamp + trascrizione per debug. Max 50 file, poi ruota."""
    try:
        from datetime import datetime
        settings.ensure_dirs()
        _WAKE_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:20]
        safe_text = re.sub(r'[^\w\s-]', '', (stt_text or 'empty')[:40]).strip().replace(' ', '_') or 'empty'
        fname = f"{ts}_{kind}_{safe_text}.webm"
        out = _WAKE_DEBUG_DIR / fname
        out.write_bytes(audio_bytes)
        existing = sorted(_WAKE_DEBUG_DIR.glob("*.webm"), key=lambda p: p.stat().st_mtime)
        while len(existing) > _WAKE_DEBUG_MAX:
            existing.pop(0).unlink(missing_ok=True)
        return str(out.name)
    except Exception as e:
        print(f"[Wake] debug audio save error: {e}", flush=True)
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


def _collect_host_ips() -> list[str]:
    """IP LAN del server (Jetson) per link client da telefono/PC sulla stessa rete."""
    ips: list[str] = []
    try:
        import subprocess

        raw = subprocess.check_output(["hostname", "-I"], text=True, timeout=2).strip()
        ips = [x for x in raw.split() if x and not x.startswith("127.")]
    except Exception:
        pass
    if not ips:
        try:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips = [s.getsockname()[0]]
            s.close()
        except Exception:
            pass
    return ips


_SERVER_LOG_PATH = Path("/tmp/talk.log")


def _tail_server_log(max_lines: int = 80) -> list[str]:
    from collections import deque

    n = max(10, min(int(max_lines), 500))
    try:
        if not _SERVER_LOG_PATH.is_file():
            return ["(log non presente — avvia: bash scripts/restart_server.sh)"]
        with _SERVER_LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=n)]
    except Exception as e:
        return [f"Errore lettura log: {e}"]


if HAS_FASTAPI:
    from fastapi.responses import JSONResponse
    from talk_module.audio.device_utils import resolve_configured_microphone_index

    app = FastAPI(
        title="G1 Talk Module",
        description="Setup e controllo vocale - AI Accelerator",
        version="1.0.0",
    )

    try:
        from talk_module.explore_teaching_api import router as explore_teaching_router
        app.include_router(explore_teaching_router)
    except Exception as _explore_err:
        print(f"[web_app] explore_teaching_api router not loaded: {_explore_err}")

    try:
        from talk_module.teaching_api import router as teaching_router
        if os.getenv("G1_LOCAL_TEACHING", "0").strip().lower() in ("1", "true", "yes", "on"):
            app.include_router(teaching_router)
        else:
            print("[web_app] local teaching API disabled (set G1_LOCAL_TEACHING=1 to enable REC/slot teaching)")
    except Exception as _tea_err:
        print(f"[web_app] teaching_api router not loaded: {_tea_err}")

    try:
        from talk_module.vr_teleop_api import router as vr_router
        app.include_router(vr_router)
    except Exception as _vr_err:
        print(f"[web_app] vr_teleop_api router not loaded: {_vr_err}")

    try:
        from talk_module.camera_api import router as camera_router
        app.include_router(camera_router)
    except Exception as _cam_err:
        print(f"[web_app] camera_api router not loaded: {_cam_err}")

    try:
        from talk_module.pick_api import router as pick_router
        app.include_router(pick_router)
    except Exception as _pick_err:
        print(f"[web_app] pick_api router not loaded: {_pick_err}")

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
        if _stt is None or _llm is None or _tts is None:
            _stt = _llm = _tts = None
            from talk_module.llm import create_llm_client
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
            _llm = create_llm_client()
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
        html = (
            CLIENT_TEMPLATE.replace("__STT_WAKE_SLICE_MS__", str(settings.stt_wake_slice_ms))
            .replace("__STT_CMD_SLICE_MS__", str(settings.stt_cmd_slice_ms))
            .replace("__STT_CMD_SILENCE_MS__", str(settings.stt_cmd_silence_ms))
            .replace("__STT_CMD_MIN_VOICE_MS__", str(settings.stt_cmd_min_voice_ms))
        )
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
        )

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

    @app.get("/vr-control", response_class=HTMLResponse)
    def vr_control_page():
        """Pagina VR control per Quest 3: hand tracking + gamepad locomotion."""
        return VR_CONTROL_TEMPLATE

    @app.get("/local")
    @app.get("/listen")
    @app.get("/local-fetch")
    def _redir_client():
        return RedirectResponse(url="/client", status_code=302)


    @app.get("/api/health")
    def health():
        llm_prov = getattr(settings, "llm_provider", "openai")
        llm_model = (
            getattr(settings, "gemini_model", settings.llm_model)
            if llm_prov == "gemini"
            else settings.llm_model
        )
        vpid = None
        vlabel = None
        try:
            from talk_module.visitor_context import active_visitor_profile_id, get_active_visitor_profile

            vpid = active_visitor_profile_id() or None
            vprof = get_active_visitor_profile()
            vlabel = (vprof or {}).get("label") if vprof else None
        except Exception:
            pass
        return {
            "status": "ok",
            "host": "AI Accelerator",
            "visitor_profile": vpid,
            "visitor_label": vlabel,
            "llm_provider": llm_prov,
            "llm_model": llm_model,
            "llm_text_model": settings.llm_text_model,
            "stt_model": settings.stt_model,
            "wake_stt_model": settings.wake_stt_model,
            "tts_model": settings.tts_model,
            "tts_voice": settings.tts_voice,
        }

    @app.get("/api/network-info")
    def api_network_info(request: Request):
        """URL per aprire il client da PC/telefono sulla stessa rete (router o WiFi G1)."""
        ips = _collect_host_ips()
        public = (os.getenv("TALK_PUBLIC_HOST") or "").strip()
        scheme = request.url.scheme or "https"
        hosts: list[str] = []
        for h in [public, request.url.hostname or "", *ips]:
            h = (h or "").strip()
            if h and h not in hosts and h not in ("0.0.0.0", "127.0.0.1"):
                hosts.append(h)

        def _url(path: str, host: str | None = None) -> str:
            h = host or (request.url.hostname or "localhost")
            return f"{scheme}://{h}:8081{path}"

        current = str(request.base_url).rstrip("/")
        client_here = f"{current}/client"
        links = []
        for h in hosts:
            links.append(
                {
                    "host": h,
                    "client": _url("/client", h),
                    "occhi": _url("/client#occhi", h),
                    "dashboard": _url("/dashboard/", h),
                    "http_redirect": f"http://{h}:8080/client",
                }
            )
        return {
            "ok": True,
            "ips": ips,
            "talk_public_host": public or None,
            "client_url": client_here,
            "occhi_url": f"{client_here}#occhi",
            "dashboard_url": f"{current}/dashboard/",
            "links": links,
            "hint": "Stessa rete del robot/router: apri client_url (HTTPS). Microfono: accetta certificato al primo accesso.",
        }

    @app.get("/api/server-log")
    def api_server_log(lines: int = 80):
        """Ultime righe di /tmp/talk.log — visibili dal client HTTPS (senza watchdog :8082)."""
        rows = _tail_server_log(lines)
        return {"ok": True, "path": str(_SERVER_LOG_PATH), "lines": rows}

    @app.get("/api/wake-debug")
    def api_wake_debug_list():
        """Lista file audio wake debug con trascrizione (dal nome file)."""
        d = _WAKE_DEBUG_DIR
        if not d.exists():
            return {"files": []}
        files = sorted(d.glob("*.webm"), key=lambda p: p.stat().st_mtime, reverse=True)
        result = []
        for f in files[:_WAKE_DEBUG_MAX]:
            result.append({"name": f.name, "size": f.stat().st_size, "url": f"/api/wake-debug/{f.name}"})
        return {"files": result}

    @app.get("/api/wake-debug/{filename}")
    def api_wake_debug_file(filename: str):
        """Scarica un file audio wake debug."""
        from fastapi.responses import Response
        safe = Path(filename).name
        fp = _WAKE_DEBUG_DIR / safe
        if not fp.exists() or not fp.is_file():
            raise HTTPException(404, "File not found")
        return Response(content=fp.read_bytes(), media_type="audio/webm",
                        headers={"Content-Disposition": f'inline; filename="{safe}"'})

    _LED_EFFECT_MAP = {
        "rainbow": ("rainbow", (255, 180, 0), 1.2),
        "breathe_blue": ("breathe", (0, 120, 255), 1.0),
        "breathe_green": ("breathe", (0, 255, 80), 1.0),
        "breathe_red": ("breathe", (255, 40, 40), 1.0),
        "breathe_purple": ("breathe", (168, 85, 247), 1.0),
        "blink_red": ("blink", (255, 0, 0), 1.5),
        "blink_blue": ("blink", (0, 100, 255), 1.5),
        "solid_blue": ("solid", (0, 120, 255), 0),
        "solid_green": ("solid", (0, 255, 80), 0),
        "solid_red": ("solid", (255, 0, 0), 0),
        "solid_amber": ("solid", (255, 180, 0), 0),
        "solid_purple": ("solid", (168, 85, 247), 0),
        "solid_cyan": ("solid", (0, 220, 220), 0),
        "solid_white": ("solid", (255, 255, 255), 0),
    }

    def _fire_led_effect(effect_name: str) -> None:
        """Activate a named LED effect (used by soundboard)."""
        if not effect_name:
            return
        entry = _LED_EFFECT_MAP.get(effect_name)
        if not entry:
            return
        mode, color, speed = entry
        try:
            from talk_module.robot_actions import led_start_animation, led_stop_animation, set_led_color
            if mode == "solid":
                led_stop_animation()
                set_led_color(*color)
            else:
                led_start_animation(mode=mode, color=color, speed=speed)
        except Exception:
            pass

    @app.post("/api/led")
    def api_set_led(data: dict = Body(...)):
        """Set LED color or animation.
        {state: 'idle'|'listening'|'thinking'|'speaking'}
        {r, g, b} for solid color
        {animation: 'rainbow'|'breathe'|'blink', color: [r,g,b], speed: 1.0}
        """
        from talk_module.robot_actions import (
            set_led_color, led_start_animation, led_stop_animation,
            LED_LISTENING, LED_THINKING, LED_SPEAKING, LED_IDLE,
        )
        effect = (data.get("effect") or "").strip().lower()
        if effect and effect in _LED_EFFECT_MAP:
            _fire_led_effect(effect)
            return {"ok": True, "message": f"LED effect '{effect}' started"}
        anim = (data.get("animation") or "").strip().lower()
        if anim:
            color = tuple(data.get("color", [255, 180, 0]))[:3]
            speed = float(data.get("speed", 1.0))
            led_start_animation(mode=anim, color=color, speed=speed)
            return {"ok": True, "message": f"Animation '{anim}' started"}
        state = (data.get("state") or "").strip().lower()
        anim_states = {"thinking": ("rainbow", LED_THINKING, 1.2), "speaking": ("breathe", LED_SPEAKING, 1.0)}
        if state in anim_states:
            mode, color, speed = anim_states[state]
            led_start_animation(mode=mode, color=color, speed=speed)
            return {"ok": True, "message": f"LED {state} animation started"}
        led_stop_animation()
        presets = {"idle": LED_IDLE, "listening": LED_LISTENING}
        if state in presets:
            r, g, b = presets[state]
        else:
            r = int(data.get("r", 255))
            g = int(data.get("g", 255))
            b = int(data.get("b", 255))
        ok, msg = set_led_color(r, g, b)
        return {"ok": ok, "message": msg}

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
            "endpointing": {
                "cmd_silence_ms": settings.stt_cmd_silence_ms,
                "cmd_slice_ms": settings.stt_cmd_slice_ms,
                "wake_slice_ms": settings.stt_wake_slice_ms,
                "cmd_min_voice_ms": settings.stt_cmd_min_voice_ms,
                "listen_silence_sec": settings.stt_listen_silence_sec,
            },
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
            llm_prov = getattr(settings, "llm_provider", "openai")
            llm_model = (
                getattr(settings, "gemini_model", settings.llm_model)
                if llm_prov == "gemini"
                else settings.llm_model
            )
            return {
                "ok": True,
                "test_phrase": test_phrase,
                "transcribed": text.strip(),
                "llm_response": resp,
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "stt_provider": stt_used,
                "llm_provider": llm_prov,
                "llm_model": llm_model,
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
        """Lista gruppi pattern -> risposta dalla knowledge base."""
        groups = load_knowledge_groups()
        return {
            "path": str(KNOWLEDGE_PATH),
            "groups": groups,
            "entries": load_knowledge(),
        }

    @app.post("/api/knowledge/reload")
    def api_reload_knowledge():
        """Ricarica knowledge da file dopo modifica."""
        reload_knowledge()
        return {"ok": True, "groups": len(load_knowledge_groups()), "entries": len(load_knowledge())}

    @app.post("/api/knowledge/save")
    def api_save_knowledge(data: dict = Body(...)):
        """Salva knowledge su config/knowledge.json (gruppi o formato legacy flat)."""
        try:
            if data.get("groups") is not None:
                groups = save_knowledge_groups(data.get("groups") or [])
                return {"ok": True, "groups": len(groups), "entries": len(load_knowledge())}
            entries = data.get("entries") or {}
            groups = save_knowledge_entries(entries)
            return {"ok": True, "groups": len(groups), "entries": len(load_knowledge())}
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
                    "led_effect": "",
                    "teaching_slot": "",
                    "audio_delay_ms": 0,
                    "gesture_delay_ms": 0,
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
                        "led_effect": "",
                        "teaching_slot": "",
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
                s.setdefault("led_effect", "")
                s.setdefault("teaching_slot", "")
                s.setdefault("audio_delay_ms", 0)
                s.setdefault("gesture_delay_ms", 0)
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
                    "led_effect": "",
                    "teaching_slot": "",
                    "audio_delay_ms": 0,
                    "gesture_delay_ms": 0,
                }
                for i in range(n)
            ]

    def _soundboard_delay_ms(value, default: int = 0, max_ms: int = 15000) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, min(v, max_ms))

    def _normalize_soundboard_gesture_fields(robot_arm: str, teaching_slot: str) -> tuple[str, str]:
        """Client may send explore teachings as explore::name in robot_arm. One gesture type per slot."""
        arm = str(robot_arm or "").strip()
        teach = str(teaching_slot or "").strip()
        if arm.startswith("explore::"):
            teach = arm[9:].strip() or teach
            arm = ""
        if arm and teach:
            teach = ""
        elif teach:
            arm = ""
        return arm, teach

    def _soundboard_slot_robot_enabled(slot_idx: int, slot: dict) -> bool:
        """Audio zone (slot 0-49): play sound only. Robot zone (50+): fire configured actions."""
        if slot_idx < SOUNDBOARD_AUDIO_ZONE_COUNT:
            return False
        return bool(
            str(slot.get("robot_arm") or "").strip()
            or str(slot.get("robot_loco") or "").strip()
            or str(slot.get("led_effect") or "").strip()
            or str(slot.get("teaching_slot") or "").strip()
        )

    def _soundboard_slots_lite(slots: list[dict]) -> list[dict]:
        """Solo metadati per la griglia UI: evita ~20MB JSON su mobile (crash/OOM su /api/soundboard)."""
        lite: list[dict] = []
        for idx, s in enumerate(slots):
            ac = str(s.get("audio_base64_clean") or "")
            has_robot_cfg = bool(
                str(s.get("robot_arm") or "").strip()
                or str(s.get("robot_loco") or "").strip()
                or str(s.get("led_effect") or "").strip()
                or str(s.get("teaching_slot") or "").strip()
            )
            lite.append(
                {
                    "icon": s.get("icon"),
                    "text": s.get("text"),
                    "format": s.get("format") or "webm",
                    "format_clean": s.get("format_clean") or "mp3",
                    "has_robot": has_robot_cfg and idx >= SOUNDBOARD_AUDIO_ZONE_COUNT,
                    "has_clean": len(ac) > 50,
                    "robot_arm": str(s.get("robot_arm") or ""),
                    "robot_loco": str(s.get("robot_loco") or ""),
                    "led_effect": str(s.get("led_effect") or ""),
                    "teaching_slot": str(s.get("teaching_slot") or ""),
                    "audio_delay_ms": _soundboard_delay_ms(s.get("audio_delay_ms")),
                    "gesture_delay_ms": _soundboard_delay_ms(s.get("gesture_delay_ms")),
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
        ac = str(s.get("audio_base64_clean") or "")
        ar = str(s.get("audio_base64") or "")
        if len(ac) <= 50 and len(ar) > 50:
            ac = ar
            fc = str(s.get("format_clean") or s.get("format") or "mp3")
        else:
            fc = str(s.get("format_clean") or "mp3")
        return {
            "icon": s.get("icon"),
            "text": s.get("text"),
            "audio_base64": "",
            "format": fc if ac else str(s.get("format") or "webm"),
            "audio_base64_clean": ac,
            "format_clean": fc if ac else "mp3",
            "robot_arm": str(s.get("robot_arm") or ""),
            "robot_loco": str(s.get("robot_loco") or ""),
            "led_effect": str(s.get("led_effect") or ""),
            "teaching_slot": str(s.get("teaching_slot") or ""),
            "audio_delay_ms": _soundboard_delay_ms(s.get("audio_delay_ms")),
            "gesture_delay_ms": _soundboard_delay_ms(s.get("gesture_delay_ms")),
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
        """Salva slot: solo traccia clean (naturale). audio_base64 non viene più persistito."""
        global _soundboard_cache
        slot = int(data.get("slot", 0))
        if slot < 0 or slot >= SOUNDBOARD_SLOT_COUNT:
            return {"ok": False, "error": f"slot 0-{SOUNDBOARD_SLOT_COUNT - 1}"}
        na = str(data.get("audio_base64") or "").strip()
        slots = _load_soundboard()
        prev = slots[slot] if slot < len(slots) else {}
        clear_audio = data.get("clear_audio") in (True, "true", "1", 1)
        if clear_audio:
            clean_b64 = ""
            clean_fmt = str(data.get("format_clean") or prev.get("format_clean") or "mp3")
        elif "audio_base64_clean" in data:
            clean_b64 = str(data.get("audio_base64_clean") or "").strip()
            clean_fmt = str(data.get("format_clean", "mp3") or "mp3")
            if not clean_b64:
                clean_b64 = str(prev.get("audio_base64_clean") or "").strip()
                if clean_b64:
                    clean_fmt = str(prev.get("format_clean") or prev.get("format") or "mp3")
        else:
            clean_b64 = str(prev.get("audio_base64_clean") or "").strip()
            clean_fmt = str(prev.get("format_clean") or "mp3")
        if not clean_b64 and na:
            clean_b64 = na
            clean_fmt = str(data.get("format_clean") or data.get("format") or "mp3")
        txt = str(data.get("text", "")).strip()[:SOUNDBOARD_TEXT_MAX_LEN] or f"Comando {slot+1}"
        ra = data.get("robot_arm")
        rl = data.get("robot_loco")
        le = data.get("led_effect")
        ts = data.get("teaching_slot")
        if slot < SOUNDBOARD_AUDIO_ZONE_COUNT:
            ra, rl, le, ts = "", "", "", ""
        else:
            ra, ts = _normalize_soundboard_gesture_fields(
                str(ra if ra is not None else (prev.get("robot_arm") or "")),
                str(ts if ts is not None else (prev.get("teaching_slot") or "")),
            )
        slots[slot] = {
            "icon": str(data.get("icon", "🎤")).strip()[:20],
            "text": txt,
            "audio_base64": "",
            "format": clean_fmt if clean_b64 else str(prev.get("format_clean") or prev.get("format") or "mp3"),
            "audio_base64_clean": clean_b64,
            "format_clean": clean_fmt if clean_b64 else "mp3",
            "robot_arm": str(ra if ra is not None else (prev.get("robot_arm") or "")),
            "robot_loco": str(rl if rl is not None else (prev.get("robot_loco") or "")),
            "led_effect": str(le if le is not None else (prev.get("led_effect") or "")),
            "teaching_slot": str(ts if ts is not None else (prev.get("teaching_slot") or "")),
            "audio_delay_ms": _soundboard_delay_ms(
                data.get("audio_delay_ms") if data.get("audio_delay_ms") is not None else prev.get("audio_delay_ms", 0)
            ),
            "gesture_delay_ms": _soundboard_delay_ms(
                data.get("gesture_delay_ms") if data.get("gesture_delay_ms") is not None else prev.get("gesture_delay_ms", 0)
            ),
        }
        if slot >= SOUNDBOARD_AUDIO_ZONE_COUNT:
            slots[slot]["robot_arm"], slots[slot]["teaching_slot"] = _normalize_soundboard_gesture_fields(
                slots[slot]["robot_arm"],
                slots[slot]["teaching_slot"],
            )
        try:
            SOUNDBOARD_PATH.write_text(json.dumps({"slots": slots}, indent=2), encoding="utf-8")
            _soundboard_cache = (SOUNDBOARD_PATH.stat().st_mtime_ns, slots[: SOUNDBOARD_SLOT_COUNT])
            _write_soundboard_lite_sidecar(_soundboard_slots_lite(slots[: SOUNDBOARD_SLOT_COUNT]))
            try:
                from talk_module.audio.soundboard_pcm_cache import invalidate_slot_pcm

                invalidate_slot_pcm(slot)
            except Exception:
                pass
            return {"ok": True}
        except Exception as e:
            _invalidate_soundboard_cache()
            return {"ok": False, "error": str(e)}

    def _soundboard_ffmpeg_exe() -> Optional[str]:
        """ffmpeg: bundle imageio-ffmpeg in venv, poi PATH di sistema."""
        try:
            from talk_module.stt.audio_convert import _ffmpeg_candidates

            for ff in _ffmpeg_candidates():
                if ff:
                    return ff
        except Exception:
            pass
        return None

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
        ff = _soundboard_ffmpeg_exe()
        if not ff:
            raise HTTPException(503, "ffmpeg non trovato (installa imageio-ffmpeg o apt install ffmpeg)")
        try:
            r = subprocess.run(
                [
                    ff,
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
            raise HTTPException(503, "ffmpeg non trovato (installa imageio-ffmpeg o apt install ffmpeg)")
        finally:
            inp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    def _soundboard_wav_boost(wav_bytes: bytes) -> bytes:
        """Guadagno lineare su PCM (ffmpeg); alimiter evita clipping duro. SOUNDBOARD_PLAYBACK_GAIN in .env (default 1.32)."""
        try:
            g = float(os.getenv("SOUNDBOARD_PLAYBACK_GAIN", "1.32"))
        except ValueError:
            g = 1.32
        if g <= 1.02 or not wav_bytes or len(wav_bytes) < 100:
            return wav_bytes
        g = min(max(g, 1.0), 2.2)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fin:
            fin.write(wav_bytes)
            inp = Path(fin.name)
        out = inp.with_suffix(".sb_boost.wav")
        ff = _soundboard_ffmpeg_exe()
        if not ff:
            return wav_bytes
        try:
            af = f"volume={g:.4f},alimiter=limit=0.97"
            r = subprocess.run(
                [
                    ff,
                    "-y",
                    "-i",
                    str(inp),
                    "-af",
                    af,
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
            if r.returncode == 0 and out.exists() and out.stat().st_size > 100:
                return out.read_bytes()
            r2 = subprocess.run(
                [
                    ff,
                    "-y",
                    "-i",
                    str(inp),
                    "-af",
                    f"volume={g:.4f}",
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
            if r2.returncode == 0 and out.exists() and out.stat().st_size > 100:
                return out.read_bytes()
        except Exception:
            pass
        finally:
            inp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
        return wav_bytes

    def _play_tts_on_server(audio_bytes: bytes, fmt: str = "mp3", device_id: int | None = None) -> bool:
        """TTS sulla cassa interna G1 (come soundboard), fallback uscita ALSA Jetson."""
        if not audio_bytes or len(audio_bytes) < 30:
            return False
        from talk_module.audio.g1_speaker import play_wav_on_g1
        from talk_module.audio.player import AudioPlayer

        try:
            wav_g1 = _soundboard_bytes_to_wav_playable(audio_bytes, fmt)
            if play_wav_on_g1(wav_g1):
                return True
        except Exception as e:
            print(f"[tts-server] g1_internal: {e}", flush=True)
        try:
            p = AudioPlayer(device_id=device_id)
            return p.play_bytes(audio_bytes, format_hint="mp3")
        except Exception as e:
            print(f"[tts-server] player: {e}", flush=True)
        return False

    @app.post("/api/soundboard-play-local")
    def api_soundboard_play_local(data: dict = Body(...)):
        """Riproduce uno slot audio sulla cassa del Jetson (sounddevice + device_id dal setup)."""
        slot_idx = int(data.get("slot", -1))
        slots = _load_soundboard()
        if slot_idx < 0 or slot_idx >= len(slots):
            raise HTTPException(400, "Slot non valido")
        s = slots[slot_idx] or {}
        ac = str(s.get("audio_base64_clean") or "")
        ar = str(s.get("audio_base64") or "")
        if len(ac) > 50:
            b64, fmt = ac, str(s.get("format_clean") or "mp3")
        elif len(ar) > 50:
            b64, fmt = ar, str(s.get("format_clean") or s.get("format") or "mp3")
        else:
            raise HTTPException(400, "Slot senza audio")
        cfg = load_device_config()
        spk = cfg.get("speaker") or {}
        dev_id = None
        if spk.get("type") == "local" and spk.get("device_id") is not None:
            try:
                dev_id = int(spk.get("device_id"))
            except (TypeError, ValueError):
                dev_id = None
        # Se l'altoparlante è configurato come "browser/rete", usa comunque l'uscita di sistema (aplay/ffplay).
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "Base64 non valido")
        if len(raw) < 80:
            raise HTTPException(400, "Audio troppo corto")

        play_t0 = time.time()

        from talk_module.audio.player import AudioPlayer
        from talk_module.audio.g1_speaker import play_wav_on_g1
        from talk_module.audio.soundboard_pcm_cache import warmup_pcm
        from talk_module.speak_gestures import (
            begin_soundboard_gesture_schedule,
            fire_named_led_effect,
            play_soundboard_audio_scheduled,
            play_soundboard_slot_synced,
        )

        robot_enabled = _soundboard_slot_robot_enabled(slot_idx, s)
        arm, teach = _normalize_soundboard_gesture_fields(
            str(s.get("robot_arm") or ""),
            str(s.get("teaching_slot") or ""),
        )
        loco = str(s.get("robot_loco") or "").strip()
        led_fx = str(s.get("led_effect") or "").strip()
        audio_delay_ms = _soundboard_delay_ms(s.get("audio_delay_ms"))
        gesture_delay_ms = _soundboard_delay_ms(s.get("gesture_delay_ms"))
        use_scheduled = audio_delay_ms > 0 or gesture_delay_ms > 0
        has_motion = bool(arm or loco or teach)

        warmup_pcm(slot_idx, raw, fmt)

        backend = "g1_internal"
        print(
            f"[soundboard-play-local] slot={slot_idx} arm={arm!r} teach={teach!r} "
            f"robot_enabled={robot_enabled} audio_delay={audio_delay_ms} gesture_delay={gesture_delay_ms}",
            flush=True,
        )

        if robot_enabled and led_fx:
            fire_named_led_effect(led_fx)
        if robot_enabled and use_scheduled and has_motion:
            begin_soundboard_gesture_schedule(
                play_t0=play_t0,
                gesture_delay_ms=gesture_delay_ms,
                robot_arm=arm,
                robot_loco=loco,
                teaching_slot=teach,
                tag="soundboard-play-local",
            )

        with _soundboard_local_play_lock:
            ok = False
            if robot_enabled:
                if use_scheduled:
                    ok = play_soundboard_audio_scheduled(
                        raw,
                        fmt,
                        play_t0=play_t0,
                        audio_delay_ms=audio_delay_ms,
                        slot_idx=slot_idx,
                        tag="soundboard-play-local",
                    )
                else:
                    try:
                        wav_g1 = _soundboard_bytes_to_wav_playable(raw, fmt)
                    except Exception as e:
                        raise HTTPException(500, f"Decodifica audio: {e!s}")
                    ok = play_soundboard_slot_synced(
                        wav_g1,
                        robot_arm=arm,
                        robot_loco=loco,
                        led_effect="",
                        teaching_slot=teach,
                        tag="soundboard-play-local",
                    )
            else:
                if use_scheduled:
                    ok = play_soundboard_audio_scheduled(
                        raw,
                        fmt,
                        play_t0=play_t0,
                        audio_delay_ms=audio_delay_ms,
                        slot_idx=slot_idx,
                        tag="soundboard-play-local",
                    )
                else:
                    try:
                        wav_g1 = _soundboard_bytes_to_wav_playable(raw, fmt)
                        wav_bytes = _soundboard_wav_boost(wav_g1)
                    except HTTPException:
                        raise
                    except Exception as e:
                        raise HTTPException(500, f"Decodifica audio: {e!s}")
                    ok = play_wav_on_g1(wav_g1)
            if not ok:
                try:
                    wav_fb = _soundboard_wav_boost(_soundboard_bytes_to_wav_playable(raw, fmt))
                except Exception:
                    wav_fb = None
                if wav_fb:
                    p = AudioPlayer(device_id=dev_id)
                    ok = p.play_bytes(wav_fb, format_hint="wav")
                    backend = "local"
            if not ok:
                raise HTTPException(
                    500,
                    "Riproduzione fallita (cassa interna G1 e uscite Jetson).",
                )
        return {"ok": True, "backend": backend}

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
                "audio_base64": "",
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

    @app.get("/api/robot-fsm")
    def api_robot_fsm():
        """Stato sport mode / FSM (diagnostica gesti braccia)."""
        try:
            from talk_module.robot_actions import probe_g1_sport_status
            robot_ip = os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
            st = probe_g1_sport_status(robot_ip=robot_ip)
            return {"ok": True, **st}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.get("/api/robot-unitree-teachings")
    def api_robot_unitree_teachings():
        """Compat: elenco teach Explore app sul robot."""
        from talk_module.explore_teaching import list_explore_teachings

        data = list_explore_teachings()
        return {
            "ok": data.get("ok", False),
            "custom": data.get("teachings") or [],
            "preset": data.get("presets") or [],
            "error": data.get("error") or "",
        }

    @app.post("/api/robot-unitree-teachings/play")
    def api_robot_unitree_teaching_play(data: dict = Body(...)):
        """Compat: riproduce teach Explore per nome."""
        from talk_module.explore_teaching import play_explore_teaching

        name = str(data.get("name") or data.get("action_name") or "").strip()
        robot_ip = data.get("robot_ip") or os.getenv("UNITREE_ROBOT_IP", "192.168.123.161")
        if not name:
            raise HTTPException(400, "name richiesto")
        result = play_explore_teaching(name, robot_ip=robot_ip)
        return {"ok": result.get("ok"), "message": result.get("message"), "name": name}

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
                le = (getattr(rm, "led_effect", None) or "").strip()
                if le:
                    _fire_led_effect(le)
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

    _wake_cooldown = [0.0]
    WAKE_COOLDOWN_S = 1.0

    def _build_wake_ack_response(raw_text: str, t0: float) -> dict:
        """Solo Hey G1: saluto visitatore (se profilo attivo) + TTS, poi command mode lato client."""
        from talk_module.visitor_context import get_hey_g1_ack_response

        resp = get_hey_g1_ack_response()
        audio_b64 = ""
        try:
            _, _, tts, _, _ = get_services()
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            if audio_out:
                audio_b64 = base64.b64encode(audio_out).decode()
                from talk_module.speak_gestures import start_talk_gesture
                start_talk_gesture("buongiorno", resp or "", had_robot_match=False)
        except Exception as e:
            print(f"[Wake] ack TTS error: {e}", flush=True)
        _wake_cooldown[0] = time.time() + WAKE_COOLDOWN_S
        return {
            "text": raw_text,
            "response": resp,
            "audio_base64": audio_b64,
            "message": "",
            "wake_ack": True,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
        }

    def _stt_and_wake_check(audio_bytes: bytes, format_hint: str = "webm") -> dict:
        """STT + wake word detection.
        - wkind 'ack' (solo wake word) → wake_ack (client entra in command mode)
        - wkind 'ok' (wake + comando nello stesso slice) → processa subito il comando
        Uses a dedicated STT prompt biased toward recognising "Hey G1".
        Server-side cooldown: ignores wake for WAKE_COOLDOWN_S after last response."""
        t0 = time.perf_counter()
        _ms = lambda: int((time.perf_counter() - t0) * 1000)
        if time.time() < _wake_cooldown[0]:
            return {"text": "", "response": "", "audio_base64": "", "message": "", "wake_miss": True, "duration_ms": _ms()}
        try:
            _save_sample_audio(audio_bytes)
            stt, _, _, _, _ = get_services()
            raw_text = stt.transcribe(
                audio_bytes,
                format_hint=format_hint,
                language="it",
                prompt=WAKE_STT_PROMPT,
                model=settings.wake_stt_model,
            )
            if not raw_text or not raw_text.strip():
                _save_wake_debug_audio(audio_bytes, "", "silence")
                return {"text": raw_text or "", "response": "", "audio_base64": "", "message": "", "wake_miss": True, "duration_ms": _ms()}
            rest, wkind = find_wake_and_rest(raw_text)
            _save_wake_debug_audio(audio_bytes, raw_text.strip(), wkind)
            print(f"[Wake] raw={raw_text!r} kind={wkind} rest={rest!r} ({_ms()}ms)", flush=True)
            if wkind == "miss":
                return {"text": raw_text, "response": "", "audio_base64": "", "message": "", "wake_miss": True, "duration_ms": _ms()}
            if wkind == "ok" and rest and rest.strip():
                # Raw STT for routing (same as /api/text-chat); fuzzy was steering wrong KB entries.
                prompt = rest.strip()
                print(f"[Wake] comando inline: {prompt!r}", flush=True)
                result = _process_after_wake(prompt, raw_text, t0)
                result["wake_cmd_inline"] = True
                _wake_cooldown[0] = time.time() + WAKE_COOLDOWN_S
                return result
            return _build_wake_ack_response(raw_text, t0)
        except Exception as e:
            print(f"[Wake] STT error: {e}", flush=True)
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}", "duration_ms": _ms()}

    def _process_after_wake(prompt: str, raw_text: str, t0: float) -> dict:
        from talk_module.processing import process_after_wake
        return process_after_wake(prompt, raw_text, t0, get_services, check_knowledge, _run_robot_match_actions)

    def _process_audio(audio_bytes: bytes, skip_wake_word: bool = False, format_hint: str = "webm") -> dict:
        """Pipeline: audio -> STT -> LLM -> TTS. skip_wake_word=True per pulsante Parla."""
        t0 = time.perf_counter()
        try:
            _save_sample_audio(audio_bytes)  # Salva ultimo campione per riuso (Test con campione)
            stt, llm, tts, _, _ = get_services()
            raw_text = stt.transcribe(
                audio_bytes,
                format_hint=format_hint,
                language="it",
                prompt=settings.whisper_prompt,
            )
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
                rest, wkind = find_wake_and_rest(raw_text)
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
                    return _build_wake_ack_response(raw_text, t0)
                # Raw STT for routing (same as /api/text-chat); fuzzy was steering wrong KB entries.
                prompt = (rest or "").strip()
                text = raw_text
            else:
                text = raw_text or ""
                prompt, msg = _extract_prompt(text, skip_wake_word=True, audio_size=len(audio_bytes))
                if msg:
                    return {"text": text or "", "response": "", "audio_base64": "", "message": msg, "duration_ms": int((time.perf_counter() - t0) * 1000)}
            if prompt == PROMPT_HEY_G1_ACK_ONLY:
                from talk_module.visitor_context import get_hey_g1_ack_response

                resp = get_hey_g1_ack_response()
                robot_match = None
            else:
                from talk_module.processing import route_user_prompt

                resp, robot_match = route_user_prompt(
                    prompt,
                    llm,
                    voice=True,
                    run_robot_match_fn=_run_robot_match_actions,
                )
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            if audio_out and not robot_match:
                from talk_module.speak_gestures import start_talk_gesture
                start_talk_gesture(prompt or "", resp or "", had_robot_match=False)
            if resp:
                _wake_cooldown[0] = time.time() + WAKE_COOLDOWN_S
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
            from talk_module.processing import route_user_prompt

            resp, robot_match = route_user_prompt(
                prompt.strip(),
                llm,
                voice=False,
                run_robot_match_fn=_run_robot_match_actions,
                text_model=settings.llm_text_model,
                max_tokens=384,
            )
            audio_out = tts.synthesize(resp, format="mp3") if resp else b""
            if audio_out and not robot_match:
                from talk_module.speak_gestures import start_talk_gesture
                start_talk_gesture(prompt.strip(), resp or "", had_robot_match=False)
            return {
                "text": prompt.strip(),
                "response": resp or "",
                "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
                "robot_matched": bool(robot_match),
                "robot_action": (robot_match.arm_action or "") if robot_match else "",
                "robot_loco": (robot_match.loco_command or "") if robot_match else "",
                "duration_ms": int((time.perf_counter() - t0) * 1000),
            }
        except Exception as e:
            return {"text": "", "response": "", "audio_base64": "", "message": f"Errore: {e}", "duration_ms": int((time.perf_counter() - t0) * 1000)}

    @app.post("/api/text-chat")
    def api_text_chat(body: dict = Body(...)):
        """Testo → routing knowledge (config/knowledge.json) → azioni robot → LLM+TTS."""
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "Testo vuoto")
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
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, partial(_process_audio, audio_bytes, True, "webm"))

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
                            if play_on == "server" and result.get("audio_base64"):
                                try:
                                    dev_id = int(out_device_id) if out_device_id is not None else None
                                    ab = base64.b64decode(result["audio_base64"])
                                    await loop.run_in_executor(
                                        _executor,
                                        partial(_play_tts_on_server, ab, "mp3", dev_id),
                                    )
                                except Exception as e:
                                    print(f"[ws/audio] server tts play: {e}", flush=True)
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
                    silence_seconds=settings.stt_listen_silence_sec,
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
                result = await loop.run_in_executor(_executor, partial(_process_audio, item, False, "wav"))
                await ws.send_text(json.dumps({"type": "response", "data": result}))
                if result.get("audio_base64"):
                    import base64 as b64

                    ab = b64.b64decode(result["audio_base64"])
                    spk_dev = spk.get("device_id") if play_local else None
                    dev_id = int(spk_dev) if spk_dev is not None and spk_dev != "" else None
                    await loop.run_in_executor(
                        _executor,
                        partial(_play_tts_on_server, ab, "mp3", dev_id),
                    )
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
                    result = await loop.run_in_executor(_executor, partial(_process_audio, item, True, "wav"))
                    await ws.send_text(json.dumps({"type": "response", "data": result}))
                    if result.get("audio_base64") and spk.get("type") == "local":
                        import base64 as b64

                        ab = b64.b64decode(result["audio_base64"])
                        spk_dev = spk.get("device_id")
                        dev_id = int(spk_dev) if spk_dev is not None and spk_dev != "" else None
                        await loop.run_in_executor(
                            _executor,
                            partial(_play_tts_on_server, ab, "mp3", dev_id),
                        )
                    # type network: il browser (/local) riproduce audio_base64 (telefono -> cassa BT)
                    recording = False
        except WebSocketDisconnect:
            pass
        finally:
            ptt_stop.set()

    @app.get("/api/grok-voice/status")
    def api_grok_voice_status():
        from talk_module.grok_voice import grok_voice_configured

        return {
            "ok": True,
            "configured": grok_voice_configured(),
            "agent_id": settings.xai_agent_id or "",
        }

    @app.websocket("/ws/grok-voice")
    async def websocket_grok_voice(ws: WebSocket):
        from talk_module.grok_voice import grok_voice_configured, proxy_grok_voice

        await ws.accept()
        if not grok_voice_configured():
            await ws.send_text(
                json.dumps({"type": "error", "error": "Grok voice non configurato (XAI_API_KEY in .env)"})
            )
            await ws.close()
            return
        try:
            local_mic = ws.query_params.get("input") == "jetson"
            try:
                input_gain = float(ws.query_params.get("gain") or "1")
            except ValueError:
                input_gain = 1.0
            try:
                threshold = int(ws.query_params.get("threshold") or "20")
            except ValueError:
                threshold = 20
            await proxy_grok_voice(
                ws,
                local_mic=local_mic,
                input_gain=input_gain,
                threshold=threshold,
            )
        except WebSocketDisconnect:
            pass

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
            except OSError as portaudio_error:
                # Jetson minimale: ALSA/PulseAudio funziona anche se libportaudio non è installata.
                from array import array
                import math

                process = None
                try:
                    from talk_module.audio.device_utils import ensure_pulse_usb_microphone_source

                    source_name = ensure_pulse_usb_microphone_source()
                    if not source_name:
                        raise RuntimeError("Sorgente PulseAudio DJI non disponibile")
                    process = subprocess.Popen(
                        [
                            "arecord", "-q", "-D", "pulse", "-t", "raw",
                            "-f", "S16_LE", "-r", "24000", "-c", "1",
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    level_queue.put(
                        {"type": "info", "name": mic.get("name") or "DJI Mic (USB Jetson)",
                         "rate": 24000, "device": mic.get("device_id", 0)}
                    )
                    while not level_stop.is_set():
                        raw = process.stdout.read(3840)
                        if not raw:
                            err = process.stderr.read().decode("utf-8", errors="replace").strip()
                            raise RuntimeError(err or str(portaudio_error))
                        usable = len(raw) - (len(raw) % 2)
                        samples = array("h")
                        samples.frombytes(raw[:usable])
                        if not samples:
                            continue
                        peak = max(abs(sample) for sample in samples) / 32768.0
                        rms = math.sqrt(
                            sum(float(sample) * float(sample) for sample in samples) / len(samples)
                        ) / 32768.0
                        db = max(-60.0, 20 * math.log10(rms + 1e-10))
                        level_queue.put(
                            {"type": "level", "rms": round(rms, 5),
                             "peak": round(peak, 5), "db": round(db, 1)}
                        )
                except Exception as fallback_error:
                    level_queue.put({"error": str(fallback_error)})
                finally:
                    if process and process.poll() is None:
                        process.terminate()
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

    _mobile_app_www = Path(__file__).resolve().parent.parent / "mobile-app" / "www"
    if _mobile_app_www.is_dir():
        from fastapi.staticfiles import StaticFiles

        @app.get("/remote")
        def remote_dashboard_redirect():
            return RedirectResponse(url="/dashboard/", status_code=302)

        app.mount(
            "/dashboard",
            StaticFiles(directory=str(_mobile_app_www), html=True),
            name="g1_dashboard",
        )


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
    <p style="margin:8px 0;"><a href="/robot-control" style="display:inline-block;padding:12px 20px;background:rgba(99,102,241,0.2);color:#a5b4fc;border-radius:8px;text-decoration:none;font-weight:600;">Robot Control</a> <span class="hint">— Joystick + gesti braccia G1 + Teaching (192.168.123.161).</span></p>
    <p style="margin:8px 0;"><a href="/vr-control" style="display:inline-block;padding:12px 20px;background:rgba(167,139,250,0.2);color:#c4b5fd;border-radius:8px;text-decoration:none;font-weight:600;">VR Control</a> <span class="hint">— Apri dal Quest 3: hand tracking braccia + controller locomotion.</span></p>
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
      overflow-x: hidden;
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
      z-index: 10005;
      isolation: isolate;
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
      left: max(0px, calc((100vw - min(420px, 100vw)) / 2)) !important;
      right: auto !important;
      transform: none !important;
      width: min(280px, 85vw);
      max-width: 280px;
      height: 100vh;
      height: 100dvh;
      box-sizing: border-box;
      background: linear-gradient(180deg, #0f1117 0%, #141922 100%);
      border-right: 1px solid rgba(255,255,255,0.08);
      box-shadow: 4px 0 28px rgba(0,0,0,0.55);
      z-index: 99999;
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
    .sidebar-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 0 16px 14px;
      margin-bottom: 4px;
      border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .sidebar-head span { font-size: 14px; font-weight: 600; color: #e4e4e7; }
    .sidebar-close {
      width: 40px;
      height: 40px;
      border: none;
      border-radius: 10px;
      background: rgba(255,255,255,0.08);
      color: #e4e4e7;
      font-size: 22px;
      line-height: 1;
      cursor: pointer;
      flex-shrink: 0;
    }
    .sidebar-close:hover { background: rgba(239,68,68,0.2); color: #fca5a5; }
    .overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.55);
      z-index: 99998;
      display: none;
      opacity: 0;
      transition: opacity 0.25s;
      pointer-events: none;
    }
    body.g1-drawer-open { overflow: hidden; }
    body.g1-drawer-open .header { z-index: 9990 !important; }
    body.g1-drawer-open .main-content { pointer-events: none; }
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
      left: 0 !important;
      right: auto !important;
      transform: none !important;
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
    .robot-panel-tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
    .robot-panel-tab {
      padding: 8px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1);
      background: rgba(255,255,255,0.05); color: #a1a1aa; font-size: 12px; font-weight: 600;
      cursor: pointer; font-family: inherit;
    }
    .robot-panel-tab.active { background: rgba(99,102,241,0.18); border-color: rgba(99,102,241,0.45); color: #c4b5fd; }
    #robotPanelUnitreeTeach { flex: 1; display: flex; flex-direction: column; min-height: 0; }
    .unitree-teach-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
    .unitree-teach-bar .hint { margin: 0; font-size: 11px; color: #71717a; flex: 1; min-width: 160px; }
    .unitree-teach-bar button {
      padding: 7px 12px; border-radius: 8px; border: 1px solid rgba(20,184,166,0.35);
      background: rgba(20,184,166,0.12); color: #5eead4; font-size: 11px; font-weight: 600; cursor: pointer;
    }
    #unitreeTeachList {
      flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 5px;
      max-height: min(52vh, 420px); padding-right: 2px;
    }
    .unitree-teach-item {
      display: flex; align-items: center; gap: 8px; padding: 8px 10px;
      background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;
    }
    .unitree-teach-item .ut-name { flex: 1; font-size: 12px; color: #e4e4e7; word-break: break-word; }
    .unitree-teach-item .ut-dur { font-size: 10px; color: #71717a; font-family: monospace; min-width: 36px; text-align: right; }
    .unitree-teach-item button {
      padding: 5px 10px; border-radius: 6px; border: 1px solid rgba(20,184,166,0.4);
      background: rgba(20,184,166,0.15); color: #5eead4; font-size: 10px; font-weight: 700; cursor: pointer;
    }
    .explore-teach-steps { margin: 0 0 14px; padding: 12px 14px; border-radius: 10px; background: rgba(20,184,166,0.06); border: 1px solid rgba(20,184,166,0.18); font-size: 12px; color: #a1a1aa; line-height: 1.5; }
    .explore-teach-steps strong { color: #5eead4; }
    .explore-teach-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
    .explore-teach-bar button { padding: 8px 14px; background: rgba(20,184,166,0.15); color: #14b8a6; border: 1px solid rgba(20,184,166,0.35); border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600; }
    .explore-teach-bar button.secondary { background: rgba(255,255,255,0.06); color: #d4d4d8; border-color: rgba(255,255,255,0.12); }
    #exploreTeachList { display: flex; flex-direction: column; gap: 8px; max-height: min(52vh, 420px); overflow-y: auto; }
    .explore-teach-item { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 10px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); }
    .explore-teach-item .et-name { flex: 1; font-size: 13px; color: #e4e4e7; word-break: break-word; }
    .explore-teach-item .et-dur { font-size: 11px; color: #71717a; font-family: monospace; min-width: 40px; text-align: right; }
    .explore-teach-item button { padding: 8px 14px; background: #14b8a6; color: #0c0e14; border: none; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 700; }
    #robotControlFrame { flex: 1; width: 100%; min-height: 360px; border: 0; border-radius: 12px; background: #0f1115; }
    .client-camera-wrap { position: relative; background: #0f1115; border-radius: 12px; overflow: hidden; aspect-ratio: 4/3; max-height: min(52vh, 420px); border: 1px solid rgba(255,255,255,0.08); margin-bottom: 12px; }
    .client-camera-wrap img { width: 100%; height: 100%; object-fit: contain; display: none; background: #000; }
    .client-camera-placeholder { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #71717a; font-size: 13px; text-align: center; padding: 16px; }
    .client-cam-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 12px; font-size: 12px; margin-bottom: 10px; }
    .client-cam-meta span.lbl { color: #71717a; }
    .client-cam-meta span.val { color: #e4e4e7; font-family: monospace; text-align: right; }
    .client-cam-meta span.val.ok { color: #4ade80; }
    .client-cam-meta span.val.err { color: #f87171; }
    .client-cam-dets { font-size: 11px; color: #a1a1aa; min-height: 2.4em; line-height: 1.4; padding: 8px 10px; background: rgba(255,255,255,0.03); border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); }
    .client-cam-btns { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .client-cam-btns button { padding: 10px 16px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.12); background: rgba(255,255,255,0.06); color: #e4e4e7; font-size: 13px; font-weight: 600; cursor: pointer; }
    .client-cam-btns button:disabled { opacity: 0.35; cursor: not-allowed; }
    .client-cam-btns button.primary { background: rgba(20,184,166,0.2); border-color: rgba(20,184,166,0.45); color: #5eead4; }
    .client-cam-vision-toggle {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 10px;
      padding: 10px 12px;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 10px;
      font-size: 13px;
      color: #d4d4d8;
      cursor: pointer;
      user-select: none;
    }
    .client-cam-vision-toggle input { width: 16px; height: 16px; accent-color: #14b8a6; cursor: pointer; }
    .client-log-box { margin-top: 8px; padding: 10px 12px; background: #0a0b10; border: 1px solid rgba(255,255,255,0.08); border-radius: 10px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; line-height: 1.45; color: #a1a1aa; max-height: min(58vh, 480px); overflow: auto; white-space: pre-wrap; word-break: break-word; }
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
      .soundboard-grid { grid-template-columns: repeat(6, 1fr) !important; gap: 4px !important; }
      #sbModal > div { max-width: 100%; margin: env(safe-area-inset-top) 12px env(safe-area-inset-bottom); padding: 20px; }
      #sbModal button, #sbModal label { min-height: 44px; padding: 12px 16px; display: inline-flex; align-items: center; justify-content: center; }
      #sbModal input[type="range"] { min-height: 36px; }
      #textInput, #btnText { min-height: 48px !important; }
      .result { padding: 14px; font-size: 14px; }
    }
    @media (max-width: 400px) {
      .soundboard-grid { grid-template-columns: repeat(5, 1fr) !important; }
    }
    @media (max-width: 360px) {
      .soundboard-grid { grid-template-columns: repeat(4, 1fr) !important; gap: 4px !important; }
    }
    @media (min-width: 900px) {
      .soundboard-grid { grid-template-columns: repeat(8, 1fr) !important; }
    }
    @media (orientation: landscape) {
      body { max-width: 100%; }
      .header { max-width: 100%; padding-top: max(8px, env(safe-area-inset-top, 0px)); padding-bottom: 8px; }
      .main-content { padding-top: calc(16px + 4.5rem + env(safe-area-inset-top, 0px)); }
      .soundboard-grid { grid-template-columns: repeat(8, 1fr) !important; gap: 3px !important; }
      .soundboard-grid [role="button"] { min-height: 46px; }
      .btn { width: 120px; height: 120px; font-size: 13px; margin: 12px auto; }
    }
    @media (orientation: landscape) and (max-height: 500px) {
      .header { padding: 6px 16px; padding-top: max(6px, env(safe-area-inset-top)); }
      .header h1 { font-size: 1rem; }
      .main-content { padding-top: calc(12px + 3.5rem + env(safe-area-inset-top, 0px)); padding-left: max(16px, env(safe-area-inset-left)); padding-right: max(16px, env(safe-area-inset-right)); }
      .sidebar { padding-top: max(12px, env(safe-area-inset-top)); }
      .sidebar nav a { padding: 10px 16px; font-size: 14px; }
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
    #section-soundboard.section.active {
      display: flex !important;
      flex-direction: column;
      min-height: calc(100dvh - 5.5rem - env(safe-area-inset-top, 0px));
    }
    #section-soundboard h2 { font-size: 1rem; margin: 0 0 6px; }
    #section-soundboard .sb-controls { margin-bottom: 6px !important; }
    #soundboardScroll { overflow: visible; max-height: none; margin-bottom: 0; flex: 1; }
    .soundboard-grid {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 4px;
      align-content: start;
    }
    .soundboard-grid [role="button"] {
      min-height: 52px;
      padding: 3px 2px 12px;
      border-radius: 7px;
      touch-action: manipulation;
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.08);
    }
    .soundboard-grid [role="button"].sb-slot-filled-teal {
      background: rgba(20,184,166,0.08);
      border: 2px solid #14b8a6;
    }
    .soundboard-grid [role="button"].sb-slot-filled-purple {
      background: rgba(139,92,246,0.12);
      border: 2px solid #a78bfa;
    }
    .soundboard-grid [role="button"] .sb-slot-icon { font-size: 16px; margin-bottom: 1px; line-height: 1; }
    .soundboard-grid [role="button"] .sb-slot-text { font-size: 8.5px; line-height: 1.1; -webkit-line-clamp: 2; color: #9ca3af; text-align: center; max-width: 100%; pointer-events: none; }
    .soundboard-grid [role="button"] .sb-slot-edit {
      position: absolute;
      bottom: 2px;
      right: 2px;
      margin: 0;
      padding: 1px 3px;
      font-size: 9px;
      line-height: 1;
      min-height: 0;
      border-radius: 4px;
      opacity: 0.75;
      background: rgba(255,255,255,0.1);
      color: #9ca3af;
      border: none;
      cursor: pointer;
      touch-action: manipulation;
    }
  .sb-zone-head {
    font-size: 10px;
    font-weight: 600;
    color: #71717a;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin: 0 0 5px;
  }
  .sb-zone-head.sb-zone-robot { color: #a78bfa; margin-top: 2px; }
  .sb-divider {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 10px 0 8px;
    color: #52525b;
    font-size: 9px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .sb-divider::before,
  .sb-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: rgba(255,255,255,0.12);
  }
    #soundboardScroll, .soundboard-grid, .soundboard-grid [role="button"] {
      pointer-events: auto !important;
    }
    .soundboard-grid [role="button"] { touch-action: manipulation; }
    .sb-slot-text { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; line-height: 1.15; word-break: break-word; }
    #sbModalText { width: 100%; min-height: 72px; padding: 10px; margin-top: 4px; background: #27272a; border: 1px solid #3f3f46; border-radius: 8px; color: #fff; font-family: inherit; font-size: 13px; resize: vertical; }
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
    /* Tab sezioni rimosse: navigazione solo da menu ☰ laterale. */
    .client-section-tabs {
      display: none !important;
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
    .talk-page-title { margin: 0; font-size: 1.3rem; color: #f4f4f5; }
    .talk-page-subtitle { margin: 5px 0 14px; color: #71717a; font-size: 12px; line-height: 1.45; }
    .talk-global-settings {
      margin: 0 0 14px;
      border: 1px solid rgba(59,130,246,0.25);
      border-radius: 12px;
      background: rgba(59,130,246,0.055);
      overflow: hidden;
    }
    .talk-global-settings > summary {
      padding: 12px 14px;
      cursor: pointer;
      color: #93c5fd;
      font-size: 13px;
      font-weight: 700;
      user-select: none;
    }
    .talk-global-settings > summary span { color: #71717a; font-size: 10px; font-weight: 400; margin-left: 6px; }
    .talk-global-settings-body { padding: 0 12px 12px; }
    .talk-active-device {
      padding: 7px 10px;
      border-radius: 7px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
      background: rgba(255,255,255,0.035);
      margin-bottom: 8px;
    }
    .talk-active-device #activeMicDot { width: 8px; height: 8px; border-radius: 50%; background: #71717a; flex-shrink: 0; }
    .talk-active-device #activeMicLabel { color: #9ca3af; }
    .talk-device-grid { margin: 0 0 10px !important; padding: 10px !important; }
    .talk-device-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .talk-device-row label, .talk-output-mode label { display: block; margin-bottom: 5px; font-size: 11px; color: #9ca3af; }
    .talk-device-row select { width: 100%; min-width: 0; }
    .talk-sensitivity { padding: 10px; border-radius: 9px; background: rgba(255,255,255,0.025); }
    .talk-level-track { position: relative; height: 20px; background: #1e1e2e; border-radius: 8px; overflow: hidden; border: 1px solid rgba(255,255,255,0.08); }
    #parlaPreviewThresholdLine { position: absolute; top: 0; bottom: 0; width: 3px; background: #f97316; z-index: 3; opacity: .9; left: 4%; box-shadow: 0 0 4px #f97316; }
    #parlaPreviewBarFill { position: relative; height: 100%; width: 0; background: linear-gradient(90deg,#22c55e,#84cc16,#eab308); border-radius: 8px; transition: width .06s linear; z-index: 1; }
    .talk-level-meta { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; gap: 6px; flex-wrap: wrap; }
    #parlaPreviewStatus { font-size: 11px; color: #a1a1aa; font-family: monospace; }
    #parlaPreviewGate { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; background: #27272a; color: #71717a; }
    .talk-slider-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; margin-top: 12px; }
    .talk-slider-grid label { color: #9ca3af; font-size: 11px; }
    .talk-slider-grid strong { color: #e4e4e7; float: right; }
    .talk-slider-grid input { display: block; width: 100%; margin-top: 6px; accent-color: #60a5fa; }
    .talk-output-mode { margin-top: 10px; padding: 9px 10px; border-radius: 8px; background: rgba(255,255,255,0.025); }
    .talk-agent-stack { display: flex; flex-direction: column; gap: 10px; }
    .talk-agent-card {
      border-radius: 13px;
      border: 1px solid rgba(255,255,255,0.1);
      overflow: hidden;
      transition: border-color .18s, background .18s;
    }
    .talk-agent-grok { background: rgba(139,92,246,.07); border-color: rgba(139,92,246,.25); }
    .talk-agent-legacy { background: rgba(20,184,166,.055); border-color: rgba(20,184,166,.2); }
    .talk-agent-card.is-active.talk-agent-grok { border-color: rgba(167,139,250,.7); background: rgba(139,92,246,.12); }
    .talk-agent-card.is-active.talk-agent-legacy { border-color: rgba(45,212,191,.65); background: rgba(20,184,166,.1); }
    .talk-agent-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 13px 14px; }
    .talk-agent-head h3 { margin: 0; font-size: 15px; color: #e4e4e7; }
    .talk-agent-grok .talk-agent-head h3 { color: #c4b5fd; }
    .talk-agent-legacy .talk-agent-head h3 { color: #5eead4; }
    .talk-agent-head p { margin: 3px 0 0; font-size: 10px; color: #71717a; }
    .talk-agent-toggle { display: flex; align-items: center; gap: 7px; cursor: pointer; flex-shrink: 0; }
    .talk-agent-toggle span { font-size: 10px; color: #9ca3af; font-weight: 700; }
    .talk-agent-toggle input { width: 22px; height: 22px; margin: 0; accent-color: #a78bfa; }
    .talk-agent-legacy .talk-agent-toggle input { accent-color: #14b8a6; }
    .talk-agent-body { padding: 0 14px 14px; max-height: 1200px; opacity: 1; overflow: hidden; transition: max-height .22s ease, opacity .16s ease, padding .22s ease; }
    .talk-agent-card.is-collapsed .talk-agent-body { max-height: 0; opacity: 0; padding-top: 0; padding-bottom: 0; }
    .talk-agent-status { margin: 0 0 8px; color: #a1a1aa; font-size: 12px; }
    .talk-transcript { margin: 0; min-height: 3em; padding: 10px; border-radius: 8px; background: rgba(0,0,0,.16); color: #d4d4d8; font-size: 12px; line-height: 1.45; }
    .talk-text-question { margin-top: 12px; }
    .talk-text-question h4 { margin: 0 0 7px; color: #d4d4d8; font-size: 12px; }
    .talk-text-question > div { display: flex; gap: 7px; flex-wrap: wrap; }
    .talk-text-question input { flex: 1; min-width: 170px; padding: 9px 11px; background: #27272a; border: 1px solid #3f3f46; border-radius: 8px; color: #e4e4e7; }
    .talk-text-question button { padding: 9px 16px; border: none; border-radius: 8px; background: #14b8a6; color: #0c0e14; font-weight: 700; cursor: pointer; }
    @media (max-width: 640px) {
      .talk-device-row, .talk-slider-grid { grid-template-columns: 1fr; }
      .talk-global-settings > summary span { display: block; margin: 3px 0 0; }
    }
    @media (max-width: 480px) {
      .header .hamburger { display: flex !important; }
    }
  </style>
</head>
<body>
  <main class="main-content">
    <script>
    (function(){
      function closeDrawer(){
        if (window.g1CloseDrawer) window.g1CloseDrawer();
      }
      window.g1ActivateClientSection = function(sec){
        var nodes, i, el = document.getElementById('section-'+sec);
        if (!el) return false;
        closeDrawer();
        nodes = document.querySelectorAll('main.main-content .section');
        for (i = 0; i < nodes.length; i++) nodes[i].classList.remove('active');
        el.classList.add('active');
        nodes = document.querySelectorAll('#sidebar nav a');
        for (i = 0; i < nodes.length; i++) nodes[i].classList.toggle('active', nodes[i].getAttribute('data-section') === sec);
        try { if (location.hash !== '#'+sec) location.hash = sec; } catch (e) {}
        try { window.scrollTo(0, 0); } catch (e) {}
        if (sec === 'robot') {
          var rf = document.getElementById('robotControlFrame');
          if (rf && (!rf.src || rf.src === 'about:blank')) { rf.src = location.origin + '/robot-control'; }
        }
        if (sec === 'teaching' && typeof window.g1LoadExploreTeachings === 'function') {
          window.g1LoadExploreTeachings();
        }
        setTimeout(function(){
          if ((sec === 'soundboard' || sec === 'parla') && navigator.mediaDevices) {
            var o = document.getElementById('sbOutput');
            if (o && o.options && o.options.length <= 1 && typeof requestAndLoadDevices === 'function') requestAndLoadDevices();
          }
          if (sec === 'parla') {
            setTimeout(function(){
              if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible();
            }, 80);
          } else if (typeof stopParlaMicPreview === 'function') {
            stopParlaMicPreview();
          }
          if (sec !== 'parla' && typeof window.g1GrokVoiceStop === 'function') {
            window.g1GrokVoiceStop();
          }
        }, 0);
        return false;
      };
    })();
    (function(){
      function g1ApplyClientHash(){
        var h = (location.hash||'').replace(/^#/, '').trim();
        if (!h) return;
        var allowed = {soundboard:1,parla:1,occhi:1,log:1,knowledge:1,teaching:1,devices:1,robot:1,info:1};
        if (allowed[h] && typeof window.g1ActivateClientSection === 'function') {
          window.g1ActivateClientSection(h);
        }
      }
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', g1ApplyClientHash);
      } else {
        g1ApplyClientHash();
      }
      window.addEventListener('hashchange', g1ApplyClientHash);
    })();
    (function(){
      function _lanFill(data){
        var cur = location.origin + '/client';
        var ic = document.getElementById('infoClientUrl');
        if (ic) { ic.href = cur; ic.textContent = cur; }
        var io = document.getElementById('infoOcchiUrl');
        if (io) { io.href = cur + '#occhi'; io.textContent = cur + '#occhi'; }
        var id = document.getElementById('infoDashUrl');
        if (id) { id.href = location.origin + '/dashboard/'; }
        var ih = document.getElementById('infoHttpUrls');
        if (ih && data && data.links && data.links.length) {
          ih.innerHTML = data.links.map(function(l){ return l.http_redirect; }).filter(Boolean).join(' · ');
        }
        var ji = document.getElementById('infoJetsonIps');
        if (ji && data && data.ips) ji.textContent = data.ips.join(' ') || '—';
      }
      fetch(location.origin + '/api/network-info', { credentials: 'same-origin' })
        .then(function(r){ return r.json(); })
        .then(_lanFill)
        .catch(function(){ _lanFill(null); });
      window.g1RefreshLanLinks = function(){
        fetch(location.origin + '/api/network-info', { credentials: 'same-origin' })
          .then(function(r){ return r.json(); })
          .then(_lanFill)
          .catch(function(){ _lanFill(null); });
      };
    })();
    </script>
    <section id="section-soundboard" class="section active">
  <h2>Soundboard</h2>
  <div class="sb-controls" style="margin-bottom:12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
    <label style="font-size:12px;color:#9ca3af;">Uscita</label>
    <select id="sbPlayDest" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;min-width:220px;">
      <option value="server" selected>Cassa sul Jetson (robot)</option>
      <option value="browser">Browser (PC / telefono)</option>
    </select>
    <label id="sbBrowserSinkLabel" style="font-size:12px;color:#9ca3af;">Riproduci su</label>
    <select id="sbOutput" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;min-width:180px;">
      <option value="default">Predefinito</option>
    </select>
    <button type="button" id="sbOutputRefresh" style="padding:6px 12px;font-size:12px;background:rgba(255,255,255,0.08);color:#9ca3af;border:1px solid rgba(255,255,255,0.1);border-radius:8px;cursor:pointer;">Aggiorna</button>
  </div>
  <div class="sb-controls" style="margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;max-width:520px;">
    <label for="sbGainSlider" style="font-size:12px;color:#9ca3af;">Volume</label>
    <input type="range" id="sbGainSlider" min="0.5" max="3" step="0.05" value="1.35" style="flex:1;min-width:140px;max-width:220px;accent-color:#14b8a6;" />
    <span id="sbGainLabel" style="font-size:12px;color:#a1a1aa;font-family:monospace;min-width:44px;">1.35×</span>
  </div>
  <p id="soundboardLoadErr" class="hint" style="display:none;margin:0 0 6px;color:#f87171;grid-column:1/-1;"></p>
  <p id="soundboardLoadHint" class="hint" style="display:none;margin:0 0 6px;font-size:11px;color:#71717a;"></p>
  <div id="soundboardScroll">
    <p class="sb-zone-head">Solo audio</p>
    <div id="soundboardGridAudio" class="soundboard-grid"></div>
    <div class="sb-divider" aria-hidden="true">Audio + robot</div>
    <p class="sb-zone-head sb-zone-robot">Con movimento robot</p>
    <p class="hint" style="margin:-6px 0 10px;font-size:11px;color:#71717a;">Slot 51+: audio + gesto predefinito o movimento addestrato (app Explore).</p>
    <div id="soundboardGridRobot" class="soundboard-grid"></div>
  </div>
  <div id="sbModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:15000;padding:8px;flex-direction:column;align-items:center;justify-content:flex-start;overflow-y:auto;-webkit-overflow-scrolling:touch;">
    <div style="background:#1a1d24;border-radius:12px;padding:14px;max-width:400px;width:100%;border:1px solid rgba(255,255,255,0.1);margin:8px auto;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <h3 style="margin:0;font-size:1rem;flex:1;">Slot <span id="sbModalSlot">0</span></h3>
        <input type="text" id="sbModalIcon" placeholder="🎤" style="width:50px;padding:4px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#fff;font-size:18px;text-align:center;" />
      </div>
      <textarea id="sbModalText" placeholder="Testo per TTS" maxlength="1000" style="width:100%;min-height:50px;padding:8px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#fff;font-size:13px;resize:vertical;margin-bottom:2px;"></textarea>
      <p class="hint" style="margin:0 0 6px;font-size:10px;"><span id="sbModalCharCount">0</span>/<span id="sbModalCharMax">280</span></p>
      <div style="margin-bottom:6px;padding:8px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid rgba(255,255,255,0.06);">
        <div id="sbModalAudioStatus" style="font-size:11px;color:#9ca3af;margin-bottom:4px;">Nessun audio</div>
        <div style="display:flex;gap:4px;flex-wrap:wrap;">
          <button type="button" id="sbModalSynth" style="padding:6px 10px;background:#6366f1;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:11px;">TTS</button>
          <button type="button" id="sbModalRecord" style="padding:6px 10px;background:#14b8a6;color:#0c0e14;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:11px;">Rec 3s</button>
          <label style="padding:6px 10px;background:rgba(255,255,255,0.1);color:#e8eaed;border-radius:6px;cursor:pointer;font-size:11px;">File<input type="file" id="sbModalFile" accept="audio/*" style="display:none;" /></label>
          <button type="button" id="sbModalTts" style="padding:6px 10px;background:#8b5cf6;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:11px;">TTS da audio</button>
          <button type="button" id="sbModalClear" style="padding:6px 10px;background:rgba(239,68,68,0.3);color:#fca5a5;border:none;border-radius:6px;cursor:pointer;font-size:11px;">Rimuovi</button>
        </div>
      </div>
      <div id="sbModalRobotBlock">
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;">
        <select id="sbModalArm" style="flex:1;min-width:100px;padding:6px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:11px;">
          <option value="">Movimento: nessuno</option>
          <optgroup label="Gesti predefiniti">
          <option value="face_wave">Saluto</option>
          <option value="shake_hand">Stretta mano</option>
          <option value="hands_up">Mani in alto</option>
          <option value="clap">Applauso</option>
          <option value="high_five">High Five</option>
          <option value="hug">Abbraccio</option>
          <option value="heart">Cuore</option>
          <option value="right_heart">Cuore dx</option>
          <option value="kiss">Bacio</option>
          <option value="two_hand_kiss">Bacio 2 mani</option>
          <option value="reject">Rifiuto</option>
          <option value="right_hand_up">Mano dx su</option>
          <option value="x_ray">Braccia incr.</option>
          <option value="high_wave">Saluto alto</option>
          </optgroup>
        </select>
        <select id="sbModalLoco" style="flex:1;min-width:100px;padding:6px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:11px;">
          <option value="">Loco: nessuna</option>
          <option value="double_step_forward">2 passi avanti</option>
          <option value="double_step_back">2 passi indietro</option>
          <option value="walk_forward_10">10 passi avanti</option>
          <option value="spin_inplace">Gira</option>
          <option value="gentle_sway">Dondolio</option>
        </select>
      </div>
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;align-items:center;">
        <select id="sbModalLed" style="flex:1;min-width:110px;padding:6px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:11px;">
          <option value="">LED: nessuno</option>
          <option value="rainbow">&#127752; Arcobaleno</option>
          <option value="breathe_blue">&#128153; Blu pulsante</option>
          <option value="breathe_green">&#128154; Verde pulsante</option>
          <option value="breathe_red">&#10084;&#65039; Rosso pulsante</option>
          <option value="breathe_purple">&#128156; Viola pulsante</option>
          <option value="blink_red">&#128308; Rosso blink</option>
          <option value="blink_blue">&#128309; Blu blink</option>
          <option value="solid_blue">&#128309; Blu</option>
          <option value="solid_green">&#128994; Verde</option>
          <option value="solid_red">&#128308; Rosso</option>
          <option value="solid_amber">&#128992; Ambra</option>
          <option value="solid_purple">&#128995; Viola</option>
          <option value="solid_cyan">&#129525; Ciano</option>
          <option value="solid_white">&#11036; Bianco</option>
        </select>
        <div id="sbModalLedPreview" style="width:22px;height:22px;border-radius:50%;border:2px solid #3f3f46;background:#27272a;flex-shrink:0;"></div>
        <input type="number" id="sbModalTeaching" min="" max="99" value="" placeholder="Teach" style="display:none;" />
        <select id="sbModalExploreTeaching" style="display:none;" aria-hidden="true" tabindex="-1">
          <option value="">— Movimento Explore —</option>
        </select>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;align-items:center;">
        <label style="font-size:11px;color:#9ca3af;flex:1;min-width:140px;">Ritardo audio (ms)
          <input type="number" id="sbModalAudioDelay" min="0" max="15000" step="50" value="0" style="display:block;width:100%;margin-top:4px;padding:6px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:11px;" />
        </label>
        <label style="font-size:11px;color:#9ca3af;flex:1;min-width:140px;">Ritardo gesto (ms)
          <input type="number" id="sbModalGestureDelay" min="0" max="15000" step="50" value="0" style="display:block;width:100%;margin-top:4px;padding:6px;background:#27272a;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:11px;" />
        </label>
      </div>
      <p class="hint" style="margin:0 0 6px;font-size:10px;color:#71717a;">Con 0/0 il gesto parte con l'audio. Con ritardi: entrambi da Play, indipendenti (es. audio 500, gesto 200 → gesto a 200 ms, audio a 500 ms).</p>
      </div>
      <div style="display:flex;gap:6px;margin-top:8px;">
        <button type="button" id="sbModalSave" style="flex:1;padding:10px;background:#14b8a6;color:#0c0e14;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;">Salva</button>
        <button type="button" id="sbModalCancel" style="padding:10px 16px;background:rgba(255,255,255,0.1);color:#e8eaed;border:none;border-radius:8px;cursor:pointer;font-size:13px;">Annulla</button>
      </div>
    </div>
  </div>
    </section>
    <section id="section-parla" class="section">

  <h2 class="talk-page-title">Talk</h2>
  <p class="talk-page-subtitle">Scegli un agente. Puoi tenerli entrambi spenti; attivandone uno l'altro viene fermato e ridotto.</p>

  <!-- AUDIO GLOBALE: condiviso da Grok e Talk classico -->
  <div id="secureContextWarn" class="step" style="display:none;border-color:rgba(251,191,36,0.4);background:rgba(251,191,36,0.08);padding:14px;">
    <p style="margin:0 0 10px;font-size:13px;color:#fcd34d;"><strong>Serve HTTPS per il microfono browser.</strong></p>
    <p style="margin:0 0 10px;font-size:13px;"><a id="secureHttpsLink" href="#" style="color:#5eead4;font-weight:600;">Apri versione HTTPS</a></p>
    <p style="margin:0;font-size:12px;color:#a1a1aa;"><a href="#" id="secureWarnMore" style="color:#14b8a6;">Dettagli</a></p>
    <details id="secureWarnDetails" style="display:none;margin-top:10px;font-size:12px;color:#9ca3af;">
      <div id="secureWarnMobile" style="display:none;"><p style="margin:0;">Avanzate, Procedi.</p></div>
      <div id="secureWarnDesktop" style="display:none;"><p style="margin:0;">Tunnel SSH poi localhost:8081/client</p></div>
    </details>
  </div>
  <details id="talkAudioSettings" class="talk-global-settings" open>
    <summary>Audio Talk <span>microfono, altoparlante, soglia e guadagno</span></summary>
    <div class="talk-global-settings-body">
  <div id="activeMicIndicator" class="talk-active-device">
    <span id="activeMicDot"></span>
    <span id="activeMicLabel">Microfono: attivazione…</span>
  </div>
  <div id="devicesWrap" class="step talk-device-grid">
    <p id="deviceStatus" style="font-size:11px;margin:0 0 10px;color:#52525b;">Microfono: attivazione…</p>
    <div class="talk-device-row">
      <div><label for="mic">Microfono</label><select id="mic"><option value="">Caricamento...</option></select></div>
      <div><label for="speaker">Altoparlante / cassa</label><select id="speaker"><option value="">Caricamento...</option></select></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px;">
      <button type="button" id="devicesRefresh" style="padding:8px 14px;background:rgba(255,255,255,0.08);color:#e8eaed;border:1px solid rgba(255,255,255,0.12);border-radius:8px;cursor:pointer;font-size:13px;">Aggiorna</button>
      <button type="button" id="devicesSave" style="padding:8px 14px;background:#14b8a6;color:#0c0e14;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:13px;">Salva</button>
      <span id="devicesSaveStatus" class="hint" style="margin:0;"></span>
    </div>
  </div>
  <div class="talk-sensitivity">
    <div id="parlaPreviewDisabledMsg" style="display:none;font-size:11px;color:#f59e0b;margin-bottom:8px;">Seleziona un microfono Browser.</div>
    <div id="parlaPreviewMeterWrap">
      <div class="talk-level-track">
        <div id="parlaPreviewThresholdLine"></div>
        <div id="parlaPreviewBarFill"></div>
      </div>
      <div class="talk-level-meta">
        <span id="parlaPreviewStatus">Picco: — · soglia: —</span>
        <span id="parlaPreviewGate">—</span>
      </div>
    </div>
    <div class="talk-slider-grid">
      <label for="micWakeThresholdSlider">Soglia voce <strong id="wakeThDisplay">20</strong><input type="range" id="micWakeThresholdSlider" min="1" max="80" value="20" /></label>
      <label for="micMonitorGainSlider">Guadagno microfono <strong id="micGainDisplay">1.0</strong>×<input type="range" id="micMonitorGainSlider" min="0.4" max="4" step="0.1" value="1" /></label>
      <label for="parlaGainSlider">Volume risposta <strong id="parlaGainLabel">2.5x</strong><input type="range" id="parlaGainSlider" min="0.5" max="10.0" step="0.1" /></label>
    </div>
  </div>
  <div id="ttsOutputWrap" class="talk-output-mode">
      <label for="ttsPlayDest">Destinazione risposta Talk classico</label>
      <select id="ttsPlayDest" style="padding:8px 12px;background:#27272a;border:1px solid #3f3f46;border-radius:8px;color:#e4e4e7;font-size:13px;width:100%;max-width:300px;">
        <option value="browser" selected>Browser (telefono/PC)</option>
        <option value="server">Cassa robot (Jetson)</option>
      </select>
      <p id="ttsServerHint" class="hint" style="display:none;margin:4px 0 0;font-size:10px;color:#52525b;"></p>
  </div>
    </div>
  </details>

  <div class="talk-agent-stack">
  <!-- GROK VOICE AGENT -->
  <div id="grokVoicePanel" class="talk-agent-card talk-agent-grok is-collapsed">
    <div class="talk-agent-head">
      <div><h3>Grok Voice Agent</h3><p>Conversazione realtime diretta con xAI</p></div>
      <label for="grokVoiceToggle" class="talk-agent-toggle"><span id="grokVoiceToggleLabel">OFF</span><input type="checkbox" id="grokVoiceToggle" /></label>
    </div>
    <div id="grokVoiceBody" class="talk-agent-body">
      <p id="grokVoiceStatus" class="talk-agent-status">Disattivato</p>
      <p id="grokVoiceTranscript" class="talk-transcript"></p>
      <p id="grokVoiceConfigHint" class="hint" style="display:none;margin:8px 0 0;font-size:11px;color:#f59e0b;">Configura XAI_API_KEY e XAI_AGENT_ID nel file .env sul server.</p>
    </div>
  </div>

  <!-- TALK CLASSICO -->
  <div id="legacyTalkPanel" class="talk-agent-card talk-agent-legacy is-collapsed">
    <div class="talk-agent-head">
      <div><h3>Talk classico</h3><p>Hey G1, knowledge, azioni robot e TTS</p></div>
      <label for="wakeListenToggle" class="talk-agent-toggle"><span id="wakeToggleLabel">OFF</span><input type="checkbox" id="wakeListenToggle" class="wake-checkbox" /></label>
    </div>
    <div id="legacyTalkBody" class="talk-agent-body">
      <p id="wakeListenStatus" class="talk-agent-status">Disattivato</p>
      <div id="wakeDebugLog" style="max-height:60px;overflow-y:auto;font-size:10px;font-family:monospace;color:#52525b;line-height:1.4;margin:0 0 8px;padding:4px 8px;background:rgba(0,0,0,0.2);border-radius:6px;display:none;"></div>
      <div id="recStatus" style="min-height:30px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
          <div style="flex:1;height:16px;background:#1e1e2e;border-radius:8px;overflow:hidden;border:1px solid rgba(255,255,255,0.06);"><div id="levelBar" style="width:0%;height:100%;background:#22c55e;transition:width 0.06s;border-radius:8px;"></div></div>
          <span id="levelLabel" style="font-size:12px;color:#71717a;font-family:monospace;min-width:140px;">Livello: --</span>
        </div>
        <p class="hint" id="recDebug" style="font-size:11px;color:#71717a;min-height:16px;margin:0;"></p>
      </div>
      <div class="result" id="result"></div>
      <div class="talk-text-question">
        <h4>Scrivi una domanda</h4>
        <div><input type="text" id="textInput" placeholder="Es: Che ore sono?" /><button type="button" id="btnText">Invia</button></div>
        <p class="hint" id="textStatus"></p>
      </div>
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
    </div>
  </div>
  </div>
    </section>
    <section id="section-knowledge" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 16px;">Knowledge</h2>
  <details id="knowledgeWrap" class="step" open style="margin-bottom:12px;border:1px solid rgba(255,255,255,0.06);">
    <summary style="cursor:pointer;color:#a1a1aa;">Gruppi pattern (<span id="knowledgeCount">caricamento…</span>)</summary>
    <p style="margin:8px 0 0;font-size:12px;color:#71717a;">Ogni gruppo può essere attivato o disattivato. Solo i gruppi attivi vengono usati per le risposte rapide.</p>
    <div id="knowledgeGroups" style="margin-top:8px;"></div>
    <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;">
      <button type="button" id="knowledgeAddGroup" style="padding:8px 12px;background:rgba(20,184,166,0.15);color:#14b8a6;border:1px solid rgba(20,184,166,0.35);border-radius:8px;cursor:pointer;font-size:12px;">Nuovo gruppo</button>
      <button type="button" id="knowledgeSave" style="padding:8px 16px;background:rgba(255,255,255,0.1);color:#e8eaed;border:1px solid rgba(255,255,255,0.2);border-radius:8px;cursor:pointer;font-size:12px;">Salva su server</button>
    </div>
  </details>
    </section>
    <section id="section-devices" class="section">
  <h2 style="font-size:1.2rem;margin:0 0 12px;">Dispositivi</h2>
  <div class="step">
    <div style="margin-top:14px;padding:12px;background:rgba(34,197,94,0.06);border:1px solid rgba(34,197,94,0.2);border-radius:10px;">
      <label style="display:block;font-size:12px;color:#86efac;font-weight:600;margin-bottom:6px;">Volume TTS</label>
      <div style="display:flex;align-items:center;gap:10px;">
        <input type="range" id="ttsGainSlider" min="0.5" max="10.0" step="0.1" style="flex:1;accent-color:#22c55e;" />
        <span id="ttsGainLabel" style="font-size:12px;color:#a1a1aa;font-family:monospace;min-width:40px;">2.5x</span>
      </div>
    </div>
    <details style="margin-top:12px;">
      <summary style="cursor:pointer;font-size:12px;color:#71717a;">Avanzate</summary>
      <div style="padding-top:8px;">
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
    <a id="infoClientUrl" href="/client" style="display:inline-block;padding:12px 18px;background:#14b8a6;color:#0c0e14;border-radius:10px;text-decoration:none;font-weight:600;font-size:14px;margin-bottom:6px;word-break:break-all;"></a>
    <p style="margin:8px 0 0;font-size:12px;"><a id="infoOcchiUrl" href="/client#occhi" style="color:#5eead4;">Occhi</a> · <a id="infoDashUrl" href="/dashboard/" style="color:#5eead4;">Dashboard</a></p>
    <span id="infoHttpUrls" style="display:none;"></span>
    <code id="infoJetsonIps" style="display:none;"></code>
  </div>
    </section>
    <section id="section-log" class="section">
      <h2 style="font-size:1.2rem;margin:0 0 8px;">Log Jetson</h2>
      <div class="client-cam-btns" style="margin-top:0;margin-bottom:8px;">
        <button type="button" class="primary" onclick="window.g1ClientLogRefresh && g1ClientLogRefresh()">Aggiorna ora</button>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:#9ca3af;">
          <input type="checkbox" id="clientLogAuto" checked style="accent-color:#14b8a6;" /> Auto (3s)
        </label>
      </div>
      <pre id="clientLogBox" class="client-log-box">Caricamento log…</pre>
    </section>
    <section id="section-occhi" class="section">
      <h2 style="font-size:1.2rem;margin:0 0 8px;">Occhi robot</h2>
      <label class="client-cam-vision-toggle" for="clientCamVisionEnable">
        <input type="checkbox" id="clientCamVisionEnable" />
        <span>Attiva visione (stream camera)</span>
      </label>
      <div class="client-camera-wrap">
        <img id="clientCamStream" alt="Occhi robot G1" />
        <div id="clientCamPlaceholder" class="client-camera-placeholder">Visione disattivata — spunta la casella sopra per collegare la camera</div>
      </div>
      <div class="client-cam-meta">
        <span class="lbl">Camera</span><span class="val" id="clientCamStatus">--</span>
        <span class="lbl">YOLO</span><span class="val" id="clientCamYolo">--</span>
        <span class="lbl">FPS</span><span class="val" id="clientCamFps">--</span>
        <span class="lbl">Backend</span><span class="val" id="clientCamBackend">--</span>
      </div>
      <div class="client-cam-dets" id="clientCamDets">Nessun oggetto rilevato</div>
      <div class="client-cam-btns">
        <button type="button" class="primary" id="clientCamBtnStart">Avvia stream</button>
        <button type="button" id="clientCamBtnStop">Ferma</button>
        <button type="button" id="clientCamBtnRefresh">Aggiorna</button>
        <button type="button" id="clientPickOnBtn" onclick="window.g1ClientPickSet(true)" style="border-color:#14b8a6;color:#5eead4;">Auto-pick ON</button>
        <button type="button" id="clientPickOffBtn" onclick="window.g1ClientPickSet(false)">Auto-pick OFF</button>
      </div>
      <div class="hint" id="clientPickStatus" style="margin-top:8px;font-size:11px;color:#71717a;">Auto-pick: —</div>
    </section>
    <section id="section-teaching" class="section">
      <h2 style="font-size:1.2rem;margin:0 0 12px;">Teaching Explore</h2>
      <div class="explore-teach-steps">
        <strong>1.</strong> Registra sul telefono: app <strong>Unitree Explore</strong> → Function → Demo → Teaching → Create → salva con un nome.<br>
        <strong>2.</strong> Sul telecomando: <strong>L1+A</strong> (sport mode). Poi <strong>Prepara robot</strong> qui sotto.<br>
        <strong>3.</strong> <strong>Aggiorna elenco</strong> e premi <strong>Play</strong> sul movimento.
      </div>
      <div class="explore-teach-bar">
        <button type="button" id="exploreTeachRefresh" onclick="window.g1LoadExploreTeachings && window.g1LoadExploreTeachings()">Aggiorna elenco</button>
        <button type="button" class="secondary" onclick="window.g1PrepareExploreTeaching && window.g1PrepareExploreTeaching()">Prepara robot</button>
        <button type="button" class="secondary" onclick="window.g1StopExploreTeaching && window.g1StopExploreTeaching()">Rilascia braccia</button>
      </div>
      <p id="exploreTeachFsm" class="hint" style="margin:0 0 8px;font-size:11px;color:#71717a;">Stato robot: —</p>
      <p id="exploreTeachErr" class="hint" style="display:none;margin:0 0 8px;color:#f87171;"></p>
      <div id="exploreTeachList"><p class="hint" style="margin:0;font-size:12px;color:#52525b;">Caricamento…</p></div>
    </section>
    <section id="section-robot" class="section">
      <h2 style="font-size:1.2rem;margin:0 0 8px;">Robot G1</h2>
      <div id="robotPanelControl">
        <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
          <a href="/vr-control" target="_blank" style="display:inline-flex;align-items:center;gap:6px;padding:10px 18px;background:rgba(167,139,250,0.15);border:1px solid rgba(167,139,250,0.4);color:#c4b5fd;border-radius:10px;text-decoration:none;font-weight:600;font-size:13px;">&#x1F576; VR Control (Quest 3)</a>
          <a href="/robot-control" target="_blank" style="display:inline-flex;align-items:center;gap:6px;padding:10px 18px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.3);color:#a5b4fc;border-radius:10px;text-decoration:none;font-weight:600;font-size:13px;">&#127918; Robot Control (fullscreen)</a>
        </div>
        <iframe id="robotControlFrame" title="Robot control" src="about:blank"></iframe>
      </div>
    </section>
  </main>
  <header class="header">
    <button type="button" class="hamburger" id="hamburger" aria-label="Menu" aria-expanded="false" onclick="return window.g1ToggleDrawer && window.g1ToggleDrawer(event)">&#9776;</button>
    <h1>G1 Talk</h1>
  </header>
  <div class="overlay" id="overlay"></div>
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <span>Menu</span>
      <button type="button" class="sidebar-close" id="sidebarClose" aria-label="Chiudi menu" onclick="return window.g1CloseDrawer && window.g1CloseDrawer()">&#10005;</button>
    </div>
    <nav>
      <a href="#soundboard" data-section="soundboard" class="active" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('soundboard')"><span class="icon">&#128266;</span> Soundboard</a>
      <a href="#parla" data-section="parla" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('parla')"><span class="icon">&#127908;</span> Parla</a>
      <a href="#occhi" data-section="occhi" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('occhi')"><span class="icon">&#128065;</span> Occhi robot</a>
      <a href="#log" data-section="log" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('log')"><span class="icon">&#128196;</span> Log Jetson</a>
      <a href="#knowledge" data-section="knowledge" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('knowledge')"><span class="icon">&#128214;</span> Knowledge</a>
      <a href="#teaching" data-section="teaching" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('teaching')"><span class="icon">&#129302;</span> Teaching</a>
      <a href="#devices" data-section="devices" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('devices')"><span class="icon">&#128268;</span> Dispositivi</a>
      <a href="#robot" data-section="robot" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('robot')"><span class="icon">&#127918;</span> Robot (G1)</a>
      <a href="#info" data-section="info" onclick="return window.g1ActivateClientSection && window.g1ActivateClientSection('info')"><span class="icon">&#8505;</span> Info</a>
    </nav>
  </aside>
  <script>
  (function(){
    function g1OpenDrawer(){
      var sb = document.getElementById('sidebar');
      var ov = document.getElementById('overlay');
      var hb = document.getElementById('hamburger');
      if (sb) sb.classList.add('open');
      if (ov) ov.classList.add('visible');
      if (hb) hb.setAttribute('aria-expanded', 'true');
      document.body.classList.add('g1-drawer-open');
    }
    function g1CloseDrawer(){
      var sb = document.getElementById('sidebar');
      var ov = document.getElementById('overlay');
      var hb = document.getElementById('hamburger');
      if (sb) sb.classList.remove('open');
      if (ov) ov.classList.remove('visible');
      if (hb) hb.setAttribute('aria-expanded', 'false');
      document.body.classList.remove('g1-drawer-open');
    }
    window.g1ToggleDrawer = function(e){
      if (e) { e.preventDefault(); e.stopPropagation(); }
      var sb = document.getElementById('sidebar');
      if (sb && sb.classList.contains('open')) g1CloseDrawer(); else g1OpenDrawer();
      return false;
    };
    window.g1CloseDrawer = g1CloseDrawer;
    window.g1OpenDrawer = g1OpenDrawer;
    var _sbClose = document.getElementById('sidebarClose');
    if (_sbClose) _sbClose.addEventListener('click', function(e){ e.preventDefault(); g1CloseDrawer(); });
  })();
  </script>

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
      var overlay = document.getElementById('overlay');
      if (overlay) overlay.addEventListener('click', function(e){ e.preventDefault(); if (window.g1CloseDrawer) window.g1CloseDrawer(); });
      var navLinks = document.querySelectorAll('.sidebar nav a');
      for (var ai = 0; ai < navLinks.length; ai++) {
        navLinks[ai].addEventListener('click', function(e){
          e.preventDefault();
          var sec = this.getAttribute('data-section');
          if (sec && window.g1ActivateClientSection) window.g1ActivateClientSection(sec);
        });
      }
      if (window.g1CloseDrawer) window.g1CloseDrawer();
    })();

    const wsUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws';
    const wsParlaUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/parla';
    const wsGrokVoiceUrl = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws/grok-voice';
    var _talkAgentMode = 'none';
    function applyTalkAgentLayout(mode){
      _talkAgentMode = mode || 'none';
      var grok = document.getElementById('grokVoicePanel');
      var legacy = document.getElementById('legacyTalkPanel');
      if (grok) {
        grok.classList.toggle('is-active', _talkAgentMode === 'grok');
        grok.classList.toggle('is-collapsed', _talkAgentMode !== 'grok');
      }
      if (legacy) {
        legacy.classList.toggle('is-active', _talkAgentMode === 'legacy');
        legacy.classList.toggle('is-collapsed', _talkAgentMode !== 'legacy');
      }
    }
    window.g1SetTalkAgentMode = function(mode){
      mode = (mode === 'grok' || mode === 'legacy') ? mode : 'none';
      if (mode === 'grok') {
        var wt = document.getElementById('wakeListenToggle');
        if (wt && wt.checked) {
          wt.checked = false;
          if (typeof stopWakeRecorder === 'function') stopWakeRecorder();
          if (typeof resetWakeCommandMode === 'function') resetWakeCommandMode();
        }
        var wl = document.getElementById('wakeToggleLabel');
        if (wl) wl.textContent = 'OFF';
      } else if (mode === 'legacy' && typeof window.g1GrokVoiceStop === 'function') {
        window.g1GrokVoiceStop(true);
      }
      applyTalkAgentLayout(mode);
    };
    applyTalkAgentLayout('none');
    (function(){
      var grokWs = null, grokMicStream = null, grokCaptureCtx = null, grokCaptureNode = null;
      var grokPlaybackCtx = null, grokNextPlayTime = 0, grokActive = false, grokSessionReady = false;
      var GROK_SAMPLE_RATE = 24000;
      function grokEl(id){ return document.getElementById(id); }
      function grokSetStatus(t){ var el = grokEl('grokVoiceStatus'); if (el) el.textContent = t; }
      var grokUserText = '', grokAssistantText = '';
      function grokUpdateTranscript(){
        var el = grokEl('grokVoiceTranscript');
        if (!el) return;
        var parts = [];
        if (grokUserText) parts.push('Tu: ' + grokUserText);
        if (grokAssistantText) parts.push('Grok: ' + grokAssistantText);
        el.textContent = parts.join(' · ');
      }
      function grokDownsample(buffer, fromRate, toRate){
        if (fromRate === toRate) return buffer;
        var ratio = fromRate / toRate;
        var newLen = Math.round(buffer.length / ratio);
        var out = new Float32Array(newLen);
        for (var i = 0; i < newLen; i++) out[i] = buffer[Math.floor(i * ratio)] || 0;
        return out;
      }
      function grokFloatToPcm16(float32){
        var buf = new ArrayBuffer(float32.length * 2);
        var view = new DataView(buf);
        for (var i = 0; i < float32.length; i++){
          var s = Math.max(-1, Math.min(1, float32[i]));
          view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7FFF, true);
        }
        return buf;
      }
      function grokPlayPcmDelta(b64){
        try {
          var raw = atob(b64);
          var bytes = new Uint8Array(raw.length);
          for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
          var int16 = new Int16Array(bytes.buffer);
          var float32 = new Float32Array(int16.length);
          for (var j = 0; j < int16.length; j++) float32[j] = int16[j] / 32768;
          if (!grokPlaybackCtx) {
            grokPlaybackCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: GROK_SAMPLE_RATE });
            var spk = grokEl('speaker');
            var sink = spk && spk.value && spk.value.indexOf('browser_') === 0 ? spk.value.replace(/^browser_/, '') : '';
            if (sink && sink !== 'default' && typeof grokPlaybackCtx.setSinkId === 'function') {
              grokPlaybackCtx.setSinkId(sink).catch(function(){});
            }
          }
          if (grokPlaybackCtx.state === 'suspended') grokPlaybackCtx.resume();
          var buffer = grokPlaybackCtx.createBuffer(1, float32.length, GROK_SAMPLE_RATE);
          buffer.copyToChannel(float32, 0);
          var src = grokPlaybackCtx.createBufferSource();
          src.buffer = buffer;
          src.connect(grokPlaybackCtx.destination);
          var t = Math.max(grokPlaybackCtx.currentTime, grokNextPlayTime);
          src.start(t);
          grokNextPlayTime = t + buffer.duration;
        } catch (_) {}
      }
      function grokSend(obj){
        if (grokWs && grokWs.readyState === 1) grokWs.send(JSON.stringify(obj));
      }
      function grokStopMic(){
        if (grokCaptureNode) { try { grokCaptureNode.disconnect(); } catch(_){} grokCaptureNode.onaudioprocess = null; grokCaptureNode = null; }
        if (grokCaptureCtx) { try { grokCaptureCtx.close(); } catch(_){} grokCaptureCtx = null; }
        if (grokMicStream) { grokMicStream.getTracks().forEach(function(t){ try { t.stop(); } catch(_){} }); grokMicStream = null; }
      }
      function grokStopWs(){
        if (grokWs) { try { grokWs.close(); } catch(_){} grokWs = null; }
      }
      window.g1GrokVoiceStop = function(keepLayout){
        var wasActive = grokActive;
        grokActive = false;
        grokSessionReady = false;
        grokNextPlayTime = 0;
        grokUserText = '';
        grokAssistantText = '';
        grokStopMic();
        grokStopWs();
        var wt = grokEl('wakeListenToggle');
        if (wt) wt.disabled = false;
        var tg = grokEl('grokVoiceToggle');
        if (tg) tg.checked = false;
        var tl = grokEl('grokVoiceToggleLabel');
        if (tl) tl.textContent = 'OFF';
        if (wasActive) grokSetStatus('Disattivato');
        grokUpdateTranscript();
        if (!keepLayout) {
          if (_talkAgentMode === 'grok') applyTalkAgentLayout('none');
          setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 180);
        }
      };
      function grokConfigureSession(){
        grokSend({
          type: 'session.update',
          session: {
            turn_detection: { type: 'server_vad' },
            audio: {
              input: { format: { type: 'audio/pcm', rate: GROK_SAMPLE_RATE } },
              output: { format: { type: 'audio/pcm', rate: GROK_SAMPLE_RATE } }
            }
          }
        });
      }
      var grokLastLevelTs = 0;
      async function grokStartMic(){
        var micSel = grokEl('mic');
        var micValue = micSel ? String(micSel.value || '') : '';
        if (micValue.indexOf('local_') === 0) {
          grokSetStatus('Microfono DJI sulla Jetson — attendo sessione…');
          return;
        }
        if (micValue.indexOf('net_') === 0) {
          window.g1GrokVoiceStop();
          grokSetStatus('Seleziona il DJI Jetson oppure un microfono Browser');
          return;
        }
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
          window.g1GrokVoiceStop();
          grokSetStatus('Microfono browser non disponibile');
          return;
        }
        var devId = micValue.indexOf('webmic_') === 0 ? decodeURIComponent(micValue.slice(7)) : '';
        grokSetStatus('Apertura microfono…');
        try {
          if (typeof getUserMediaWithFallback === 'function') {
            grokMicStream = await getUserMediaWithFallback(devId || null);
          } else {
            grokMicStream = await navigator.mediaDevices.getUserMedia(
              typeof buildAudioCaptureConstraints === 'function'
                ? buildAudioCaptureConstraints(devId)
                : { audio: devId ? { deviceId: { exact: devId } } : true }
            );
          }
        } catch (e) {
          window.g1GrokVoiceStop();
          grokSetStatus((e && e.message) ? e.message : 'Permesso microfono negato');
          return;
        }
        grokCaptureCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (grokCaptureCtx.state === 'suspended') await grokCaptureCtx.resume();
        var source = grokCaptureCtx.createMediaStreamSource(grokMicStream);
        grokCaptureNode = grokCaptureCtx.createScriptProcessor(8192, 1, 1);
        var grokSilentOut = grokCaptureCtx.createGain();
        grokSilentOut.gain.value = 0;
        grokCaptureNode.onaudioprocess = function(ev){
          if (!grokActive || !grokSessionReady || !grokWs || grokWs.readyState !== 1) return;
          var input = ev.inputBuffer.getChannelData(0);
          var inputGain = typeof getParlaMonitorGain === 'function' ? getParlaMonitorGain() : 1;
          var peak = 0;
          for (var p = 0; p < input.length; p++) peak = Math.max(peak, Math.abs(input[p]));
          var peak255 = Math.min(255, Math.round(peak * 255 * inputGain));
          var now = Date.now();
          if (now - grokLastLevelTs >= 120 && typeof window.g1UpdateTalkMicLevel === 'function') {
            grokLastLevelTs = now;
            window.g1UpdateTalkMicLevel(peak255);
          }
          var threshold = typeof getWakeVoiceThreshold === 'function' ? getWakeVoiceThreshold() : 20;
          var adjusted = new Float32Array(input.length);
          if (peak255 >= threshold) {
            for (var a = 0; a < input.length; a++) adjusted[a] = Math.max(-1, Math.min(1, input[a] * inputGain));
          }
          var pcm = grokDownsample(adjusted, grokCaptureCtx.sampleRate, GROK_SAMPLE_RATE);
          var buf = grokFloatToPcm16(pcm);
          grokSend({ type: 'input_audio_buffer.append', audio: arrayBufferToBase64(buf) });
        };
        source.connect(grokCaptureNode);
        grokCaptureNode.connect(grokSilentOut);
        grokSilentOut.connect(grokCaptureCtx.destination);
        grokSetStatus('In ascolto — parla con Grok');
      }
      function grokHandleEvent(ev){
        if (!ev || !ev.type) return;
        if (ev.type === 'proxy.ready') {
          grokSessionReady = false;
          grokConfigureSession();
          return;
        }
        if (ev.type === 'proxy.mic_info') {
          grokSetStatus('In ascolto dal ' + (ev.name || 'microfono Jetson'));
          return;
        }
        if (ev.type === 'proxy.mic_level') {
          if (typeof window.g1UpdateTalkMicLevel === 'function') {
            window.g1UpdateTalkMicLevel(ev.peak255 || 0);
          }
          return;
        }
        if (ev.type === 'session.updated' || ev.type === 'session.created') {
          grokSessionReady = true;
          grokSetStatus('Agente attivo — parla');
          return;
        }
        if (ev.type === 'response.output_audio.delta' && ev.delta) {
          grokPlayPcmDelta(ev.delta);
          grokSetStatus('Grok sta parlando…');
          return;
        }
        if (ev.type === 'response.output_audio_transcript.delta' && ev.delta) {
          grokAssistantText += ev.delta;
          grokUpdateTranscript();
          return;
        }
        if (ev.type === 'conversation.item.input_audio_transcription.completed' && ev.transcript) {
          grokUserText = ev.transcript;
          grokAssistantText = '';
          grokUpdateTranscript();
          return;
        }
        if (ev.type === 'response.created') grokAssistantText = '';
        if (ev.type === 'input_audio_buffer.speech_started') grokSetStatus('Ti sto ascoltando…');
        if (ev.type === 'response.done') grokSetStatus('In ascolto — parla con Grok');
        if (ev.type === 'error') {
          grokSetStatus('Errore: ' + (ev.error && ev.error.message ? ev.error.message : (ev.error || 'sconosciuto')));
        }
      }
      window.g1GrokVoiceStart = async function(){
        if (grokActive) return;
        window.g1SetTalkAgentMode('grok');
        if (typeof stopParlaMicPreview === 'function') stopParlaMicPreview();
        if (typeof stopWakeRecorder === 'function') stopWakeRecorder();
        var wt = grokEl('wakeListenToggle');
        if (wt) { wt.checked = false; wt.disabled = true; }
        grokActive = true;
        grokSetStatus('Connessione a Grok…');
        grokUserText = '';
        grokAssistantText = '';
        grokUpdateTranscript();
        var micEl = grokEl('mic');
        var micValue = micEl ? String(micEl.value || '') : '';
        var grokUrl = wsGrokVoiceUrl;
        if (micValue.indexOf('local_') === 0) {
          var gain = typeof getParlaMonitorGain === 'function' ? getParlaMonitorGain() : 1;
          var threshold = typeof getWakeVoiceThreshold === 'function' ? getWakeVoiceThreshold() : 20;
          grokUrl += '?input=jetson&gain=' + encodeURIComponent(gain) + '&threshold=' + encodeURIComponent(threshold);
        }
        grokWs = new WebSocket(grokUrl);
        grokWs.onopen = function(){ grokSetStatus('Connesso — configurazione sessione…'); };
        grokWs.onmessage = function(msg){
          try { grokHandleEvent(JSON.parse(msg.data)); } catch (_) {}
        };
        grokWs.onerror = function(){ grokSetStatus('Errore WebSocket'); };
        grokWs.onclose = function(){
          grokWs = null;
          if (grokActive) {
            grokActive = false;
            grokStopMic();
            grokSetStatus('Connessione chiusa');
            var tg = grokEl('grokVoiceToggle');
            if (tg) tg.checked = false;
            var wt = grokEl('wakeListenToggle');
            if (wt) wt.disabled = false;
          }
        };
        await grokStartMic();
      };
      function grokRefreshPanel(){
        fetch('/api/grok-voice/status').then(function(r){ return r.json(); }).then(function(d){
          var panel = grokEl('grokVoicePanel');
          var hint = grokEl('grokVoiceConfigHint');
          var tg = grokEl('grokVoiceToggle');
          if (!panel) return;
          if (!d.configured) {
            if (hint) hint.style.display = 'block';
            if (tg) tg.disabled = true;
            grokSetStatus('Non configurato sul server');
          } else {
            if (hint) hint.style.display = 'none';
            if (tg) tg.disabled = false;
            grokSetStatus('Disattivato' + (d.agent_id ? ' (agent ' + d.agent_id + ')' : ''));
          }
        }).catch(function(){});
      }
      var grokToggle = grokEl('grokVoiceToggle');
      if (grokToggle) {
        grokToggle.onchange = function(){
          var lbl = grokEl('grokVoiceToggleLabel');
          if (lbl) lbl.textContent = grokToggle.checked ? 'ON' : 'OFF';
          if (grokToggle.checked) {
            window.g1SetTalkAgentMode('grok');
            window.g1GrokVoiceStart();
          } else {
            window.g1GrokVoiceStop();
          }
        };
      }
      grokRefreshPanel();
    })();
    var btn = null;
    let _loadDevicesSeq = 0;
    let _micPermissionGranted = false;
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
      const name = (opt && opt.textContent) ? String(opt.textContent).trim() : '';
      const isLocal = val && String(val).indexOf('local_') === 0;
      const isBrowser = val && String(val).indexOf('webmic_') === 0;
      const wt = document.getElementById('wakeListenToggle');
      const listening = wt && wt.checked;
      if (isLocal) {
        dot.style.background = listening ? '#22c55e' : '#14b8a6';
        dot.style.boxShadow = listening ? '0 0 6px #22c55e' : 'none';
        lbl.innerHTML = 'Microfono: <strong style="color:#2dd4bf;">' + escapeHtmlDevices(name || 'Jetson USB') + '</strong>' + (listening ? ' <span style="color:#22c55e;">(ascolto attivo)</span>' : '');
      } else if (isBrowser) {
        dot.style.background = listening ? '#22c55e' : '#3b82f6';
        dot.style.boxShadow = listening ? '0 0 6px #22c55e' : 'none';
        lbl.innerHTML = 'Microfono: <strong style="color:#60a5fa;">' + escapeHtmlDevices(name || 'Browser') + '</strong>' + (listening ? ' <span style="color:#22c55e;">(ascolto attivo)</span>' : '');
      } else if (name && name !== 'Caricamento...' && name !== 'Nessun microfono browser') {
        dot.style.background = '#71717a';
        dot.style.boxShadow = 'none';
        lbl.innerHTML = 'Microfono: <strong style="color:#e4e4e7;">' + escapeHtmlDevices(name) + '</strong>';
      } else {
        dot.style.background = '#71717a';
        dot.style.boxShadow = 'none';
        lbl.innerHTML = '<span style="color:#71717a;">Microfono: attivazione…</span>';
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
    let wakeListenActive = false, wakeMimeType = '', wakeActiveMr = null, wakeDiscardCurrentSlice = false;
    let wsListenServer = null, wakeServerMode = false;
    let wakeCommandMode = false, wakeCommandIdleTimer = null;
    let wakeAudioInFlight = false, wakeQueuedBlob = null;
    let listenServerWakeLatched = false;
    let wakeLevelCtx = null, wakeAnalyser = null, wakeInputGainNode = null, wakeLevelSampleInterval = null;
    let wakeSlicePeak = 0;
    /** Default soglia voce (0-255 FFT); override con slider e localStorage g1_wake_voice_threshold. */
    const WAKE_VOICE_THRESHOLD_DEFAULT = 20;
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
    function applyLocalMicDefaultsIfUnset(micValue) {
      if (!micValue || String(micValue).indexOf('local_') !== 0) return;
      try {
        var migrated = localStorage.getItem('g1_dji_levels_v1') === '1';
        var oldThreshold = localStorage.getItem('g1_wake_voice_threshold');
        var oldGain = localStorage.getItem('g1_mic_monitor_gain');
        if (oldThreshold == null || (!migrated && oldThreshold === '20')) {
          localStorage.setItem('g1_wake_voice_threshold', '5');
        }
        if (oldGain == null || (!migrated && (oldGain === '1' || oldGain === '1.0'))) {
          localStorage.setItem('g1_mic_monitor_gain', '2');
        }
        localStorage.setItem('g1_dji_levels_v1', '1');
      } catch (_) {}
      var threshold = getWakeVoiceThreshold();
      var gain = getParlaMonitorGain();
      var thSlider = document.getElementById('micWakeThresholdSlider');
      var thDisplay = document.getElementById('wakeThDisplay');
      var gainSlider = document.getElementById('micMonitorGainSlider');
      var gainDisplay = document.getElementById('micGainDisplay');
      if (thSlider) thSlider.value = String(threshold);
      if (thDisplay) thDisplay.textContent = String(threshold);
      if (gainSlider) gainSlider.value = String(gain);
      if (gainDisplay) gainDisplay.textContent = gain.toFixed(1);
      updateParlaThresholdLine();
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
    window.g1UpdateTalkMicLevel = function(peak){
      peak = Math.max(0, Math.min(255, Number(peak) || 0));
      var th = getWakeVoiceThreshold();
      var gain = getParlaMonitorGain();
      var barW = Math.min(100, peak * (100 / 255));
      var fill = document.getElementById('parlaPreviewBarFill');
      var st = document.getElementById('parlaPreviewStatus');
      var gate = document.getElementById('parlaPreviewGate');
      if (fill) fill.style.width = barW.toFixed(1) + '%';
      if (st) st.textContent = 'Picco: ' + Math.round(peak) + ' / 255 · soglia: ' + th + ' · gain: ' + gain.toFixed(1) + '×';
      if (gate) {
        if (peak >= th) {
          gate.textContent = 'VOCE';
          gate.style.background = 'rgba(34,197,94,0.25)';
          gate.style.color = '#4ade80';
        } else {
          gate.textContent = 'Silenzio';
          gate.style.background = '#27272a';
          gate.style.color = '#71717a';
        }
      }
      updateParlaThresholdLine();
    };
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
          if (wakeInputGainNode) wakeInputGainNode.gain.value = g;
        });
      }
      updateParlaThresholdLine();
    })();
    function startParlaMicPreviewIfEligible() {
      var sec = document.getElementById('section-parla');
      if (!sec || !sec.classList.contains('active')) return;
      if (_talkAgentMode === 'grok' || _talkAgentMode === 'legacy') {
        stopParlaMicPreview();
        return;
      }
      stopParlaMicPreview();
      var micEl = document.getElementById('mic');
      var micVal = micEl ? micEl.value : '';
      var wrap = document.getElementById('parlaPreviewMeterWrap');
      var msg = document.getElementById('parlaPreviewDisabledMsg');
      var isBrowserMic = micVal && micVal.indexOf('webmic_') === 0;
      var isLocalMic = micVal && micVal.indexOf('local_') === 0;
      if (isLocalMic) {
        if (wrap) wrap.style.display = '';
        if (msg) msg.style.display = 'none';
        updateParlaThresholdLine();
        return;
      }
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
      var previewOpen = (typeof getUserMediaWithFallback === 'function')
        ? getUserMediaWithFallback(micForBrowserCapture())
        : navigator.mediaDevices.getUserMedia(buildAudioCaptureConstraints(micForBrowserCapture()));
      previewOpen.then(function(stream){
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
          var gain = getParlaMonitorGain();
          window.g1UpdateTalkMicLevel(Math.min(255, peak * gain));
        }, 55);
      }).catch(function(err){
        var m = document.getElementById('parlaPreviewDisabledMsg');
        if (m) {
          m.style.display = 'block';
          m.textContent = (err && err.message) ? err.message : 'Microfono non disponibile: consenti l\\'accesso (Dispositivi) e riprova.';
        }
      });
    }
    const WAKE_SLICE_MS = __STT_WAKE_SLICE_MS__;
    const CMD_SLICE_MS  = __STT_CMD_SLICE_MS__;
    const CMD_SILENCE_MS = __STT_CMD_SILENCE_MS__;
    const CMD_MIN_VOICE_MS = __STT_CMD_MIN_VOICE_MS__;
    const CMD_TIMEOUT_MS = 45000;
    let _wakeSliceScheduled = false;
    let scheduleNextWakeSliceIfListening = function(){};
    /** Coda riproduzione TTS: evita che due risposte MP3 si sovrappongano. */
    let ttsPlaybackQueue = [];
    let ttsPlaybackBusy = false;
    const TTS_BEFORE_PLAY_GAP_MS = 60;
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
    let sbBrowserCtx = null, sbBrowserSource = null, sbBrowserAudioEl = null;
    function sbStopSoundboardPlayback(){
      try {
        try { if (sbBrowserSource) { sbBrowserSource.stop(); sbBrowserSource.disconnect(); } } catch(_){}
        sbBrowserSource = null;
        try { if (sbBrowserCtx) { sbBrowserCtx.close().catch(function(){}); } } catch(_){}
        sbBrowserCtx = null;
        if (sbBrowserAudioEl) { try { sbBrowserAudioEl.pause(); sbBrowserAudioEl.removeAttribute('src'); sbBrowserAudioEl.load(); } catch(_){} }
        sbBrowserAudioEl = null;
      } catch(_){}
    }
    function getSoundboardBrowserGain(){
      try {
        var v = parseFloat(localStorage.getItem('g1_soundboard_gain'));
        if (!isNaN(v) && v >= 0.35 && v <= 3.5) return v;
      } catch(_){}
      return 1.35;
    }
    function setSoundboardBrowserGain(v){
      try { localStorage.setItem('g1_soundboard_gain', String(v)); } catch(_){}
    }
    function playSoundboardBrowser(b64, fmt, onStart){
      sbStopSoundboardPlayback();
      if (!b64 || String(b64).length < 50) return;
      var mime = sbMimeForFmt(fmt);
      var gain = getSoundboardBrowserGain();
      var sinkId = resolveBrowserPlaybackSinkIdLikeSoundboard();
      function fireOnStart(){
        if (typeof onStart === 'function') {
          try { onStart(); } catch(_) {}
        }
      }
      var bin = atob(String(b64));
      var buf = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      var ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
      var ctxOpts = {};
      if (sinkId) { try { ctxOpts.sinkId = sinkId; } catch(_){} }
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) {
        var a0 = new Audio('data:'+mime+';base64,'+b64);
        sbBrowserAudioEl = a0;
        a0.addEventListener('playing', function onPlay(){ a0.removeEventListener('playing', onPlay); fireOnStart(); });
        applySinkThenPlay(a0, sinkId).catch(function(){});
        a0.onended = function(){ sbBrowserAudioEl = null; };
        return;
      }
      var ctx;
      try { ctx = new Ctx(ctxOpts); } catch(_) { ctx = new Ctx(); }
      sbBrowserCtx = ctx;
      if (ctx.resume) ctx.resume();
      ctx.decodeAudioData(ab, function(decoded){
        if (!sbBrowserCtx || sbBrowserCtx !== ctx) return;
        var src = ctx.createBufferSource();
        src.buffer = decoded;
        var gn = ctx.createGain();
        gn.gain.value = gain;
        src.connect(gn);
        gn.connect(ctx.destination);
        sbBrowserSource = src;
        src.onended = function(){
          sbBrowserSource = null;
          if (sbBrowserCtx === ctx) { try { ctx.close().catch(function(){}); } catch(_){} sbBrowserCtx = null; }
        };
        fireOnStart();
        src.start(0);
      }, function(){
        sbBrowserCtx = null;
        var a1 = new Audio('data:'+mime+';base64,'+b64);
        sbBrowserAudioEl = a1;
        a1.addEventListener('playing', function onPlay(){ a1.removeEventListener('playing', onPlay); fireOnStart(); });
        applySinkThenPlay(a1, sinkId).catch(function(){});
        a1.onended = function(){ sbBrowserAudioEl = null; };
      });
    }
    function sbFireSlotRobotIfConfigured(sd, slotIndex) {
      if (typeof slotIndex === 'number' && slotIndex < SB_ROBOT_START) return;
      if (!sd) return;
      var arm = (sd.robot_arm && String(sd.robot_arm).trim()) || '';
      var loco = (sd.robot_loco && String(sd.robot_loco).trim()) || '';
      var led = (sd.led_effect && String(sd.led_effect).trim()) || '';
      var teach = (sd.teaching_slot != null && String(sd.teaching_slot).trim()) || '';
      if (arm && teach) teach = '';
      if (!arm && !loco && !teach && !led) return;
      var ip = '192.168.123.161';
      try {
        var ls = localStorage.getItem('g1_robot_ip');
        if (ls && ls.trim()) ip = ls.trim();
      } catch(_) {}
      if (led) {
        fetch('/api/led', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ effect: led }) }).catch(function(){});
      }
      if (teach) {
        fetch('/api/explore-teachings/play', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name: teach }) }).catch(function(){});
      }
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
              var limiter = ctx.createDynamicsCompressor();
              limiter.threshold.value = -3;
              limiter.knee.value = 6;
              limiter.ratio.value = 20;
              limiter.attack.value = 0.002;
              limiter.release.value = 0.05;
              src.connect(gn);
              gn.connect(limiter);
              limiter.connect(ctx.destination);
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
    /** Vincoli leggeri per USB/wireless (es. DJI Mic): evita hang del browser con processing aggressivo. */
    function buildAudioCaptureConstraintsMinimal(deviceIdExact) {
      const a = {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
      };
      if (deviceIdExact && String(deviceIdExact).length > 5) {
        a.deviceId = { ideal: deviceIdExact };
      }
      return { audio: a };
    }
    async function getUserMediaWithFallback(deviceId, timeoutMs) {
      timeoutMs = timeoutMs || 9000;
      function withTimeout(promise) {
        return Promise.race([
          promise,
          new Promise(function(_, reject) {
            setTimeout(function(){ reject(new Error('Microfono: timeout apertura (' + Math.round(timeoutMs / 1000) + 's)')); }, timeoutMs);
          })
        ]);
      }
      var attempts = [
        buildAudioCaptureConstraints(deviceId),
        buildAudioCaptureConstraintsMinimal(deviceId),
        { audio: deviceId ? { deviceId: { ideal: deviceId }, channelCount: 1 } : { channelCount: 1 } }
      ];
      var lastErr = null;
      for (var i = 0; i < attempts.length; i++) {
        try {
          return await withTimeout(navigator.mediaDevices.getUserMedia(attempts[i]));
        } catch (e) {
          lastErr = e;
        }
      }
      throw lastErr || new Error('Microfono non disponibile');
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
        _listenHumGain.gain.value = 0.03;
        var lfo = _listenHumCtx.createOscillator();
        lfo.type = 'sine';
        lfo.frequency.value = 2;
        var lfoGain = _listenHumCtx.createGain();
        lfoGain.gain.value = 0.015;
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
        const t0 = ctx.currentTime;
        const master = ctx.createGain();
        master.gain.value = 0.78;
        master.connect(ctx.destination);
        function wakeNote(freq, delay, dur, peak){
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.type = 'triangle';
          o.frequency.value = freq;
          g.gain.setValueAtTime(0.0001, t0 + delay);
          g.gain.exponentialRampToValueAtTime(Math.max(0.08, peak), t0 + delay + 0.035);
          g.gain.exponentialRampToValueAtTime(0.0001, t0 + delay + dur);
          o.connect(g);
          g.connect(master);
          o.start(t0 + delay);
          o.stop(t0 + delay + dur + 0.03);
        }
        /* Arpeggio maggiore con nona (C-E-G + D alta): tono chiaro, “jazz / positivo” */
        wakeNote(523.25, 0.0, 0.2, 0.52);
        wakeNote(659.25, 0.1, 0.2, 0.55);
        wakeNote(783.99, 0.2, 0.2, 0.58);
        wakeNote(1174.66, 0.3, 0.26, 0.5);
        wakeNote(1046.5, 0.42, 0.16, 0.38);
        setTimeout(function(){ ctx.close().catch(function(){}); }, 950);
      } catch(_){}
    }
    function resetWakeCommandMode(){
      wakeCommandMode = false;
      stopListeningHum();
      if (wakeCommandIdleTimer) { clearTimeout(wakeCommandIdleTimer); wakeCommandIdleTimer = null; }
      wakeQueuedBlob = null;
      if (wakeActiveMr) {
        wakeDiscardCurrentSlice = true;
        try { if (wakeActiveMr.state !== 'inactive') wakeActiveMr.stop(); } catch(_){}
      }
    }
    /** Dopo la risposta vocale: spegne «Hey G1 continuo» (solo se l'utente vuole stop esplicito). */
    function disableWakeListenAfterResponse(){
      var el = document.getElementById('wakeListenToggle');
      var wtl = document.getElementById('wakeToggleLabel');
      var st = document.getElementById('wakeListenStatus');
      if (el && el.checked) {
        el.checked = false;
        if (wtl) wtl.textContent = 'OFF';
      }
      stopWakeRecorder();
      resetWakeCommandMode();
      setRobotLed('idle');
      if (st) st.textContent = 'Ascolto disattivato — riattiva «Hey G1 continuo» per parlare di nuovo';
      wakeLog('Risposta finita: ascolto disattivato', '#71717a');
      updateActiveMicIndicator();
    }
    function clearWakePipelineLock(){
      wakeAudioInFlight = false;
      wakeListenPending = false;
      if (wakeResponseTimeout) { clearTimeout(wakeResponseTimeout); wakeResponseTimeout = null; }
    }
    /** Dopo TTS: resta in ascolto per domande successive (presentazione / dialogo). */
    function resumeWakeListenAfterResponse(){
      clearWakePipelineLock();
      wakeQueuedBlob = null;
      wakeDiscardCurrentSlice = false;
      _wakeDropSlicesAfterTts = 0;
      setRobotLed('listening');
      var el = document.getElementById('wakeListenToggle');
      var st = document.getElementById('wakeListenStatus');
      if (!el || !el.checked) return;
      wakeCommandMode = true;
      startWakeCommandIdleTimer();
      if (st) st.textContent = "Ti ascolto\u2026 puoi fare un'altra domanda.";
      setTimeout(function(){ startListeningHum(); }, 250);
      scheduleNextWakeSliceIfListening();
      wakeLog('Pronto per altra domanda', '#14b8a6');
    }
    function startWakeCommandIdleTimer(){
      if (wakeCommandIdleTimer) clearTimeout(wakeCommandIdleTimer);
      wakeCommandIdleTimer = setTimeout(function(){
        if (wakeCommandMode) {
          wakeLog('Timeout comando, torno in ascolto wake', '#71717a');
          resetWakeCommandMode();
          scheduleNextWakeSliceIfListening();
        }
      }, CMD_TIMEOUT_MS);
    }
    function stopWakeLevelMeter(){
      if (wakeLevelSampleInterval) { clearInterval(wakeLevelSampleInterval); wakeLevelSampleInterval = null; }
      if (wakeLevelCtx) { try { wakeLevelCtx.close(); } catch(_){} wakeLevelCtx = null; }
      wakeAnalyser = null;
      wakeInputGainNode = null;
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
        const micGain = wakeLevelCtx.createGain();
        micGain.gain.value = getParlaMonitorGain();
        wakeInputGainNode = micGain;
        const dest = wakeLevelCtx.createMediaStreamDestination();
        src.connect(hp);
        hp.connect(comp);
        comp.connect(micGain);
        micGain.connect(wakeAnalyser);
        micGain.connect(dest);
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
    var WAKE_POST_TTS_PAUSE_MS = 350;
    var _wakeDropSlicesAfterTts = 0;
    function setRobotLed(state){
      try { fetch('/api/led', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({state:state})}); } catch(e){}
    }
    function onWakeResponseDone(){
      setTimeout(function(){
        resumeWakeListenAfterResponse();
      }, WAKE_POST_TTS_PAUSE_MS);
    }
    let wakeResponseTimeout = null;
    function ttsDestFromUi() {
      const ttsEl = document.getElementById('ttsPlayDest');
      const wantServer = ttsEl && ttsEl.value === 'server';
      if (wantServer) {
        return {
          playOn: 'server',
          deviceId: (serverTtsDeviceId !== null && !isNaN(serverTtsDeviceId)) ? serverTtsDeviceId : null
        };
      }
      return { playOn: 'browser', deviceId: null };
    }
    function trySendWakeChunk(blob, skipWakeForBlob){
      if (!blob || blob.size < WS_AUDIO_MIN_BYTES) { scheduleNextWakeSliceIfListening(); return; }
      if (!document.getElementById('wakeListenToggle').checked) return;
      if (isRecording) { scheduleNextWakeSliceIfListening(); return; }
      if (!ws || ws.readyState !== WebSocket.OPEN) { scheduleNextWakeSliceIfListening(); return; }
      var sk = (typeof skipWakeForBlob === 'boolean') ? skipWakeForBlob : !!wakeCommandMode;
      if (wakeAudioInFlight) {
        wakeQueuedBlob = { blob: blob, skipWake: sk };
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
      }, 70000);
      const fr = new FileReader();
      fr.onload = function(){
        const b64 = arrayBufferToBase64(fr.result);
        try {
          if (sk) { stopListeningHum(); playStopChime(); startThinkingFeedback(); }
          const td = ttsDestFromUi();
          lastPlayOn = td.playOn;
          sendAudioOverWs(b64, wakeMimeType, { playOn: td.playOn, skipWake: sk, deviceId: td.deviceId });
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
      wakeDiscardCurrentSlice = false;
      _wakeSliceScheduled = false;
      _wakeDropSlicesAfterTts = 0;
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
      listenServerWakeLatched = false;
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
        listenServerWakeLatched = false;
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
              if (listenServerWakeLatched) return;
              listenServerWakeLatched = true;
              if (d.response && String(d.response).trim()) {
                var resEl = document.getElementById('result');
                if (resEl) resEl.innerHTML = '<div class="ok"><strong>Tu:</strong> ' + (d.text||'').replace(/</g,'&lt;') + '<br><strong>G1:</strong> ' + (d.response||'').replace(/</g,'&lt;') + '</div>';
              } else {
                playWakeChime();
              }
              if (st) st.textContent = 'Dì Hey G1 + domanda';
              return;
            }
            listenServerWakeLatched = false;
            if (d.response) {
              var resEl = document.getElementById('result');
              if (resEl) resEl.innerHTML = '<div class="ok"><strong>Tu:</strong> ' + (d.text||'').replace(/</g,'&lt;') + '<br><strong>G1:</strong> ' + (d.response||'').replace(/</g,'&lt;') + '</div>';
              if (st) st.textContent = 'In ascolto per «Hey G1»…';
            }
            /* Risposta TTS: solo sul robot (ws/listen), non sul browser. */
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
        if (typeof getUserMediaWithFallback === 'function') {
          wakeRawStream = await getUserMediaWithFallback(micForBrowserCapture());
        } else {
          wakeRawStream = await navigator.mediaDevices.getUserMedia(buildAudioCaptureConstraints(micForBrowserCapture()));
        }
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
        if (_wakeSliceScheduled) return;
        if (!wakeListenActive) return;
        const tg = document.getElementById('wakeListenToggle');
        if (!tg || !tg.checked) return;
        if (isRecording) return;
        _wakeSliceScheduled = true;
        setTimeout(function(){ _wakeSliceScheduled = false; runWakeSlice(); }, 40);
      };
      function runWakeSlice(){
        if (!wakeListenActive || !document.getElementById('wakeListenToggle').checked) return;
        if (isRecording) { setTimeout(runWakeSlice, 350); return; }
        if (!wakeStream) return;
        if (wakeActiveMr) return;
        const isCmd = wakeCommandMode;
        const sliceMs = isCmd ? CMD_SLICE_MS : WAKE_SLICE_MS;
        const mr = new MediaRecorder(wakeStream, { mimeType: wakeMimeType, audioBitsPerSecond: 128000 });
        wakeActiveMr = mr;
        const ch = [];
        wakeSlicePeak = 0;
        let voiceDurationMs = 0, lastVoiceTs = 0, stopped = false;
        let sliceInterval = null;
        function stopMr(){
          if (stopped) return; stopped = true;
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
            if (typeof window.g1UpdateTalkMicLevel === 'function') window.g1UpdateTalkMicLevel(s);
            const th = getWakeVoiceThreshold();
            if (s >= th) { voiceDurationMs += 50; lastVoiceTs = Date.now(); }
            if (isCmd && voiceDurationMs >= CMD_MIN_VOICE_MS && lastVoiceTs > 0 && (Date.now() - lastVoiceTs >= CMD_SILENCE_MS)) {
              stopMr();
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
          if (wakeDiscardCurrentSlice) {
            wakeDiscardCurrentSlice = false;
            return;
          }
          const blob = new Blob(ch, { type: wakeMimeType });
          var voiced = !wakeAnalyser || wakeSlicePeak >= getWakeVoiceThreshold();
          if (!isCmd) {
            scheduleNextWakeSliceIfListening();
            if (blob.size >= WS_AUDIO_MIN_BYTES && voiced) trySendWakeChunk(blob, false);
          } else {
            if (blob.size >= WS_AUDIO_MIN_BYTES && voiced) trySendWakeChunk(blob, true);
            else scheduleNextWakeSliceIfListening();
          }
        };
        mr.start();
        setTimeout(function(){ stopMr(); }, sliceMs);
      }
      runWakeSlice();
      const st = document.getElementById('wakeListenStatus');
      if (st) st.textContent = 'In ascolto per \u00abHey G1\u00bb\u2026';
    }
    const wakeListenToggleEl = document.getElementById('wakeListenToggle');
    if (wakeListenToggleEl) {
      wakeListenToggleEl.onchange = function(){
        if (wakeListenToggleEl.checked) {
          window.g1SetTalkAgentMode('legacy');
          const st = document.getElementById('wakeListenStatus');
          if (st) st.textContent = 'Avvio ascolto…';
          startWakeRecorder();
        } else {
          stopWakeRecorder();
          resetWakeCommandMode();
          if (_talkAgentMode === 'legacy') applyTalkAgentLayout('none');
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
      var _ha = document.getElementById('hintAccess'); if (_ha) _ha.style.display = 'none';
      var _aw = document.getElementById('allowWrap'); if (_aw) _aw.style.display = 'none';
      var _dw = document.getElementById('devicesWrap'); if (_dw) _dw.style.display = 'none';
      var _swm = document.getElementById('secureWarnMore');
      if (_swm) _swm.onclick = (e)=>{ e.preventDefault(); const d=document.getElementById('secureWarnDetails'); d.style.display = d.style.display==='none' ? 'block' : 'none'; };
    }
    if (!navigator.mediaDevices) {
      document.getElementById('secureContextWarn').style.display = 'block';
      var _ha2 = document.getElementById('hintAccess'); if (_ha2) _ha2.style.display = 'none';
      var _aw2 = document.getElementById('allowWrap'); if (_aw2) _aw2.style.display = 'none';
      var _dw2 = document.getElementById('devicesWrap'); if (_dw2) _dw2.style.display = 'none';
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
        let resumeWakeListen = false;
        try {
          if (wakeListenPending) {
            wakeListenPending = false;
            if (r.wake_miss) {
              wakeAudioInFlight = false;
              if (wakeResponseTimeout) { clearTimeout(wakeResponseTimeout); wakeResponseTimeout = null; }
              var sttTxt = String(r.text||'').trim();
              wakeLog(sttTxt ? 'STT: "'+sttTxt+'" \u2192 miss (no wake word)' : 'silenzio / no speech', '#71717a');
              if (btn) btn.disabled = false;
              resumeWakeListen = true;
              return;
            }
            if (r.wake_cmd_inline) {
              wakeDiscardCurrentSlice = true;
              wakeQueuedBlob = null;
              if (wakeActiveMr) {
                try { if (wakeActiveMr.state !== 'inactive') wakeActiveMr.stop(); } catch(_){}
              }
              wakeLog('WAKE + CMD inline: "'+String(r.text||'')+'"', '#22c55e');
              playWakeChime();
            }
            if (r.wake_ack) {
              clearWakePipelineLock();
              if (wakeCommandMode) {
                if (btn) btn.disabled = false;
                scheduleNextWakeSliceIfListening();
                return;
              }
              wakeDiscardCurrentSlice = true;
              wakeQueuedBlob = null;
              _wakeSliceScheduled = false;
              _wakeDropSlicesAfterTts = 0;
              if (wakeActiveMr) {
                try { if (wakeActiveMr.state !== 'inactive') wakeActiveMr.stop(); } catch(_){}
              }
              if (r.response && String(r.response).trim()) {
                wakeLog('WAKE: ' + String(r.response), '#22c55e');
                document.getElementById('result').innerHTML = '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+'</div>';
                var ackHasTts = lastPlayOn === 'browser' && r.audio_base64 && String(r.audio_base64).length > 50;
                if (ackHasTts) {
                  enqueueTtsPlayback(r.audio_base64, function(){
                    resumeWakeListenAfterResponse();
                  });
                  if (btn) btn.disabled = false;
                  return;
                }
              } else {
                wakeLog('WAKE! Ti ascolto\u2026', '#22c55e');
                playWakeChime();
              }
              resumeWakeListenAfterResponse();
              if (btn) btn.disabled = false;
              return;
            }
            if (!r.response && r.message) {
              clearWakePipelineLock();
              wakeLog('msg: '+r.message, '#f59e0b');
              const wst = document.getElementById('wakeListenStatus');
              if (wst) wst.textContent = wakeCommandMode ? 'Ti ascolto\u2026' : 'In ascolto per \u00abHey G1\u00bb\u2026';
              document.getElementById('result').innerHTML = '<div class="warn">'+r.message+'</div>';
              if (btn) btn.disabled = false;
              resumeWakeListen = true;
              return;
            }
            if (r.text) wakeLog('CMD: "'+String(r.text||'')+'" \u2192 risposta', '#14b8a6');
          }
          if (btn) btn.disabled = false;
          recordingServerJetson = false;
          document.getElementById('recDebug').textContent = r.text ? '' : (r.message || '');
          document.getElementById('recDebug').style.color = r.message ? '#f59e0b' : '#71717a';
          const msg = r.message ? '<div class="warn">'+r.message+'</div>' : '';
          const dur = r.duration_ms ? ' <span style="color:#71717a;font-size:12px;">('+r.duration_ms+' ms)</span>' : '';
          document.getElementById('result').innerHTML = msg + '<div><b>Hai detto:</b> '+(r.text||'')+'</div><div><b>Risposta:</b> '+(r.response||'')+dur+'</div>';
          const hasTts = lastPlayOn === 'browser' && r.audio_base64 && String(r.audio_base64).length > 50;
          const hasServerTts = lastPlayOn === 'server' && r.response && String(r.response).trim().length > 0;
          if (r.response && String(r.response).trim()) {
            if (hasTts) {
              enqueueTtsPlayback(r.audio_base64, onWakeResponseDone);
            } else if (hasServerTts) {
              setTimeout(onWakeResponseDone, 1200);
            } else {
              onWakeResponseDone();
            }
          } else {
            clearWakePipelineLock();
            var wt = document.getElementById('wakeListenToggle');
            if (wt && wt.checked) resumeWakeListenAfterResponse();
          }
        } finally {
          if (resumeWakeListen) {
            wakeDiscardCurrentSlice = false;
            setRobotLed('listening');
            scheduleNextWakeSliceIfListening();
          }
        }
      } else if(d.type==='wake_chime'){
        if (!wakeCommandMode) { playWakeChime(); wakeLog('Hey G1 rilevato, elaboro...', '#22c55e'); }
      } else if(d.type==='error'){
        stopThinkingFeedback();
        clearTtsPlaybackQueue();
        wakeAudioInFlight = false;
        wakeQueuedBlob = null;
        if (btn) btn.disabled = false;
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
      if (btn) btn.classList.add('recording');
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
        if (btn) btn.classList.remove('recording');
        clearAllIntervals();
        document.getElementById('result').innerHTML = '<div class="warn">Invio start fallito.</div>';
      }
    }
    /* connect() dopo init UI — vedi fine script */
    (function loadServerTtsConfig(){
      fetch('/api/config').then(function(r){ return r.json(); }).then(function(cfg){
        var mic = cfg && cfg.microphone;
        var micIsBrowser = mic && mic.type === 'network' && mic.value && mic.value !== 'web_wait';
        var sp = cfg && cfg.speaker;
        var hasLocalSpk = sp && sp.type === 'local' && (sp.device_id !== undefined && sp.device_id !== null && sp.device_id !== '');
        var tts = document.getElementById('ttsPlayDest');
        if (micIsBrowser && tts) {
          tts.value = 'browser';
        } else if (hasLocalSpk) {
          serverTtsDeviceId = parseInt(sp.device_id, 10);
          if (tts && !isNaN(serverTtsDeviceId)) tts.value = 'server';
          var sb = document.getElementById('sbPlayDest');
          if (sb && !isNaN(serverTtsDeviceId)) sb.value = 'server';
        } else {
          var sb0 = document.getElementById('sbPlayDest');
          if (sb0) { sb0.value = 'browser'; }
          if (tts) tts.value = 'browser';
        }
        var wrap = document.getElementById('ttsOutputWrap');
        if (wrap) wrap.style.display = 'block';
        updateSbBrowserRowVisibility();
      }).catch(function(){});
    })();

    async function ensureMicPermissionForEnumerate(){
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
      if (_micPermissionGranted) return true;
      try {
        if (navigator.permissions && navigator.permissions.query) {
          var pst = await navigator.permissions.query({ name: 'microphone' });
          if (pst.state === 'granted') {
            _micPermissionGranted = true;
            return true;
          }
          if (pst.state === 'denied') return false;
        }
      } catch(_){}
      try {
        var stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        stream.getTracks().forEach(function(t){ t.stop(); });
        _micPermissionGranted = true;
        return true;
      } catch(_) {
        return false;
      }
    }

    function preferBrowserMicOnClient(){
      var micSel = document.getElementById('mic');
      if (!micSel || !micSel.options || !micSel.options.length) return;
      var pick = -1;
      for (var i = 0; i < micSel.options.length; i++) {
        var v = micSel.options[i].value;
        if (v && v.indexOf('webmic_') === 0 && v.length > 7) {
          var txt = (micSel.options[i].textContent || '').toLowerCase();
          if (txt.indexOf('dji') >= 0) { pick = i; break; }
          if (txt.indexOf('realsense') >= 0 && pick < 0) pick = i;
          if (pick < 0) pick = i;
        }
      }
      if (pick >= 0) {
        micSel.selectedIndex = pick;
        updateActiveMicIndicator();
        var st = document.getElementById('deviceStatus');
        if (st) st.textContent = 'Microfono: ' + (micSel.options[pick].textContent || 'browser') + ' — premi Salva per memorizzarlo sul server.';
        if (typeof startParlaMicPreviewIfEligible === 'function') setTimeout(startParlaMicPreviewIfEligible, 80);
      }
    }

    async function requestAndLoadDevices(){
      if (!navigator.mediaDevices) return;
      const statusEl = document.getElementById('deviceStatus');
      if (statusEl) statusEl.textContent = 'Microfono: attivazione…';
      try {
        var ok = await ensureMicPermissionForEnumerate();
        if (!ok) throw new Error('permesso negato');
        await loadDevices({ ensureMic: false, preferBrowser: true });
      } catch(e) {
        if (statusEl) statusEl.textContent = 'Microfono: permesso negato — abilita il mic per questo sito nelle impostazioni browser.';
        await loadDevices({ ensureMic: false, preferBrowser: false });
        updateActiveMicIndicator();
      }
    }
    window.initClientMic = requestAndLoadDevices;

    function applyMicListToUi(micSel, spkSel, serverData, mics, spks, seq){
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
      if (seq !== _loadDevicesSeq) return false;
      micSel.innerHTML = micHtml;
      micSel.onchange = function(){
        applyLocalMicDefaultsIfUnset(micSel.value);
        updateActiveMicIndicator();
        autoSaveMicConfigFromUi();
        stopParlaMicPreview();
        if (_talkAgentMode === 'grok') {
          window.g1GrokVoiceStop(true);
          var gt = document.getElementById('grokVoiceToggle');
          if (gt) gt.checked = true;
          setTimeout(function(){ window.g1GrokVoiceStart(); }, 200);
        } else if (_talkAgentMode === 'legacy') {
          stopWakeRecorder();
          setTimeout(function(){ startWakeRecorder(); }, 200);
        } else {
          setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 120);
        }
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
        autoSaveMicConfigFromUi();
        if (_talkAgentMode === 'grok') {
          window.g1GrokVoiceStop(true);
          var gt = document.getElementById('grokVoiceToggle');
          if (gt) gt.checked = true;
          setTimeout(function(){ window.g1GrokVoiceStart(); }, 120);
        }
      };
      const sbOut = document.getElementById('sbOutput');
      if (sbOut) {
        sbOut.innerHTML = '<option value="default">Predefinito</option>' + spks.map(function(s,i){
          return '<option value="'+s.deviceId+'">'+escapeHtmlDevices(s.label || ('Output '+(i+1)))+'</option>';
        }).join('');
        sbOut.onchange = function(){ syncSpeakerFromSbOutput(); updateActiveMicIndicator(); };
      }
      return true;
    }

    function autoSaveMicConfigFromUi(){
      var micSel = document.getElementById('mic');
      var spkSel = document.getElementById('speaker');
      if (!micSel || !spkSel || !micSel.value) return;
      var body = { microphone: buildMicCfgFromSelect(micSel.value), speaker: buildSpkCfgFromSelect(spkSel.value) };
      fetch('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).catch(function(){});
    }

    function finishMicDeviceUi(micSel, spkSel, statusEl, mics, spks, serverData, seq){
      updateActiveMicIndicator();
      const nJet = ((serverData.microphones || []).filter(function(m){ return m && String(m.value||'').indexOf('local_') === 0; }).length)
        + ((serverData.speakers || []).filter(function(s){ return s && String(s.value||'').indexOf('local_') === 0; }).length);
        if (statusEl) {
          if (!_micPermissionGranted && mics.length === 0 && (isSecure || isLocalhost)) {
            statusEl.textContent = "Microfono: consenti l'accesso nelle impostazioni del browser per questo sito.";
          } else if (mics.length) {
            var selOpt = micSel.options[micSel.selectedIndex];
            statusEl.textContent = 'Microfono attivo: ' + ((selOpt && selOpt.textContent) ? selOpt.textContent.trim() : mics.length + ' device');
          } else {
            statusEl.textContent = nJet ? ('Jetson: '+nJet+' device · Browser: nessun mic') : ('Browser: nessun microfono rilevato');
          }
        }
      fetch('/api/config').then(function(r){ return r.json(); }).then(function(cfg){
        if (seq !== _loadDevicesSeq || !cfg || !micSel) return;
        var restoredMic = false;
        if (cfg.microphone && cfg.microphone.value) {
          var mv = cfg.microphone.value;
          if (cfg.microphone.type === 'network' && mv && mv !== 'web_wait' && mv.indexOf('local_') !== 0 && mv.indexOf('net_') !== 0) {
            for (var i = 0; i < micSel.options.length; i++) {
              var o = micSel.options[i];
              if (o.value.indexOf('webmic_') === 0 && decodeURIComponent(o.value.slice(7)) === mv) {
                micSel.selectedIndex = i;
                restoredMic = true;
                break;
              }
            }
          } else if (mv) {
            try {
              micSel.value = mv;
              restoredMic = micSel.value === mv;
            } catch(_){}
          }
        }
        if (cfg.speaker && cfg.speaker.value) {
          try { spkSel.value = cfg.speaker.value; } catch(_){}
        }
        if (!restoredMic && (!cfg.microphone || !cfg.microphone.value || cfg.microphone.value === 'web_wait')) {
          preferBrowserMicOnClient();
        }
        applyLocalMicDefaultsIfUnset(micSel.value);
        var vsp = spkSel.value;
        lastSinkId = (vsp && vsp.indexOf('browser_') === 0 && vsp !== 'browser_default') ? vsp.replace(/^browser_/, '') : null;
        syncSbOutputFromSpeaker();
        updateActiveMicIndicator();
        if (statusEl) {
          var selected = micSel.options[micSel.selectedIndex];
          statusEl.textContent = 'Microfono attivo: ' + ((selected && selected.textContent) ? selected.textContent.trim() : '—');
        }
        if (typeof startParlaMicPreviewIfEligible === 'function') setTimeout(startParlaMicPreviewIfEligible, 120);
      }).catch(function(){ updateActiveMicIndicator(); });
    }

    async function loadDevices(opts){
      opts = opts || {};
      if (!navigator.mediaDevices) return;
      const micSel = document.getElementById('mic');
      const spkSel = document.getElementById('speaker');
      const statusEl = document.getElementById('deviceStatus');
      if (!micSel || !spkSel) return;
      var seq = ++_loadDevicesSeq;
      if (opts.ensureMic !== false && (isSecure || isLocalhost)) {
        var permOk = await ensureMicPermissionForEnumerate();
        if (seq !== _loadDevicesSeq) return;
      }
      let serverData = { microphones: [], speakers: [], hardware_probe: null };
      try {
        var devs = await navigator.mediaDevices.enumerateDevices();
        if (seq !== _loadDevicesSeq) return;
        var mics = devs.filter(function(d){ return d.kind === 'audioinput' && d.deviceId; });
        var spks = devs.filter(function(d){ return d.kind === 'audiooutput' && d.deviceId; });
        try {
          var r = await Promise.race([
            fetch('/api/devices?all=1').then(function(resp){ return resp.ok ? resp.json() : serverData; }),
            new Promise(function(resolve){ setTimeout(function(){ resolve(serverData); }, 4000); })
          ]);
          if (r && typeof r === 'object') serverData = r;
        } catch(_) {}
        if (seq !== _loadDevicesSeq) return;
        _serverDevicesCache.microphones = serverData.microphones || [];
        _serverDevicesCache.speakers = serverData.speakers || [];
        _serverDevicesCache.hardware_probe = serverData.hardware_probe || null;
        updateHwProbe(serverData.hardware_probe);
        if (!applyMicListToUi(micSel, spkSel, serverData, mics, spks, seq)) return;
        finishMicDeviceUi(micSel, spkSel, statusEl, mics, spks, serverData, seq);
      } catch(e) {
        micSel.innerHTML = '<option value="">Errore: '+escapeHtmlDevices(e.message)+'</option>';
        spkSel.innerHTML = '<option value="browser_default">Riproduci qui</option>';
        const sbOut = document.getElementById('sbOutput');
        if (sbOut) {
          sbOut.innerHTML = '<option value="default">Predefinito</option>';
          sbOut.onchange = function(){ syncSpeakerFromSbOutput(); updateActiveMicIndicator(); };
        }
        if (statusEl) statusEl.textContent = 'Errore lettura dispositivi.';
        updateActiveMicIndicator();
      }
    }

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
      if (dr) dr.onclick = function(){ requestAndLoadDevices(); };
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
    (function(){
      var sbg = document.getElementById('sbGainSlider');
      var sbl = document.getElementById('sbGainLabel');
      if (sbg) {
        var gv = getSoundboardBrowserGain();
        sbg.value = String(Math.min(3, Math.max(0.5, gv)));
        if (sbl) sbl.textContent = parseFloat(sbg.value).toFixed(2) + '\u00d7';
        sbg.addEventListener('input', function(){
          var v = parseFloat(sbg.value);
          if (!isNaN(v)) { setSoundboardBrowserGain(v); if (sbl) sbl.textContent = v.toFixed(2) + '\u00d7'; }
        });
      }
    })();
    if (navigator.mediaDevices) {
      if (isLocalhost || isSecure) {
        requestAndLoadDevices();
        try {
          navigator.mediaDevices.addEventListener('devicechange', function(){
            loadDevices({ ensureMic: false, preferBrowser: true });
          });
        } catch(_){}
      } else {
        loadDevices({ ensureMic: false, preferBrowser: false });
      }
    }

    let knowledgeGroups = [];
    function knowledgeEsc(s) {
      return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    }
    function knowledgeGroupCounts() {
      var total = 0, active = 0;
      (knowledgeGroups || []).forEach(function(g) {
        var n = Object.keys(g.entries || {}).length;
        total += n;
        if (g.enabled !== false) active += n;
      });
      return { total: total, active: active };
    }
    function renderKnowledge() {
      var el = document.getElementById('knowledgeGroups');
      var cnt = document.getElementById('knowledgeCount');
      var counts = knowledgeGroupCounts();
      if (cnt) cnt.textContent = counts.total ? (counts.active + ' attivi / ' + counts.total + ' totali') : 'vuoto';
      if (!el) return;
      if (!knowledgeGroups.length) {
        el.innerHTML = '<span style="color:#71717a;">(nessun gruppo)</span>';
        return;
      }
      el.innerHTML = knowledgeGroups.map(function(g, gi) {
        var enabled = g.enabled !== false;
        var entries = g.entries || {};
        var rows = Object.entries(entries).map(function(pair) {
          var k = pair[0], v = pair[1];
          return '<div style="display:flex;align-items:center;gap:6px;margin:4px 0;font-size:12px;opacity:' + (enabled ? '1' : '0.55') + ';">' +
            '<span style="color:#9ca3af;min-width:120px;">' + knowledgeEsc(k) + '</span>' +
            '<span style="color:#e8eaed;flex:1;">' + knowledgeEsc(v.length > 40 ? v.substring(0, 40) + '...' : v) + '</span>' +
            '<button type="button" class="knowledgeDel" data-gi="' + gi + '" data-key="' + encodeURIComponent(k) + '" style="padding:2px 8px;background:rgba(239,68,68,0.3);color:#fca5a5;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Elimina</button></div>';
        }).join('') || '<div style="color:#71717a;font-size:12px;">(nessun pattern)</div>';
        return '<div class="knowledge-group" style="margin:10px 0;padding:10px;border:1px solid rgba(255,255,255,0.08);border-radius:10px;' + (enabled ? '' : 'opacity:0.75;') + '">' +
          '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">' +
          '<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;color:#e8eaed;flex:1;">' +
          '<input type="checkbox" class="knowledgeGroupToggle" data-gi="' + gi + '" ' + (enabled ? 'checked' : '') + ' style="accent-color:#14b8a6;" />' +
          '<input type="text" class="knowledgeGroupName" data-gi="' + gi + '" value="' + knowledgeEsc(g.name || g.id || '') + '" style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,0.15);background:#27272a;color:#fff;font-size:12px;" />' +
          '</label>' +
          '<button type="button" class="knowledgeGroupDel" data-gi="' + gi + '" style="padding:4px 10px;background:rgba(239,68,68,0.2);color:#fca5a5;border:none;border-radius:6px;cursor:pointer;font-size:11px;">Elimina gruppo</button>' +
          '</div>' + rows +
          '<div style="margin-top:8px;display:flex;gap:4px;flex-wrap:wrap;">' +
          '<input type="text" class="knowledgePattern" data-gi="' + gi + '" placeholder="Pattern" style="flex:1;min-width:100px;padding:6px;border-radius:6px;border:1px solid rgba(255,255,255,0.15);background:#27272a;color:#fff;font-size:12px;" />' +
          '<input type="text" class="knowledgeResponse" data-gi="' + gi + '" placeholder="Risposta" style="flex:2;min-width:140px;padding:6px;border-radius:6px;border:1px solid rgba(255,255,255,0.15);background:#27272a;color:#fff;font-size:12px;" />' +
          '<button type="button" class="knowledgeAdd" data-gi="' + gi + '" style="padding:6px 10px;background:#14b8a6;color:#0c0e14;border:none;border-radius:6px;cursor:pointer;font-size:11px;">Aggiungi</button>' +
          '</div></div>';
      }).join('');
      el.querySelectorAll('.knowledgeGroupToggle').forEach(function(cb) {
        cb.onchange = function() {
          var gi = +cb.dataset.gi;
          if (knowledgeGroups[gi]) knowledgeGroups[gi].enabled = !!cb.checked;
          renderKnowledge();
        };
      });
      el.querySelectorAll('.knowledgeGroupName').forEach(function(inp) {
        inp.oninput = function() {
          var gi = +inp.dataset.gi;
          if (knowledgeGroups[gi]) knowledgeGroups[gi].name = inp.value;
        };
      });
      el.querySelectorAll('.knowledgeGroupDel').forEach(function(btn) {
        btn.onclick = function() {
          var gi = +btn.dataset.gi;
          if (!confirm('Eliminare questo gruppo?')) return;
          knowledgeGroups.splice(gi, 1);
          renderKnowledge();
        };
      });
      el.querySelectorAll('.knowledgeDel').forEach(function(btn) {
        btn.onclick = function() {
          var gi = +btn.dataset.gi;
          var key = decodeURIComponent(btn.dataset.key || '');
          if (knowledgeGroups[gi] && knowledgeGroups[gi].entries) delete knowledgeGroups[gi].entries[key];
          renderKnowledge();
        };
      });
      el.querySelectorAll('.knowledgeAdd').forEach(function(btn) {
        btn.onclick = function() {
          var gi = +btn.dataset.gi;
          var wrap = btn.parentElement;
          var p = ((wrap.querySelector('.knowledgePattern') || {}).value || '').trim();
          var r = ((wrap.querySelector('.knowledgeResponse') || {}).value || '').trim();
          if (!p || !r) return;
          if (!knowledgeGroups[gi].entries) knowledgeGroups[gi].entries = {};
          knowledgeGroups[gi].entries[p] = r;
          renderKnowledge();
        };
      });
    }
    fetch('/api/knowledge').then(function(r) { return r.json(); }).then(function(d) {
      knowledgeGroups = (d.groups || []).map(function(g) {
        return {
          id: g.id || '',
          name: g.name || g.id || 'Gruppo',
          enabled: g.enabled !== false,
          entries: Object.assign({}, g.entries || {})
        };
      });
      if (!knowledgeGroups.length && d.entries) {
        knowledgeGroups = [{ id: 'general', name: 'Generale', enabled: true, entries: Object.assign({}, d.entries) }];
      }
      renderKnowledge();
    }).catch(function() {
      var c = document.getElementById('knowledgeCount');
      if (c) c.textContent = 'errore caricamento';
    });
    var knowledgeAddGroupEl = document.getElementById('knowledgeAddGroup');
    if (knowledgeAddGroupEl) knowledgeAddGroupEl.onclick = function() {
      var n = knowledgeGroups.length + 1;
      knowledgeGroups.push({ id: 'group_' + Date.now(), name: 'Gruppo ' + n, enabled: true, entries: {} });
      renderKnowledge();
    };
    var knowledgeSaveEl = document.getElementById('knowledgeSave');
    if (knowledgeSaveEl) knowledgeSaveEl.onclick = function() {
      fetch('/api/knowledge/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ groups: knowledgeGroups }) })
        .then(function(r) { return r.json(); })
        .then(function(d) {
          if (d.ok) {
            knowledgeSaveEl.textContent = 'Salvato!';
            setTimeout(function() { knowledgeSaveEl.textContent = 'Salva su server'; }, 2000);
          } else {
            alert(d.error || 'Errore');
          }
        })
        .catch(function(e) { alert('Errore: ' + e.message); });
    };

    let soundboardSlots = [];
    let sbTextMax = 280;
    let sbEditIdx = -1, sbEditAudio = null, sbEditFmt = '', sbEditAudioRaw = null;
    let sbEditAudioClean = null, sbEditFmtClean = 'mp3', sbEditAudioCleared = false;
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
        if(!sd.audio_base64_clean || sd.audio_base64_clean.length<=50) return;
        const b64 = sd.audio_base64_clean, fmt = sd.format_clean||'mp3';
        var audioDelay = parseInt(sd.audio_delay_ms, 10) || 0;
        var gestureDelay = parseInt(sd.gesture_delay_ms, 10) || 0;
        if (audioDelay < 0) audioDelay = 0;
        if (gestureDelay < 0) gestureDelay = 0;
        if (audioDelay > 15000) audioDelay = 15000;
        if (gestureDelay > 15000) gestureDelay = 15000;
        if (audioDelay === 0 && gestureDelay === 0) {
          playSoundboardBrowser(b64, fmt, function(){
            sbFireSlotRobotIfConfigured(sd, slotIndex);
          });
        } else {
          setTimeout(function(){
            playSoundboardBrowser(b64, fmt);
          }, Math.max(0, audioDelay));
          setTimeout(function(){
            sbFireSlotRobotIfConfigured(sd, slotIndex);
          }, Math.max(0, gestureDelay));
        }
      }
      if (s.audio_base64_clean && s.audio_base64_clean.length>50) {
        var arm0 = (s.robot_arm && String(s.robot_arm).trim()) || '';
        var loco0 = (s.robot_loco && String(s.robot_loco).trim()) || '';
        var led0 = (s.led_effect && String(s.led_effect).trim()) || '';
        var teach0 = (s.teaching_slot != null && String(s.teaching_slot).trim()) || '';
        if ((!arm0 && !loco0 && !led0 && !teach0) && typeof slotIndex === 'number') {
          fetch('/api/soundboard-slot/'+slotIndex).then(function(r){
            if (!r.ok) return Promise.resolve(s);
            return r.json();
          }).then(function(full){
            var merged = Object.assign({}, s, {
              robot_arm: (full && full.robot_arm) ? String(full.robot_arm) : '',
              robot_loco: (full && full.robot_loco) ? String(full.robot_loco) : '',
              led_effect: (full && full.led_effect) ? String(full.led_effect) : '',
              teaching_slot: (full && full.teaching_slot) ? String(full.teaching_slot) : '',
              audio_delay_ms: (full && full.audio_delay_ms != null) ? full.audio_delay_ms : 0,
              gesture_delay_ms: (full && full.gesture_delay_ms != null) ? full.gesture_delay_ms : 0
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
    const SB_AUDIO_COUNT = 50;
    const SB_ROBOT_START = 50;
    const SB_EXPLORE_GESTURE_PREFIX = 'explore::';
    var _sbArmGestureRefreshGen = 0;
    var _sbArmGestureRefreshPromise = null;
    function sbRemoveExploreGestureGroups(sel){
      if (!sel) return;
      sel.querySelectorAll('optgroup').forEach(function(og){
        if (og.id === 'sbExploreGesturesGroup' || og.label === 'Addestrati (Explore)') og.remove();
      });
    }
    function sbRefreshArmGestureOptions(){
      var sel = document.getElementById('sbModalArm');
      if (!sel) return Promise.resolve();
      if (_sbArmGestureRefreshPromise) return _sbArmGestureRefreshPromise;
      sbRemoveExploreGestureGroups(sel);
      var gen = ++_sbArmGestureRefreshGen;
      _sbArmGestureRefreshPromise = fetch('/api/explore-teachings')
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (gen !== _sbArmGestureRefreshGen) return;
          sbRemoveExploreGestureGroups(sel);
          var items = (d && d.ok && d.teachings) ? d.teachings : [];
          if (!items.length) return;
          var og = document.createElement('optgroup');
          og.id = 'sbExploreGesturesGroup';
          og.label = 'Addestrati (Explore)';
          items.forEach(function(t){
            var nm = String(t.name || '').trim();
            if (!nm) return;
            var opt = document.createElement('option');
            opt.value = SB_EXPLORE_GESTURE_PREFIX + nm;
            opt.textContent = nm;
            og.appendChild(opt);
          });
          sel.appendChild(og);
        })
        .catch(function(){})
        .finally(function(){
          if (gen === _sbArmGestureRefreshGen) _sbArmGestureRefreshPromise = null;
        });
      return _sbArmGestureRefreshPromise;
    }
    function sbGetModalGesture(){
      var v = (document.getElementById('sbModalArm')||{}).value || '';
      if (v.indexOf(SB_EXPLORE_GESTURE_PREFIX) === 0) {
        return { robot_arm: '', teaching_slot: v.slice(SB_EXPLORE_GESTURE_PREFIX.length) };
      }
      return { robot_arm: v, teaching_slot: '' };
    }
    function sbGestureFieldsForSave(){
      var g = sbGetModalGesture();
      return {
        robot_arm: g.robot_arm || '',
        teaching_slot: g.teaching_slot || ''
      };
    }
    function sbParseDelayMs(el){
      var v = parseInt((el && el.value) || '0', 10);
      if (!isFinite(v) || v < 0) return 0;
      return Math.min(v, 15000);
    }
    function sbSetModalGesture(s){
      var sel = document.getElementById('sbModalArm');
      if (!sel) return;
      var teach = (s && s.teaching_slot != null) ? String(s.teaching_slot).trim() : '';
      var arm = (s && s.robot_arm != null) ? String(s.robot_arm).trim() : '';
      var want = teach ? (SB_EXPLORE_GESTURE_PREFIX + teach) : (arm || '');
      var hasOpt = Array.prototype.some.call(sel.options, function(o){ return o.value === want; });
      if (want && !hasOpt && teach) {
        var og = sel.querySelector('#sbExploreGesturesGroup') || sel.querySelector('optgroup[label="Addestrati (Explore)"]');
        if (!og) {
          og = document.createElement('optgroup');
          og.id = 'sbExploreGesturesGroup';
          og.label = 'Addestrati (Explore)';
          sel.appendChild(og);
        }
        var opt = document.createElement('option');
        opt.value = want;
        opt.textContent = teach;
        og.appendChild(opt);
      }
      sel.value = want;
    }
    function sbSlotHasAudio(s){
      return (typeof s.has_clean === 'boolean') ? s.has_clean : !!(s.audio_base64_clean && s.audio_base64_clean.length > 50);
    }
    function sbBuildSlotHtml(s, i, zone){
      const hasAudio = sbSlotHasAudio(s);
      const hasRobot = zone === 'robot' && !!(s.has_robot || (s.teaching_slot && String(s.teaching_slot).trim()) || (s.robot_arm && String(s.robot_arm).trim()));
      let cls = 'sb-slot';
      let badgeColor = '#71717a';
      if (zone === 'robot') {
        if (hasAudio) { cls += ' sb-slot-filled-purple'; badgeColor = '#a78bfa'; }
      } else {
        if (hasAudio) { cls += ' sb-slot-filled-teal'; badgeColor = '#14b8a6'; }
      }
      let badgeTitle = 'Vuoto', badgeHtml = '&#8212;';
      if (hasAudio && hasRobot) { badgeTitle = 'Audio + movimento'; badgeHtml = '&#9654;&#129302;'; }
      else if (hasAudio){ badgeTitle = 'Audio'; badgeHtml = '&#9654;'; }
      else if (hasRobot) { badgeTitle = 'Movimento robot'; badgeHtml = '&#129302;'; }
      const badge = hasAudio
        ? '<span style="position:absolute;top:4px;right:4px;font-size:9px;font-weight:700;color:'+badgeColor+';" title="'+badgeTitle+'">'+badgeHtml+'</span>'
        : '<span style="position:absolute;top:4px;right:4px;font-size:10px;color:#71717a;" title="Vuoto">&#8212;</span>';
      const label = (s.text||'Comando '+(i+1)).replace(/\u003c/g,'&lt;').replace(/&/g,'&amp;').replace(/"/g,'&quot;');
      return '<div id="sb'+i+'" class="'+cls+'" role="button" tabindex="0" aria-label="Riproduci slot '+(i+1)+'">'+badge+'<span class="sb-slot-icon" style="pointer-events:none;">'+(s.icon||sbIconAt(i))+'</span><span class="sb-slot-text" style="pointer-events:none;">'+label+'</span><button type="button" class="sb-slot-edit" onclick="event.stopPropagation();editSoundboard('+i+')">✏️</button></div>';
    }
    function sbBindSlotEvents(){
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
    function renderSoundboard(){
      const gridAudio = document.getElementById('soundboardGridAudio');
      const gridRobot = document.getElementById('soundboardGridRobot');
      if (!gridAudio && !gridRobot) return;
      const audioSlots = soundboardSlots.slice(0, SB_AUDIO_COUNT);
      const robotSlots = soundboardSlots.slice(SB_ROBOT_START);
      if (gridAudio) gridAudio.innerHTML = audioSlots.map((s,i)=>sbBuildSlotHtml(s, i, 'audio')).join('');
      if (gridRobot) gridRobot.innerHTML = robotSlots.map((s,j)=>sbBuildSlotHtml(s, SB_ROBOT_START + j, 'robot')).join('');
      sbBindSlotEvents();
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
      var n = (d && typeof d.slot_count === 'number' && d.slot_count > 0) ? d.slot_count : 100;
      if (d && d.slots && d.slots.length) {
        soundboardSlots = d.slots.slice();
        while (soundboardSlots.length < n) {
          var i = soundboardSlots.length;
          soundboardSlots.push({ icon: sbIconAt(i), text: 'Comando '+(i+1), has_robot: false, has_clean: false });
        }
        var withAudio = soundboardSlots.filter(function(s){ return s.has_clean; }).length;
        var hint = document.getElementById('soundboardLoadHint');
        if (hint) { hint.style.display = 'none'; hint.textContent = ''; }
      } else {
        var hint2 = document.getElementById('soundboardLoadHint');
        if (hint2) { hint2.style.display = 'none'; hint2.textContent = ''; }
      }
      if (typeof d.text_max_len === 'number' && d.text_max_len > 0) { sbTextMax = d.text_max_len; const mx = document.getElementById('sbModalCharMax'); if(mx) mx.textContent = sbTextMax; }
      sbRefreshArmGestureOptions();
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
    soundboardSlots = Array.from({length: 100}, function(_, i){
      return { icon: sbIconAt(i), text: 'Comando '+(i+1), has_robot: false, has_clean: false };
    });
    renderSoundboard();
    sbLoadLiteSlots();
    (function(){
      var baseNav = window.g1ActivateClientSection;
      if (typeof baseNav !== 'function') return;
      window.g1ActivateClientSection = function(sec){
        baseNav(sec);
        if (sec === 'soundboard') {
          setTimeout(function(){
            if (!soundboardSlots.length) { sbLoadLiteSlots(); }
            else if (typeof window.renderSoundboard === 'function') { window.renderSoundboard(); }
          }, 0);
        }
        if (sec === 'parla') {
          setTimeout(function(){
            if (typeof requestAndLoadDevices === 'function') requestAndLoadDevices();
            if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible();
          }, 120);
        } else {
          if (typeof stopParlaMicPreview === 'function') stopParlaMicPreview();
        }
        if (sec === 'occhi') {
          setTimeout(function(){ if (typeof window.g1ClientCameraOnShow === 'function') window.g1ClientCameraOnShow(); }, 80);
        } else if (typeof window.g1ClientCameraOnHide === 'function') {
          window.g1ClientCameraOnHide();
        }
        if (sec === 'info' && typeof window.g1RefreshLanLinks === 'function') {
          setTimeout(window.g1RefreshLanLinks, 0);
        }
        if (sec === 'log' && typeof window.g1ClientLogOnShow === 'function') {
          setTimeout(window.g1ClientLogOnShow, 0);
        } else if (typeof window.g1ClientLogOnHide === 'function') {
          window.g1ClientLogOnHide();
        }
        if (sec === 'teaching' && typeof window.g1LoadExploreTeachings === 'function') {
          setTimeout(window.g1LoadExploreTeachings, 0);
        }
        return false;
      };
    })();
    window.g1PlayExploreTeaching = function(name){
      if (!name) return;
      fetch('/api/explore-teachings/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name })
      }).then(function(r){ return r.json(); }).then(function(d){
        if (!d.ok) alert('Teaching: ' + (d.message || 'errore'));
        if (typeof window.g1RefreshExploreTeachFsm === 'function') window.g1RefreshExploreTeachFsm();
      }).catch(function(e){ alert('Teaching: ' + (e.message || String(e))); });
    };
    window.g1PrepareExploreTeaching = function(){
      fetch('/api/robot-loco', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: 'arm_ready' })
      }).then(function(r){ return r.json(); }).then(function(d){
        if (!d.ok) alert('Prepara robot: ' + (d.message || 'errore'));
        if (typeof window.g1RefreshExploreTeachFsm === 'function') window.g1RefreshExploreTeachFsm();
      }).catch(function(e){ alert('Prepara robot: ' + (e.message || String(e))); });
    };
    window.g1RefreshExploreTeachFsm = function(){
      var el = document.getElementById('exploreTeachFsm');
      if (!el) return;
      fetch('/api/explore-teachings/status').then(function(r){ return r.json(); }).then(function(d){
        var sport = (d && d.sport) ? d.sport : {};
        var label = sport.sport_label || sport.sport_status || '—';
        var detail = sport.detail || '';
        var arm = d.arm_sdk_active ? ' · arm_sdk OCCUPATO (ferma VR/REC)' : '';
        var count = (d.teaching_count != null) ? (' · ' + d.teaching_count + ' teach') : '';
        el.textContent = 'Stato robot: ' + label + count + arm + (detail ? (' — ' + detail) : '');
        el.style.color = d.arm_sdk_active ? '#f87171' : '#71717a';
      }).catch(function(){ el.textContent = 'Stato robot: non disponibile'; });
    };
    window.g1StopExploreTeaching = function(){
      fetch('/api/explore-teachings/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
        .then(function(r){ return r.json(); })
        .then(function(d){ if (!d.ok) alert('Stop: ' + (d.message || 'errore')); })
        .catch(function(e){ alert('Stop: ' + (e.message || String(e))); });
    };
    window.g1PopulateExploreTeachingSelect = function(selectEl, selectedName){
      if (!selectEl) return Promise.resolve();
      return fetch('/api/explore-teachings')
        .then(function(r){ return r.json(); })
        .then(function(d){
          var items = (d && d.ok && d.teachings) ? d.teachings : ((d && d.custom) ? d.custom : []);
          var html = '<option value="">— Movimento Explore —</option>';
          items.forEach(function(t){
            var nm = String(t.name || '');
            var esc = nm.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
            html += '<option value="' + esc + '">' + esc + '</option>';
          });
          selectEl.innerHTML = html;
          if (selectedName) selectEl.value = selectedName;
        })
        .catch(function(){});
    };
    (function(){
      function bindExploreTeachListClicks(){
        var list = document.getElementById('exploreTeachList');
        if (!list || list._explorePlayBound) return;
        list._explorePlayBound = true;
        list.addEventListener('click', function(ev){
          var btn = ev.target && ev.target.closest ? ev.target.closest('button[data-explore-play]') : null;
          if (!btn || !list.contains(btn)) return;
          ev.preventDefault();
          var teachName = btn.getAttribute('data-explore-play') || '';
          if (typeof window.g1PlayExploreTeaching === 'function') window.g1PlayExploreTeaching(teachName);
        });
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindExploreTeachListClicks);
      else bindExploreTeachListClicks();
    })();
    window.g1LoadExploreTeachings = function(){
      var list = document.getElementById('exploreTeachList');
      var err = document.getElementById('exploreTeachErr');
      if (!list) return;
      if (typeof window.g1RefreshExploreTeachFsm === 'function') window.g1RefreshExploreTeachFsm();
      list.innerHTML = '<p class="hint" style="margin:0;font-size:12px;color:#52525b;">Caricamento…</p>';
      if (err) { err.style.display = 'none'; err.textContent = ''; }
      fetch('/api/explore-teachings')
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (!d.ok) {
            if (err) { err.style.display = 'block'; err.textContent = d.error || 'Elenco non disponibile'; }
            list.innerHTML = '<p class="hint" style="margin:0;font-size:12px;color:#52525b;">—</p>';
            return;
          }
          var items = d.teachings || [];
          window.g1PopulateExploreTeachingSelect(document.getElementById('sbModalExploreTeaching'));
          if (!items.length) {
            list.innerHTML = '<p class="hint" style="margin:0;font-size:12px;color:#52525b;">Nessun movimento nell&#39;app Explore sul robot. Registra prima dal telefono, poi Aggiorna.</p>';
            return;
          }
          list.innerHTML = items.map(function(t){
            var dur = (t.duration_s != null) ? (t.duration_s + 's') : '—';
            var nm = String(t.name || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
            var attrName = String(t.name || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/'/g,'&#39;');
            return '<div class="explore-teach-item"><span class="et-name">' + nm + '</span>'
              + '<span class="et-dur">' + dur + '</span>'
              + '<button type="button" data-explore-play="' + attrName + '">Play</button></div>';
          }).join('');
        })
        .catch(function(e){
          if (err) { err.style.display = 'block'; err.textContent = e.message || 'Rete'; }
          list.innerHTML = '';
        });
    };
    window.g1PlayUnitreeTeaching = window.g1PlayExploreTeaching;
    window.g1LoadUnitreeTeachings = window.g1LoadExploreTeachings;
    function updateSbModalStatus(){
      const st = document.getElementById('sbModalAudioStatus');
      if(!st) return;
      const kb = sbEditAudioClean ? Math.round((sbEditAudioClean.length||0)/1024) : 0;
      st.innerHTML = sbEditAudioClean ? '&#128266; <span style="color:#14b8a6;">Audio</span> '+kb+' KB' : '&#128266; <span style="color:#71717a;">Nessun audio</span>';
    }
    function editSoundboard(idx){
      sbEditIdx = idx;
      const s = soundboardSlots[idx] || {};
      document.getElementById('sbModalSlot').textContent = idx+1;
      document.getElementById('sbModalIcon').value = s.icon || sbIconAt(idx);
      document.getElementById('sbModalText').value = s.text || 'Comando '+(idx+1);
      document.getElementById('sbModalText').setAttribute('maxlength', String(sbTextMax));
      sbEditAudioRaw = null;
      sbEditAudioCleared = false;
      function applyFull(full){
        sbEditAudio = null; sbEditFmt = '';
        sbEditAudioClean = full.audio_base64_clean || full.audio_base64 || null;
        sbEditFmtClean = full.format_clean || full.format || 'mp3';
        sbEditAudioCleared = false;
        updateSbModalStatus();
        updateSbCharCount();
      }
      var armSel = document.getElementById('sbModalArm');
      var locoSel = document.getElementById('sbModalLoco');
      var ledSel = document.getElementById('sbModalLed');
      var robotBlock = document.getElementById('sbModalRobotBlock');
      var isRobotZone = idx >= SB_ROBOT_START;
      if (robotBlock) robotBlock.style.display = isRobotZone ? '' : 'none';
      function applyRobotFields(slotData){
        if (!isRobotZone) return;
        sbSetModalGesture(slotData || {});
        if (locoSel) locoSel.value = (slotData && slotData.robot_loco) ? slotData.robot_loco : '';
        if (ledSel) { ledSel.value = (slotData && slotData.led_effect) ? slotData.led_effect : ''; sbUpdateLedPreview(); }
        var adEl = document.getElementById('sbModalAudioDelay');
        var gdEl = document.getElementById('sbModalGestureDelay');
        if (adEl) adEl.value = String((slotData && slotData.audio_delay_ms != null) ? slotData.audio_delay_ms : 0);
        if (gdEl) gdEl.value = String((slotData && slotData.gesture_delay_ms != null) ? slotData.gesture_delay_ms : 0);
      }
      function loadRobotFields(slotData){
        if (!isRobotZone) return Promise.resolve();
        return sbRefreshArmGestureOptions().then(function(){ applyRobotFields(slotData); });
      }
      document.getElementById('sbModal').style.display = 'flex';
      if (s.audio_base64_clean && s.audio_base64_clean.length>50) {
        applyFull(s);
        loadRobotFields(s);
        return;
      }
      var st = document.getElementById('sbModalAudioStatus');
      if (st) st.innerHTML = 'Caricamento audio…';
      sbEditAudio = null; sbEditFmt = 'webm'; sbEditAudioClean = null; sbEditFmtClean = 'mp3';
      fetch('/api/soundboard-slot/'+idx).then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); }).then(function(full){
        applyFull(full);
        return loadRobotFields(full);
      }).catch(function(e){
        if (st) st.innerHTML = 'Errore: '+(e.message||String(e));
      });
    }
    window.editSoundboard = editSoundboard;
    var _sbLedColorMap = {
      'rainbow':'linear-gradient(90deg,red,orange,yellow,green,blue,violet)','breathe_blue':'#0078ff','breathe_green':'#00ff50',
      'breathe_red':'#ff2828','breathe_purple':'#a855f7','blink_red':'#ff0000','blink_blue':'#0064ff',
      'solid_blue':'#0078ff','solid_green':'#00ff50','solid_red':'#ff0000','solid_amber':'#ffb400',
      'solid_purple':'#a855f7','solid_cyan':'#00dcdc','solid_white':'#ffffff'
    };
    function sbUpdateLedPreview(){
      var sel = document.getElementById('sbModalLed');
      var prev = document.getElementById('sbModalLedPreview');
      if (!sel || !prev) return;
      var c = _sbLedColorMap[sel.value] || '#27272a';
      prev.style.background = c;
    }
    var _sbLedSel = document.getElementById('sbModalLed');
    if (_sbLedSel) _sbLedSel.onchange = sbUpdateLedPreview;
    const sbModalTextEl = document.getElementById('sbModalText');
    if (sbModalTextEl) { sbModalTextEl.oninput = updateSbCharCount; }
    function closeSbModal(){ document.getElementById('sbModal').style.display = 'none'; sbEditIdx = -1; sbEditAudio = null; sbEditAudioClean = null; sbEditFmtClean = 'mp3'; sbEditAudioCleared = false; }
    var sbModalCancelEl = document.getElementById('sbModalCancel');
    if (sbModalCancelEl) sbModalCancelEl.onclick = closeSbModal;
    var sbModalSaveEl = document.getElementById('sbModalSave');
    if (sbModalSaveEl) sbModalSaveEl.onclick = function(){
      if (sbEditIdx < 0) return;
      const icon = (document.getElementById('sbModalIcon').value || '🎤').trim().substring(0,20);
      const text = (document.getElementById('sbModalText').value || 'Comando '+(sbEditIdx+1)).trim().substring(0, sbTextMax);
      let robot_arm = '', robot_loco = '', led_effect = '', teaching_slot = '';
      let audio_delay_ms = 0, gesture_delay_ms = 0;
      if (sbEditIdx >= SB_ROBOT_START) {
        var gesture = sbGestureFieldsForSave();
        robot_arm = gesture.robot_arm;
        teaching_slot = gesture.teaching_slot;
        robot_loco = (document.getElementById('sbModalLoco')||{}).value || '';
        led_effect = (document.getElementById('sbModalLed')||{}).value || '';
        audio_delay_ms = sbParseDelayMs(document.getElementById('sbModalAudioDelay'));
        gesture_delay_ms = sbParseDelayMs(document.getElementById('sbModalGestureDelay'));
      }
      fetch('/api/soundboard', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({slot:sbEditIdx, icon, text, audio_base64: '', format: sbEditFmtClean||'mp3', audio_base64_clean: sbEditAudioClean||'', format_clean: sbEditFmtClean||'mp3', clear_audio: !!sbEditAudioCleared, robot_arm, robot_loco, led_effect, teaching_slot, audio_delay_ms, gesture_delay_ms})}).then(r=>r.json()).then(()=>{ sbLoadLiteSlots(); });
      closeSbModal();
    };
    var sbModalSynthEl = document.getElementById('sbModalSynth');
    if (sbModalSynthEl) sbModalSynthEl.onclick = async function(){
      const text = (document.getElementById('sbModalText').value||'').trim().substring(0, sbTextMax);
      if (!text) { alert('Scrivi il testo da sintetizzare'); return; }
      const btn = document.getElementById('sbModalSynth');
      btn.disabled = true;
      document.getElementById('sbModalAudioStatus').innerHTML = 'Generazione TTS...';
      try {
        const r = await fetch('/api/soundboard-synth', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})});
        const d = await r.json();
        if (d.ok) {
          sbEditAudio = null; sbEditFmt = '';
          sbEditAudioClean = d.audio_base64_clean || d.audio_base64 || null; sbEditFmtClean = d.format_clean || d.format || 'wav';
          sbEditAudioRaw = null; sbEditAudioCleared = false; updateSbModalStatus();
        }
        else alert(d.error || 'Errore TTS');
      } catch(e) { alert('Errore: '+e.message); }
      btn.disabled = false;
    };
    var sbModalRecordEl = document.getElementById('sbModalRecord');
    if (sbModalRecordEl) sbModalRecordEl.onclick = function(){
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
            sbEditAudio = null; sbEditFmt = '';
            sbEditAudioClean = b64; sbEditFmtClean = 'webm';
            sbEditAudioRaw = null; sbEditAudioCleared = false;
            updateSbModalStatus();
            document.getElementById('sbModalRecord').disabled = false;
          };
          fr.readAsArrayBuffer(blob);
        };
        mr.start(); setTimeout(()=>mr.stop(), 3000);
      }).catch(function(){ alert('Microfono non disponibile'); document.getElementById('sbModalRecord').disabled = false; });
    };
    var _sbModalFile = document.getElementById('sbModalFile');
    if (_sbModalFile) _sbModalFile.onchange = async function(e){
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const ext = (f.name.split('.').pop()||'').toLowerCase();
      const mime = {mp3:'audio/mpeg',wav:'audio/wav',ogg:'audio/ogg',webm:'audio/webm',m4a:'audio/mp4'}[ext] || f.type || 'audio/mpeg';
      const buf = await f.arrayBuffer();
      const b64 = arrayBufferToBase64(buf);
      document.getElementById('sbModalAudioStatus').innerHTML = 'File caricato';
      sbEditAudio = b64; sbEditFmt = ext || 'mp3';
      sbEditAudioClean = b64; sbEditFmtClean = ext || 'mp3';
      sbEditAudioRaw = null; sbEditAudioCleared = false;
      updateSbModalStatus();
      e.target.value = '';
    };
    var sbModalClearEl = document.getElementById('sbModalClear');
    if (sbModalClearEl) sbModalClearEl.onclick = function(){ sbEditAudio = null; sbEditFmt = ''; sbEditAudioClean = null; sbEditFmtClean = 'mp3'; sbEditAudioRaw = null; sbEditAudioCleared = true; updateSbModalStatus(); };
    var sbModalTtsEl = document.getElementById('sbModalTts');
    if (sbModalTtsEl) sbModalTtsEl.onclick = async function(){
      if (!sbEditAudioClean || sbEditAudioClean.length < 100) { alert('Serve prima un audio (registra o importa)'); return; }
      const btn = document.getElementById('sbModalTts');
      btn.disabled = true;
      document.getElementById('sbModalAudioStatus').innerHTML = 'Riprocessamento con TTS...';
      try {
        const r = await fetch('/api/audio-to-robot-voice', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({audio_base64: sbEditAudioClean, format: sbEditFmtClean||'wav'})});
        const d = await r.json();
        if (d.ok) {
          sbEditAudio = null; sbEditFmt = '';
          sbEditAudioClean = d.audio_base64;
          sbEditFmtClean = 'mp3';
          sbEditAudioCleared = false;
          updateSbModalStatus();
        } else alert(d.error || 'Errore');
      } catch(e) { alert('Errore: '+e.message); }
      btn.disabled = false;
    };
    var sbOutRef = document.getElementById('sbOutputRefresh');
    if (sbOutRef) sbOutRef.onclick = () => { if (typeof requestAndLoadDevices === 'function') requestAndLoadDevices(); };
    function escAttr(s){ return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/\u003c/g,'&lt;'); }
    var btnTestEl = document.getElementById('btnTest');
    if (btnTestEl) btnTestEl.onclick = async function(){
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

    var btnTextEl = document.getElementById('btnText');
    if (btnTextEl) btnTextEl.onclick = async function(){
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
    var _textInput = document.getElementById('textInput');
    if (_textInput) _textInput.onkeydown = function(e) { if (e.key === 'Enter') { var b=document.getElementById('btnText'); if(b)b.click(); } };

    var btnSampleEl = document.getElementById('btnSample');
    if (btnSampleEl) btnSampleEl.onclick = async function(){
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
          if (btn) btn.classList.remove('recording');
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
            setTimeout(function(){ if (typeof startParlaMicPreviewIfEligible === 'function') startParlaMicPreviewIfEligible(); }, 350);
          }, 80);
        };
        mediaRecorder.onerror = (e) => {
          clearAllIntervals();
          isRecording = false;
          if (btn) btn.classList.remove('recording');
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
        if (btn) btn.classList.add('recording');
        document.getElementById('recDebug').textContent = 'Registrazione: 0.0 sec';
        recTimeout = setTimeout(() => { stopRec(); }, MAX_REC_SEC * 1000);
      } catch(err) {
        isRecording = false;
        pendingStop = false;
        var micVal = document.getElementById('mic') ? document.getElementById('mic').value : '';
        var hint = (micVal && micVal.indexOf('local_') === 0)
          ? 'Hai selezionato microfono Jetson: per parlare da questo PC/telefono scegli un microfono <strong>Browser</strong> nel menu sopra.'
          : 'Abilita il microfono per questo sito nelle impostazioni browser, poi scegli il device <strong>Browser</strong> nel menu sopra.';
        document.getElementById('result').innerHTML = '<div class="warn">Microfono non disponibile. '+hint+'</div>';
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
        if (btn) btn.classList.remove('recording');
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
      if (btn) btn.classList.remove('recording');
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
        if (btn) btn.disabled = false;
        return;
      }
      const sizeKb = (blob.size/1024).toFixed(1);
      document.getElementById('recDebug').textContent = 'Invio '+sizeKb+' KB...';
      document.getElementById('recDebug').style.color = '#3b82f6';
      document.getElementById('result').innerHTML = '<div style="color:#3b82f6;">Elaborazione…</div>';
      if (btn) btn.disabled = true;
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

    /* ---- Mic level (solo tab Parla: usa levelBar / levelLabel) ---- */
    (function(){
      var _pmWs = null;
      var _pmTimer = null;
      var _pmSource = null;

      function pmParlaActive() {
        var sec = document.getElementById('section-parla');
        return !!(sec && sec.classList.contains('active'));
      }

      function pmBars() {
        return {
          bar: document.getElementById('levelBar'),
          lbl: document.getElementById('levelLabel'),
        };
      }

      function pmUpdateFromBrowser() {
        var ui = pmBars();
        if (!ui.bar) return;
        var an = analyserNode || parlaPreviewAnalyser || wakeAnalyser || null;
        if (!an) {
          ui.bar.style.width = '0%';
          if (ui.lbl) ui.lbl.textContent = 'Livello: --';
          return;
        }
        var buf = new Uint8Array(an.frequencyBinCount);
        an.getByteFrequencyData(buf);
        var peak = 0;
        for (var i = 0; i < buf.length; i++) if (buf[i] > peak) peak = buf[i];
        var gain = typeof getParlaMonitorGain === 'function' ? getParlaMonitorGain() : 1;
        var pct = Math.min(100, peak * gain * (100 / 255));
        ui.bar.style.width = pct.toFixed(1) + '%';
        ui.bar.style.background = peak > 128 ? '#ef4444' : peak > 50 ? '#22c55e' : peak > 13 ? '#eab308' : '#52525b';
        if (ui.lbl) ui.lbl.textContent = peak > 5 ? ('Livello: ' + (pct|0) + '%') : 'Livello: --';
      }

      function pmStartServerWs() {
        if (_pmWs && _pmWs.readyState <= 1) return;
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        _pmWs = new WebSocket(proto + '//' + location.host + '/ws/mic-level');
        _pmWs.onmessage = function(ev) {
          try {
            var d = JSON.parse(ev.data);
            if (d.type === 'level') {
              var ui = pmBars();
              var pct = Math.max(0, Math.min(100, ((d.db + 60) / 60) * 100));
              var gain = typeof getParlaMonitorGain === 'function' ? getParlaMonitorGain() : 1;
              var peak255 = Math.min(255, Math.round((Number(d.peak) || 0) * 255 * gain));
              if (typeof window.g1UpdateTalkMicLevel === 'function') {
                window.g1UpdateTalkMicLevel(peak255);
              }
              if (ui.bar) {
                ui.bar.style.width = pct.toFixed(1) + '%';
                ui.bar.style.background = d.peak > 0.5 ? '#ef4444' : d.rms > 0.02 ? '#22c55e' : d.rms > 0.005 ? '#eab308' : '#52525b';
              }
              if (ui.lbl) ui.lbl.textContent = d.rms > 0.01 ? ('Livello: ' + (pct|0) + '%') : 'Livello: --';
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
        if (!pmParlaActive()) {
          if (_pmSource === 'server') pmStopServerWs();
          var ui = pmBars();
          if (ui.bar) ui.bar.style.width = '0%';
          if (ui.lbl) ui.lbl.textContent = 'Livello: --';
          return;
        }
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
    (function(){
      var _camStreaming = false, _camPoll = null, _camSessionActive = false;
      function _camEl(id){ return document.getElementById(id); }
      function _camVisionActive(){
        var cb = _camEl('clientCamVisionEnable');
        return !!(cb && cb.checked);
      }
      function _camStreamUrl(){ return location.origin + '/api/camera/stream?_=' + Date.now(); }
      function _camResetIdleUI(){
        _camSetStatus(_camEl('clientCamStatus'), 'Visione disattivata', null);
        _camSetStatus(_camEl('clientCamYolo'), '--', null);
        var fps = _camEl('clientCamFps'); if (fps) fps.textContent = '--';
        var be = _camEl('clientCamBackend'); if (be) be.textContent = '--';
        var detEl = _camEl('clientCamDets');
        if (detEl) detEl.textContent = '—';
        var pickEl = _camEl('clientPickStatus');
        if (pickEl) pickEl.textContent = 'Auto-pick: — (attiva visione)';
      }
      function _camUpdateControls(enabled){
        ['clientCamBtnStart','clientCamBtnStop','clientCamBtnRefresh','clientPickOnBtn','clientPickOffBtn'].forEach(function(id){
          var el = _camEl(id);
          if (el) el.disabled = !enabled;
        });
      }
      function _camShow(on){
        var img = _camEl('clientCamStream'), ph = _camEl('clientCamPlaceholder');
        if (!img) return;
        if (on) {
          if (!_camVisionActive()) return;
          img.onerror = function(){
            _camSetStatus(_camEl('clientCamStatus'), 'Stream non disponibile (camera Jetson?)', false);
            if (ph) { ph.style.display = 'flex'; ph.textContent = 'Stream non disponibile — controlla RealSense / G1_CAMERA_DEVICE'; }
            img.style.display = 'none';
            _camStreaming = false;
          };
          img.onload = function(){ if (ph) ph.style.display = 'none'; };
          img.style.display = 'block';
          img.src = _camStreamUrl();
          if (ph) ph.style.display = 'none';
          _camStreaming = true;
        } else {
          img.onerror = null;
          img.onload = null;
          img.style.display = 'none';
          img.removeAttribute('src');
          if (ph) { ph.style.display = 'flex'; ph.textContent = 'Visione disattivata — spunta la casella sopra per collegare la camera'; }
          _camStreaming = false;
        }
      }
      function _camSetStatus(el, text, ok){
        if (!el) return;
        el.textContent = text;
        el.className = 'val' + (ok === true ? ' ok' : ok === false ? ' err' : '');
      }
      async function _camPollStatus(){
        if (!_camVisionActive() || !_camSessionActive) return;
        try {
          var r = await fetch(location.origin + '/api/camera/status', { credentials: 'same-origin' });
          var s = await r.json();
          _camSetStatus(_camEl('clientCamStatus'), s.open_error ? ('Errore: ' + String(s.open_error).slice(0, 48)) : (s.running && s.has_frame ? (s.backend || 'ok') + ' ' + (s.resolution || '') : (s.running ? 'Avvio…' : 'Ferma')), !s.open_error && s.has_frame);
          if (s.yolo_enabled) {
            _camSetStatus(_camEl('clientCamYolo'), s.yolo_loaded ? (s.yolo_model || 'ok') : (s.yolo_error ? String(s.yolo_error).slice(0, 32) : 'Caricamento…'), s.yolo_loaded);
          } else {
            _camSetStatus(_camEl('clientCamYolo'), 'Disabilitato', null);
          }
          _camEl('clientCamFps').textContent = s.fps ? String(s.fps) : '--';
          _camEl('clientCamBackend').textContent = s.backend || (s.source || '--');
          var dets = s.detections || [];
          var detEl = _camEl('clientCamDets');
          if (detEl) {
            detEl.textContent = dets.length ? dets.map(function(d){
              var t = d.class + ' ' + Math.round((d.confidence || 0) * 100) + '%';
              if (d.depth_m != null) t += ' · ' + d.depth_m + 'm';
              return t;
            }).join(' · ') : 'Nessun oggetto rilevato';
          }
          _camPollPick();
        } catch (e) {
          _camSetStatus(_camEl('clientCamStatus'), 'API non raggiungibile', false);
        }
      }
      function _camStartPoll(){
        if (_camPoll || !_camVisionActive() || !_camSessionActive) return;
        _camPollStatus();
        _camPoll = setInterval(_camPollStatus, 2000);
      }
      function _camStopPoll(){
        if (_camPoll) { clearInterval(_camPoll); _camPoll = null; }
      }
      function _camVisionCheckbox(on){
        var cb = _camEl('clientCamVisionEnable');
        if (cb) cb.checked = !!on;
      }
      async function _camTeardownSession(){
        var hadSession = _camSessionActive;
        _camSessionActive = false;
        _camStopPoll();
        _camShow(false);
        if (hadSession) {
          try { await fetch(location.origin + '/api/camera/stop', { method: 'POST', credentials: 'same-origin' }); } catch (_) {}
          try {
            await fetch(location.origin + '/api/pick/enable', {
              method: 'POST',
              credentials: 'same-origin',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ enabled: false }),
            });
          } catch (_) {}
        }
        _camResetIdleUI();
      }
      async function _camOnVisionToggle(){
        if (_camVisionActive()) {
          _camUpdateControls(true);
          await window.g1ClientCameraStart();
        } else {
          _camUpdateControls(false);
          await _camTeardownSession();
        }
      }
      async function _camPollPick(){
        if (!_camVisionActive() || !_camSessionActive) return;
        try {
          var r = await fetch(location.origin + '/api/pick/status', { credentials: 'same-origin' });
          var p = await r.json();
          var el = _camEl('clientPickStatus');
          if (!el) return;
          el.textContent = 'Auto-pick: ' + (p.enabled ? 'ON' : 'OFF')
            + ' · ' + ((p.target_classes || []).join(', ') || '?')
            + ' · slot ' + (p.teaching_slot != null ? p.teaching_slot : '?')
            + ' · trigger ' + (p.trigger_count || 0);
        } catch (_) {}
      }
      window.g1ClientPickSet = async function(on){
        if (!_camVisionActive()) {
          if (!on) return;
          _camVisionCheckbox(true);
          _camUpdateControls(true);
          await window.g1ClientCameraStart();
        }
        if (!_camVisionActive() || !_camSessionActive) {
          var el0 = _camEl('clientPickStatus');
          if (el0) el0.textContent = 'Auto-pick: attiva prima la visione';
          return;
        }
        try {
          await fetch(location.origin + '/api/pick/enable', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !!on }),
          });
          _camPollPick();
        } catch (e) {
          var el = _camEl('clientPickStatus');
          if (el) el.textContent = 'Auto-pick errore: ' + (e.message || e);
        }
      };
      window.g1ClientCameraStart = async function(){
        if (!_camVisionActive()) return;
        _camUpdateControls(true);
        try {
          var r = await fetch(location.origin + '/api/camera/start', { method: 'POST', credentials: 'same-origin' });
          var d = await r.json().catch(function(){ return {}; });
          if (!r.ok) {
            var msg = (d && (d.message || d.detail || d.open_error)) ? String(d.message || d.detail || d.open_error) : ('HTTP '+r.status);
            _camSetStatus(_camEl('clientCamStatus'), msg.slice(0, 80), false);
            return;
          }
          _camSessionActive = true;
          _camShow(true);
          _camStartPoll();
        } catch (e) {
          _camSetStatus(_camEl('clientCamStatus'), String(e.message || e), false);
        }
      };
      window.g1ClientCameraStop = async function(){
        _camVisionCheckbox(false);
        _camUpdateControls(false);
        await _camTeardownSession();
      };
      window.g1ClientCameraRefresh = function(){
        if (!_camVisionActive() || !_camSessionActive) return;
        if (_camStreaming) {
          var img = _camEl('clientCamStream');
          if (img) img.src = _camStreamUrl();
        }
        _camPollStatus();
      };
      window.g1ClientCameraOnShow = function(){
        if (_camVisionActive() && !_camSessionActive) {
          _camUpdateControls(true);
          window.g1ClientCameraStart();
        } else {
          _camUpdateControls(_camVisionActive());
          if (!_camVisionActive()) {
            _camShow(false);
            _camResetIdleUI();
          }
        }
      };
      window.g1ClientCameraOnHide = async function(){
        _camVisionCheckbox(false);
        _camUpdateControls(false);
        await _camTeardownSession();
      };
      _camUpdateControls(false);
      _camResetIdleUI();
      var _camBtnStart = _camEl('clientCamBtnStart');
      var _camBtnStop = _camEl('clientCamBtnStop');
      var _camBtnRefresh = _camEl('clientCamBtnRefresh');
      var _camVisionCb = _camEl('clientCamVisionEnable');
      if (_camVisionCb) _camVisionCb.onchange = function(){ _camOnVisionToggle(); };
      if (_camBtnStart) _camBtnStart.onclick = function(){ if (!_camVisionActive()) _camVisionCheckbox(true); window.g1ClientCameraStart(); };
      if (_camBtnStop) _camBtnStop.onclick = function(){ window.g1ClientCameraStop(); };
      if (_camBtnRefresh) _camBtnRefresh.onclick = function(){ window.g1ClientCameraRefresh(); };
    })();
    (function(){
      var _logTimer = null;
      function _logBox(){ return document.getElementById('clientLogBox'); }
      window.g1ClientLogRefresh = async function(){
        var box = _logBox();
        if (!box) return;
        try {
          var r = await fetch(location.origin + '/api/server-log?lines=120', { credentials: 'same-origin' });
          var d = await r.json();
          var text = (d.lines || []).join('\\n') || '(vuoto)';
          box.textContent = text;
          box.scrollTop = box.scrollHeight;
        } catch (e) {
          box.textContent = 'Errore log: ' + (e.message || e);
        }
      };
      window.g1ClientLogOnShow = function(){
        window.g1ClientLogRefresh();
        if (_logTimer) return;
        _logTimer = setInterval(function(){
          var auto = document.getElementById('clientLogAuto');
          if (!auto || auto.checked) window.g1ClientLogRefresh();
        }, 3000);
      };
      window.g1ClientLogOnHide = function(){
        if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
      };
    })();
    try { connect(); } catch (e) { console.error(e); }
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
  <p style="color:#71717a;">Di &laquo;Hey G1&raquo;, aspetta il chime, poi parla. Non toccare nulla.</p>
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
.top-bar .status.warn{color:#fbbf24;}
.sport-banner{margin:8px 12px 0;padding:10px 12px;border-radius:10px;font-size:12px;line-height:1.45;border:1px solid #3f3f46;background:#18181b;}
.sport-banner .sport-title{font-weight:700;font-size:13px;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between;gap:8px;}
.sport-banner .sport-detail{color:#a1a1aa;}
.sport-banner.sport_ok{border-color:rgba(34,197,94,0.45);background:rgba(34,197,94,0.08);}
.sport-banner.sport_ok .sport-title{color:#86efac;}
.sport-banner.dds_ok{border-color:rgba(251,191,36,0.45);background:rgba(251,191,36,0.08);}
.sport-banner.dds_ok .sport-title{color:#fcd34d;}
.sport-banner.ready{border-color:rgba(56,189,248,0.4);background:rgba(56,189,248,0.08);}
.sport-banner.ready .sport-title{color:#7dd3fc;}
.sport-banner.ai_blocked,.sport-banner.offline,.sport-banner.error{border-color:rgba(239,68,68,0.45);background:rgba(239,68,68,0.08);}
.sport-banner.ai_blocked .sport-title,.sport-banner.offline .sport-title,.sport-banner.error .sport-title{color:#fca5a5;}
.sport-banner button{font-size:11px;padding:4px 8px;border-radius:6px;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;cursor:pointer;}
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
.motion-preview{display:grid;grid-template-columns:minmax(160px,1.2fr) minmax(140px,0.8fr);gap:12px;align-items:center;margin-top:12px;padding:12px;background:#0a0b10;border:1px solid #27272a;border-radius:12px;}
.motion-stage{position:relative;min-height:160px;border-radius:14px;overflow:hidden;border:1px solid #30313a;background:
  linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
  linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px),
  radial-gradient(circle at 50% 50%, rgba(20,184,166,0.18), rgba(10,11,16,0.94) 60%);
background-size:24px 24px,24px 24px,100% 100%;}
.motion-stage::before,.motion-stage::after{content:'';position:absolute;background:rgba(148,163,184,0.16);}
.motion-stage::before{left:50%;top:12px;bottom:12px;width:1px;transform:translateX(-0.5px);}
.motion-stage::after{top:50%;left:12px;right:12px;height:1px;transform:translateY(-0.5px);}
.motion-body{position:absolute;left:50%;top:50%;width:54px;height:74px;border-radius:20px 20px 28px 28px;background:linear-gradient(180deg,#2dd4bf 0%,#0f766e 100%);box-shadow:0 0 0 6px rgba(20,184,166,0.12),0 0 20px rgba(20,184,166,0.18);transform:translate(-50%,-50%);transition:transform 0.08s ease,box-shadow 0.15s ease,background 0.15s ease;}
.motion-body::before{content:'';position:absolute;left:50%;top:8px;width:20px;height:10px;border-radius:999px;background:rgba(255,255,255,0.35);transform:translateX(-50%);}
.motion-body::after{content:'';position:absolute;left:50%;top:10px;width:12px;height:28px;border-radius:999px;background:rgba(255,255,255,0.16);transform:translateX(-50%);}
.motion-heading{position:absolute;left:50%;top:50%;width:0;height:0;transform:translate(-50%,-50%);border-left:12px solid transparent;border-right:12px solid transparent;border-bottom:34px solid rgba(45,212,191,0.95);filter:drop-shadow(0 0 10px rgba(45,212,191,0.35));transform-origin:50% 78%;transition:transform 0.08s ease;}
.motion-orbit{position:absolute;left:50%;top:50%;width:110px;height:110px;border:1px dashed rgba(167,139,250,0.28);border-radius:50%;transform:translate(-50%,-50%);}
.motion-orbit-dot{position:absolute;left:50%;top:50%;width:12px;height:12px;border-radius:50%;background:#a78bfa;box-shadow:0 0 12px rgba(167,139,250,0.55);transform:translate(-50%,-50%) rotate(0deg) translateY(-50px);transition:transform 0.08s ease;}
.motion-meta{display:flex;flex-direction:column;gap:8px;font-size:11px;color:#a1a1aa;font-family:monospace;}
.motion-command{font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:#71717a;}
.motion-command strong{display:block;margin-top:2px;font-size:14px;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;text-transform:none;letter-spacing:0;}
.motion-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 8px;border-radius:8px;background:#111318;border:1px solid #27272a;}
.motion-row span:last-child{color:#e4e4e7;}
.motion-row.speed span:last-child{color:#5eead4;}
@media (max-width: 780px){.motion-preview{grid-template-columns:1fr;}.motion-stage{min-height:150px;}}
#robotIp{width:140px;padding:6px 8px;background:#1e1e2e;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:12px;font-family:monospace;}
.log-area{max-height:60px;overflow-y:auto;padding:4px 8px;font-size:10px;color:#52525b;font-family:monospace;line-height:1.4;}
.log-area .ok{color:#22c55e;}.log-area .err{color:#ef4444;}
.teach-section{margin-top:12px;padding:12px;background:#111318;border:1px solid #27272a;border-radius:12px;}
.teach-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.teach-header h2{font-size:13px;color:#a78bfa;font-weight:700;text-transform:uppercase;letter-spacing:1px;}
.teach-status{font-size:11px;padding:3px 8px;border-radius:6px;background:#27272a;color:#a1a1aa;}
.teach-status.recording{background:#ef444430;color:#ef4444;animation:pulse-rec 1s infinite;}
.teach-status.replaying{background:#14b8a630;color:#14b8a6;}
@keyframes pulse-rec{0%,100%{opacity:1;}50%{opacity:0.5;}}
.teach-btns{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;}
.teach-btn{padding:10px 16px;border-radius:8px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;-webkit-tap-highlight-color:transparent;transition:background 0.15s;}
.teach-btn:active{transform:scale(0.96);}
.teach-btn.rec{background:rgba(239,68,68,0.15);border-color:rgba(239,68,68,0.4);color:#fca5a5;}
.teach-btn.rec:active{background:rgba(239,68,68,0.3);}
.teach-btn.play{background:rgba(20,184,166,0.15);border-color:rgba(20,184,166,0.4);color:#5eead4;}
.teach-btn.play:active{background:rgba(20,184,166,0.3);}
.teach-btn.save{background:rgba(99,102,241,0.15);border-color:rgba(99,102,241,0.4);color:#a5b4fc;}
.teach-btn.save:active{background:rgba(99,102,241,0.3);}
.teach-btn.stop{background:rgba(245,158,11,0.15);border-color:rgba(245,158,11,0.4);color:#fcd34d;}
.teach-btn.stop:active{background:rgba(245,158,11,0.3);}
.teach-btn:disabled{opacity:0.35;pointer-events:none;}
.joints-vis{display:flex;flex-direction:column;gap:4px;margin-top:8px;}
.joint-group-label{font-size:9px;color:#71717a;text-transform:uppercase;letter-spacing:0.5px;margin-top:4px;}
.joint-row{display:flex;align-items:center;gap:4px;}
.joint-label{font-size:9px;color:#52525b;width:20px;text-align:right;font-family:monospace;}
.joint-bar-wrap{flex:1;height:8px;background:#1e1e2e;border-radius:4px;overflow:hidden;position:relative;}
.joint-bar{height:100%;background:#14b8a6;border-radius:4px;transition:width 0.1s;width:50%;}
.joint-bar.waist{background:#a78bfa;}
.joint-val{font-size:9px;color:#52525b;width:36px;font-family:monospace;}
.teach-timer{font-size:24px;font-weight:700;color:#ef4444;font-family:monospace;text-align:center;margin:6px 0;}
.teach-slot-row{display:flex;align-items:center;gap:6px;margin-top:8px;}
.teach-slot-row label{font-size:11px;color:#71717a;}
.teach-slot-row input{width:60px;padding:4px 6px;background:#1e1e2e;border:1px solid #3f3f46;border-radius:6px;color:#e4e4e7;font-size:12px;font-family:monospace;text-align:center;}
.slot-card{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:#0a0b10;border:1px solid #27272a;border-radius:8px;font-size:11px;}
.slot-card .slot-info{display:flex;align-items:center;gap:8px;color:#a1a1aa;}
.slot-card .slot-id{font-weight:700;color:#a78bfa;min-width:36px;}
.slot-card .slot-dur{color:#71717a;font-family:monospace;font-size:10px;}
.slot-card .slot-actions{display:flex;gap:4px;}
.slot-card .slot-actions button{padding:4px 10px;border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;-webkit-tap-highlight-color:transparent;}
.slot-card .slot-actions .sl-play{background:rgba(20,184,166,0.15);border-color:rgba(20,184,166,0.4);color:#5eead4;}
.slot-card .slot-actions .sl-del{background:rgba(239,68,68,0.1);border-color:rgba(239,68,68,0.3);color:#fca5a5;}
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
<div id="sportBanner" class="sport-banner dds_ok" style="display:none;">
  <div class="sport-title"><span id="sportLabel">Sport mode —</span><button type="button" onclick="refreshSportStatus()">Aggiorna</button></div>
  <div class="sport-detail" id="sportDetail">Caricamento…</div>
</div>
<div class="container">
  <div class="actions-area">
    <div class="section-label">Gesti braccia</div>
    <p class="hint" style="margin:0 0 8px;font-size:11px;color:#9ca3af;">Prima <strong>L1+A sul telecomando</strong> (sport mode). Poi <strong>Ready</strong> e un gesto braccio. «Modalità gesti» dal PC è opzionale (FSM spesso non leggibile).</p>
    <div class="gesture-grid" id="gestureGrid"></div>
    <div class="section-label">Locomozione (sport mode)</div>
    <div class="quick-btns">
      <button type="button" class="quick-btn loco" onclick="sendLoco('ready')">Ready</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('arm_ready')" title="FSM 500 — necessario per gesti braccia">Modalità gesti</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('walk')">Walk</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('stop_walk')">Stop walk</button>
      <button type="button" class="quick-btn loco" onclick="sendLoco('low_stand')">Low stand</button>
    </div>
    <div class="section-label">Braccia / sicurezza</div>
    <div class="quick-btns">
      <button type="button" class="quick-btn" onclick="sendAction(99)">Rilascia braccia</button>
      <button type="button" class="quick-btn" onclick="sendMove(0,0,0)">STOP vel.</button>
    </div>
    <div class="section-label">Vista movimento</div>
    <div class="motion-preview" aria-label="Anteprima movimento G1">
      <div class="motion-stage">
        <div class="motion-orbit" id="motionOrbit"><div class="motion-orbit-dot" id="motionOrbitDot"></div></div>
        <div class="motion-body" id="motionBody"></div>
        <div class="motion-heading" id="motionHeading"></div>
      </div>
      <div class="motion-meta">
        <div class="motion-command">Comando
          <strong id="motionCommand">idle</strong>
        </div>
        <div class="motion-row"><span>vx</span><span id="motionVx">0.00</span></div>
        <div class="motion-row"><span>vy</span><span id="motionVy">0.00</span></div>
        <div class="motion-row"><span>vyaw</span><span id="motionVyaw">0.00</span></div>
        <div class="motion-row speed"><span>Velocità</span><span id="motionSpeed">0%</span></div>
      </div>
    </div>
    <div class="log-area" id="logArea"></div>
  </div>

  <div class="teach-section">
    <div class="teach-header">
      <h2>Teaching (braccia)</h2>
      <span id="teachStatus" class="teach-status">idle</span>
    </div>
    <div id="teachTimer" class="teach-timer" style="display:none;">00:00</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">
      <button type="button" class="teach-btn rec" id="btnRec" onclick="teachAction('start_record')">REC</button>
      <button type="button" class="teach-btn stop" id="btnStop" onclick="teachAction('stop_record')" disabled>STOP</button>
      <button type="button" class="teach-btn play" id="btnPlay" onclick="teachAction('replay_temp')" disabled>PLAY temp</button>
      <button type="button" class="teach-btn stop" id="btnEmStop" onclick="teachAction('emergency_stop')">E-STOP</button>
    </div>
    <div class="teach-slot-row" style="margin-bottom:8px;">
      <label>Salva in slot:</label>
      <input id="teachSlotId" type="number" min="0" max="99" value="0" />
      <input id="teachSlotName" type="text" placeholder="nome (es. spiegazione)" style="max-width:140px;" />
      <button type="button" class="teach-btn save" id="btnSave" onclick="teachSaveSlot()" disabled>SAVE</button>
    </div>
    <div id="slotListWrap" style="margin-top:4px;">
      <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.8px;color:#71717a;font-weight:600;margin-bottom:6px;">Slot salvati</div>
      <div id="slotList" style="display:flex;flex-direction:column;gap:4px;max-height:180px;overflow-y:auto;"></div>
      <div id="slotListEmpty" style="font-size:11px;color:#52525b;padding:8px 0;">Nessuno slot salvato</div>
    </div>
    <div class="joints-vis" id="jointsVis" style="margin-top:8px;">
      <div class="joint-group-label">Vita</div>
      <div id="jgWaist"></div>
      <div class="joint-group-label">Braccio SX</div>
      <div id="jgLeft"></div>
      <div class="joint-group-label">Braccio DX</div>
      <div id="jgRight"></div>
    </div>
  </div>

  <div class="teach-section" style="margin-top:10px;border-top:1px solid #27272a;padding-top:10px;">
    <div class="teach-header">
      <h2 style="font-size:14px;margin:0;">Auto-pick (visione)</h2>
      <span id="pickBadge" class="teach-status">off</span>
    </div>
    <p style="margin:0 0 8px;font-size:11px;color:#71717a;line-height:1.45;">Registra un <strong>teaching</strong> verso il punto presa. Con YOLO+depth attivi, se vede la classe (es. bottle) parte il <strong>replay dello slot</strong> (con correzione se hai calibrato). Opzionale: <code>G1_PICK_MODE=safe_reach</code> per la manovra a fasi sui joint.</p>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;">
      <button type="button" class="teach-btn play" onclick="pickSetEnabled(true)">Auto-pick ON</button>
      <button type="button" class="teach-btn stop" onclick="pickSetEnabled(false)">OFF</button>
      <button type="button" class="teach-btn" onclick="pickManeuverTest()">Test manovra</button>
      <button type="button" class="teach-btn" onclick="pickTriggerTest()">Test replay slot</button>
      <button type="button" class="teach-btn save" onclick="pickCalibrate()">Calibra bottiglia qui</button>
    </div>
    <p style="margin:0 0 6px;font-size:10px;color:#52525b;">Calibra con la bottiglia in vista (camera ON). Poi spostala: le ultime fasi correggono sinistra/destra e distanza. Regola le fasi in <code>config/pick_maneuver.json</code> sul Jetson.</p>
    <div id="pickStatusLine" style="font-size:11px;color:#a1a1aa;line-height:1.5;">—</div>
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
    el.textContent = s === 'ok' ? 'OK' : s === 'err' ? 'ERR' : s === 'warn' ? 'DDS?' : '--';
  }

  function refreshSportStatus(){
    fetch('/api/robot-fsm').then(function(r){ return r.json(); }).then(function(d){
      var banner = document.getElementById('sportBanner');
      var label = document.getElementById('sportLabel');
      var detail = document.getElementById('sportDetail');
      if (!banner || !label || !detail) return;
      banner.style.display = 'block';
      if (!d.ok) {
        banner.className = 'sport-banner error';
        label.textContent = 'Diagnostica fallita';
        detail.textContent = d.message || 'Errore';
        setStatus('err');
        return;
      }
      var st = d.sport_status || 'unknown';
      banner.className = 'sport-banner ' + st;
      label.textContent = d.sport_label || st;
      var lines = [d.detail || ''];
      if (d.fsm_id != null) lines.push('FSM letto: ' + d.fsm_id + (d.fsm_mode != null ? ' mode=' + d.fsm_mode : ''));
      else lines.push('FSM: non leggibile dal Jetson (normale)');
      if (d.dds_ok) lines.push('DDS/loco: connesso (rc=0)');
      else if (d.loco_vel_rc != null) lines.push('DDS/loco: rc=' + d.loco_vel_rc);
      detail.textContent = lines.filter(Boolean).join(' · ');
      if (st === 'sport_ok') setStatus('ok');
      else if (st === 'dds_ok' || st === 'ready') setStatus('warn');
      else setStatus('err');
    }).catch(function(e){
      setStatus('err');
    });
  }
  refreshSportStatus();
  setInterval(refreshSportStatus, 4000);

  function renderMotionPreview(vx, vy, vyaw, command) {
    var body = document.getElementById('motionBody');
    var heading = document.getElementById('motionHeading');
    var orbitDot = document.getElementById('motionOrbitDot');
    var cmd = document.getElementById('motionCommand');
    var vxEl = document.getElementById('motionVx');
    var vyEl = document.getElementById('motionVy');
    var vyawEl = document.getElementById('motionVyaw');
    var speedEl = document.getElementById('motionSpeed');
    if (!body || !heading || !orbitDot || !cmd || !vxEl || !vyEl || !vyawEl || !speedEl) return;

    var maxLin = Math.max(parseFloat(speedSlider.value) || 0.5, 0.1);
    var maxYaw = Math.max(parseFloat(yawSlider.value) || 0.8, 0.1);
    var linX = Math.max(-1, Math.min(1, (Number(vy) || 0) / maxLin));
    var linY = Math.max(-1, Math.min(1, (Number(vx) || 0) / maxLin));
    var yawN = Math.max(-1, Math.min(1, (Number(vyaw) || 0) / maxYaw));
    var offsetX = Math.round(linX * 36);
    var offsetY = Math.round(-linY * 36);
    var headingDeg = Math.atan2(Number(vy) || 0, Number(vx) || 0) * 180 / Math.PI;
    var orbitDeg = Math.round(yawN * 160);
    var speedPct = Math.round(Math.min(1, Math.hypot(Number(vx) || 0, Number(vy) || 0) / maxLin) * 100);

    body.style.transform = 'translate(calc(-50% + ' + offsetX + 'px), calc(-50% + ' + offsetY + 'px))';
    heading.style.transform = 'translate(-50%, -50%) rotate(' + headingDeg + 'deg)';
    orbitDot.style.transform = 'translate(-50%, -50%) rotate(' + orbitDeg + 'deg) translateY(-50px)';

    cmd.textContent = command || 'idle';
    vxEl.textContent = (Number(vx) || 0).toFixed(2);
    vyEl.textContent = (Number(vy) || 0).toFixed(2);
    vyawEl.textContent = (Number(vyaw) || 0).toFixed(2);
    speedEl.textContent = speedPct + '%';
    body.style.boxShadow = speedPct > 0 ? '0 0 0 6px rgba(20,184,166,0.12),0 0 24px rgba(20,184,166,0.24)' : '0 0 0 6px rgba(20,184,166,0.12),0 0 20px rgba(20,184,166,0.18)';
  }

  function motionIsForceStop(command) {
    return /^(stop|stop_walk|ready|low_stand|damp|zero_torque|squat_up_damp|lie_standup)$/i.test(String(command || '').trim());
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

  /* ── Teaching section ── */
  var teachState = 'idle';
  var teachEvt = null;
  var timerEl = document.getElementById('teachTimer');
  var timerStart = 0, timerIv = null;

  var JOINT_NAMES_W = ['Y','R','P'];
  var JOINT_NAMES_A = ['SP','SR','SY','E','WR','WP','WY'];
  buildJointBars('jgWaist', JOINT_NAMES_W, 'waist');
  buildJointBars('jgLeft', JOINT_NAMES_A, '');
  buildJointBars('jgRight', JOINT_NAMES_A, '');

  function buildJointBars(containerId, names, cls) {
    var c = document.getElementById(containerId);
    names.forEach(function(n, i) {
      var row = document.createElement('div');
      row.className = 'joint-row';
      row.innerHTML = '<span class="joint-label">' + n + '</span>' +
        '<div class="joint-bar-wrap"><div class="joint-bar ' + cls + '" id="' + containerId + '_b' + i + '"></div></div>' +
        '<span class="joint-val" id="' + containerId + '_v' + i + '">0.00</span>';
      c.appendChild(row);
    });
  }

  function updateBars(groupId, vals) {
    if (!vals) return;
    vals.forEach(function(v, i) {
      var bar = document.getElementById(groupId + '_b' + i);
      var lbl = document.getElementById(groupId + '_v' + i);
      if (!bar) return;
      var pct = Math.min(100, Math.max(0, 50 + (v / Math.PI) * 50));
      bar.style.width = pct + '%';
      if (lbl) lbl.textContent = v.toFixed(2);
    });
  }

  function connectSSE() {
    if (teachEvt) { try { teachEvt.close(); } catch(e){} }
    teachEvt = new EventSource('/api/teaching/stream');
    teachEvt.onmessage = function(e) {
      try {
        var d = JSON.parse(e.data);
        updateTeachUI(d);
        if (d.joints) {
          updateBars('jgWaist', d.joints.waist);
          updateBars('jgLeft', d.joints.left_arm);
          updateBars('jgRight', d.joints.right_arm);
        }
      } catch(err) {}
    };
    teachEvt.onerror = function() {
      setTimeout(connectSSE, 3000);
    };
  }
  connectSSE();

  function updateTeachUI(d) {
    var st = d.state || 'idle';
    var badge = document.getElementById('teachStatus');
    badge.textContent = st;
    badge.className = 'teach-status ' + st;

    document.getElementById('btnRec').disabled = (st !== 'idle');
    document.getElementById('btnStop').disabled = (st !== 'recording');
    document.getElementById('btnPlay').disabled = (st !== 'idle');
    document.getElementById('btnSave').disabled = (st !== 'idle');

    if (st === 'recording') {
      timerEl.style.display = '';
      if (!timerIv) {
        timerStart = Date.now();
        timerIv = setInterval(function() {
          var s = Math.floor((Date.now() - timerStart) / 1000);
          var m = Math.floor(s / 60);
          timerEl.textContent = String(m).padStart(2,'0') + ':' + String(s % 60).padStart(2,'0');
        }, 500);
      }
    } else {
      timerEl.style.display = 'none';
      if (timerIv) { clearInterval(timerIv); timerIv = null; }
    }
  }

  window.teachAction = function(action) {
    log('Teaching: ' + action);
    fetch('/api/teaching/' + action, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) log('Teaching OK: ' + action, 'ok');
        else log('Teaching ERR: ' + (d.error || JSON.stringify(d)), 'err');
      })
      .catch(function(e){ log('Teaching rete: ' + e, 'err'); });
  };

  window.teachSaveSlot = function() {
    var slotId = parseInt(document.getElementById('teachSlotId').value) || 0;
    var slotName = (document.getElementById('teachSlotName') && document.getElementById('teachSlotName').value || '').trim();
    log('Teaching: save to slot ' + slotId + (slotName ? ' (' + slotName + ')' : ''));
    fetch('/api/teaching/save_to_slot/' + slotId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: slotName })
    })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) { log('Saved to slot ' + slotId, 'ok'); loadSlotList(); }
        else log('Save ERR: ' + (d.error || ''), 'err');
      })
      .catch(function(e){ log('Save rete: ' + e, 'err'); });
  };

  window.teachPlaySlot = function(slotId) {
    if (slotId == null) slotId = parseInt(document.getElementById('teachSlotId').value) || 0;
    log('Teaching: replay slot ' + slotId);
    fetch('/api/teaching/replay_slot/' + slotId, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) log('Replay slot ' + slotId + ' OK', 'ok');
        else log('Replay ERR: ' + (d.error || ''), 'err');
      })
      .catch(function(e){ log('Replay rete: ' + e, 'err'); });
  };

  window.teachDeleteSlot = function(slotId) {
    if (!confirm('Eliminare slot ' + slotId + '?')) return;
    fetch('/api/teaching/delete/' + slotId, { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) { log('Slot ' + slotId + ' eliminato', 'ok'); loadSlotList(); }
        else log('Delete ERR', 'err');
      })
      .catch(function(e){ log('Delete rete: ' + e, 'err'); });
  };

  function loadSlotList() {
    fetch('/api/teaching/list')
      .then(function(r){ return r.json(); })
      .then(function(slots){
        var listEl = document.getElementById('slotList');
        var emptyEl = document.getElementById('slotListEmpty');
        listEl.innerHTML = '';
        if (!slots || !slots.length) { emptyEl.style.display = ''; return; }
        emptyEl.style.display = 'none';
        slots.forEach(function(s){
          var card = document.createElement('div');
          card.className = 'slot-card';
          var dur = s.duration_s ? s.duration_s.toFixed(1) + 's' : '--';
          var label = s.name ? ('«' + s.name + '»') : ('#' + s.slot_id);
          card.innerHTML = '<div class="slot-info"><span class="slot-id">' + label + '</span>'
            + '<span>' + (s.frames || 0) + ' frames</span>'
            + '<span class="slot-dur">' + dur + '</span></div>'
            + '<div class="slot-actions">'
            + '<button type="button" class="sl-play" onclick="teachPlaySlot(' + s.slot_id + ')">PLAY</button>'
            + '<button type="button" class="sl-del" onclick="teachDeleteSlot(' + s.slot_id + ')">DEL</button>'
            + '</div>';
          listEl.appendChild(card);
        });
      })
      .catch(function(){});
  }

  function updatePickUI(d) {
    var badge = document.getElementById('pickBadge');
    var line = document.getElementById('pickStatusLine');
    var range = document.getElementById('pickDepthRange');
    if (!d || !line) return;
    if (badge) {
      badge.textContent = d.enabled ? 'ON' : 'off';
      badge.className = 'teach-status ' + (d.enabled ? 'replaying' : 'idle');
    }
    if (range && d.depth_min_m != null) {
      range.textContent = d.depth_min_m + '\u2013' + d.depth_max_m;
    }
    var parts = [
      'Modo: ' + (d.pick_mode || 'teaching'),
      'Classe: ' + ((d.target_classes || []).join(', ') || '?'),
      'Slot: ' + (d.teaching_slot != null ? d.teaching_slot : '?'),
      'Trigger: ' + (d.trigger_count || 0),
    ];
    if (d.enabled && d.cooldown_remaining_s > 0) {
      parts.push('Cooldown ' + d.cooldown_remaining_s + 's');
    }
    if (d.last_detection) {
      parts.push('Ultimo: ' + d.last_detection.class + ' ' + d.last_detection.depth_m + 'm');
    }
    if (d.last_adjustments) {
      parts.push('Adj: ' + JSON.stringify(d.last_adjustments));
    }
    if (!d.calibrated) parts.push('Calibra la bottiglia sul punto teaching');
    if (d.ref_bbox_u != null) parts.push('Ref u=' + d.ref_bbox_u + ' d=' + d.ref_depth_m + 'm');
    if (d.last_error) parts.push('Err: ' + d.last_error);
    line.textContent = parts.join(' \u00b7 ');
  }

  window.pickCalibrate = function() {
    log('Pick: calibra riferimento bottiglia (camera ON, bottiglia in vista)');
    fetch('/api/pick/calibrate', { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) {
          log('Calibrazione OK u=' + d.ref_bbox_u + ' depth=' + d.ref_depth_m + 'm', 'ok');
          pollPickStatus();
        } else {
          log('Calibra ERR: ' + (d.error || ''), 'err');
        }
      })
      .catch(function(e){ log('Calibra rete: ' + e, 'err'); });
  };

  window.pickSetEnabled = function(on) {
    log('Auto-pick ' + (on ? 'ON' : 'OFF'));
    fetch('/api/pick/enable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!on }),
    })
      .then(function(r){ return r.json(); })
      .then(function(d){
        updatePickUI(d);
        if (d.enabled) log('Auto-pick attivo (camera deve essere ON)', 'ok');
        else log('Auto-pick disattivato', 'ok');
      })
      .catch(function(e){ log('Pick rete: ' + e, 'err'); });
  };

  window.pickManeuverTest = function() {
    log('Pick: test manovra safe_reach');
    fetch('/api/pick/maneuver', { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) log('Manovra avviata', 'ok');
        else log('Manovra ERR: ' + (d.error || ''), 'err');
        pollPickStatus();
      })
      .catch(function(e){ log('Manovra rete: ' + e, 'err'); });
  };

  window.pickTriggerTest = function() {
    log('Pick: test replay manuale');
    fetch('/api/pick/trigger', { method: 'POST' })
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (d.ok) log('Pick test OK', 'ok');
        else log('Pick test ERR: ' + (d.error || ''), 'err');
        pollPickStatus();
      })
      .catch(function(e){ log('Pick test rete: ' + e, 'err'); });
  };

  function pollPickStatus() {
    fetch('/api/pick/status')
      .then(function(r){ return r.json(); })
      .then(updatePickUI)
      .catch(function(){});
  }
  pollPickStatus();
  setInterval(pollPickStatus, 2500);

  loadSlotList();
})();
</script>
</body>
</html>
"""


VR_CONTROL_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>G1 VR Control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
html,body{min-height:100vh;background:#08090d;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;overflow-x:hidden;}
body{padding:0 0 max(24px,env(safe-area-inset-bottom));}
.top-bar{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#111318;border-bottom:1px solid #27272a;position:sticky;top:0;z-index:20;}
.top-bar h1{font-size:18px;font-weight:700;color:#a78bfa;}
.estop-btn{padding:12px 24px;background:#dc2626;color:#fff;font-size:16px;font-weight:800;border:2px solid #ef4444;border-radius:12px;cursor:pointer;text-transform:uppercase;letter-spacing:1px;animation:none;}
.estop-btn:active{background:#991b1b;transform:scale(0.95);}
.main{padding:12px 16px;display:flex;flex-direction:column;gap:14px;}
.panel{background:#111318;border:1px solid #27272a;border-radius:14px;padding:14px;}
.panel-title{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#71717a;font-weight:700;margin-bottom:10px;}
.status-row{display:flex;align-items:center;gap:12px;flex-wrap:wrap;}
.badge{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;background:#27272a;color:#a1a1aa;}
.badge.ok{background:#14532d;color:#4ade80;}
.badge.warn{background:#78350f;color:#fbbf24;}
.badge.err{background:#7f1d1d;color:#fca5a5;}
.badge.active{background:#312e81;color:#a78bfa;}
.tracking-hands{display:flex;gap:16px;margin-top:10px;}
.hand-card{flex:1;padding:10px;background:#0a0b10;border:1px solid #27272a;border-radius:10px;text-align:center;}
.hand-card .hand-icon{font-size:28px;line-height:1;}
.hand-card .hand-label{font-size:10px;color:#71717a;margin:4px 0;}
.hand-card .hand-pos{font-size:10px;color:#52525b;font-family:monospace;}
.hand-card.tracking{border-color:#14b8a680;}
.hand-card.lost{border-color:#ef444440;opacity:0.5;}
.stick-wrap{display:flex;justify-content:center;margin:4px 0;}
.stick-wrap canvas{background:#0a0b10;border:1px solid #27272a;border-radius:12px;}
.ctrl-btns{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;}
.ctrl-btn{padding:14px 28px;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;-webkit-tap-highlight-color:transparent;transition:background 0.15s,transform 0.1s;}
.ctrl-btn:active{transform:scale(0.95);}
.ctrl-btn.start{background:rgba(20,184,166,0.2);border-color:rgba(20,184,166,0.5);color:#5eead4;font-size:18px;padding:16px 40px;}
.ctrl-btn.stop{background:rgba(239,68,68,0.15);border-color:rgba(239,68,68,0.4);color:#fca5a5;}
.ctrl-btn.cal{background:rgba(99,102,241,0.15);border-color:rgba(99,102,241,0.4);color:#a5b4fc;}
.ctrl-btn:disabled{opacity:0.3;pointer-events:none;}
.loco-panel{display:flex;align-items:center;gap:12px;justify-content:center;}
.loco-circle{width:100px;height:100px;border-radius:50%;background:#0a0b10;border:2px solid #27272a;position:relative;display:flex;align-items:center;justify-content:center;}
.loco-dot{width:16px;height:16px;border-radius:50%;background:#14b8a6;box-shadow:0 0 8px #14b8a640;position:absolute;transition:transform 0.05s;}
.loco-label{font-size:10px;color:#52525b;text-align:center;font-family:monospace;}
.loco-vals{font-size:11px;color:#71717a;font-family:monospace;text-align:center;}
.log-area{max-height:80px;overflow-y:auto;padding:6px 8px;font-size:10px;color:#52525b;font-family:monospace;line-height:1.5;background:#0a0b10;border-radius:8px;margin-top:8px;}
.log-area .ok{color:#22c55e;}.log-area .err{color:#ef4444;}.log-area .warn{color:#fbbf24;}
</style>
</head>
<body>
<div class="top-bar">
  <h1>G1 VR Control</h1>
  <button type="button" class="estop-btn" onclick="vrEmergencyStop()">E-STOP</button>
</div>
<div class="main">

  <div class="panel">
    <div class="panel-title">Stato</div>
    <div class="status-row">
      <span class="badge" id="vrStateBadge">idle</span>
      <span class="badge" id="vrTrackBadge">no tracking</span>
      <span style="font-size:11px;color:#52525b;">HTS porta: <span id="vrPort">9000</span></span>
    </div>
    <div class="tracking-hands">
      <div class="hand-card lost" id="handLeft">
        <div class="hand-icon">&#x1F91A;</div>
        <div class="hand-label">Sinistra</div>
        <div class="hand-pos" id="handLeftPos">--</div>
      </div>
      <div class="hand-card lost" id="handRight">
        <div class="hand-icon">&#x270B;</div>
        <div class="hand-label">Destra</div>
        <div class="hand-pos" id="handRightPos">--</div>
      </div>
    </div>
    <div style="margin-top:10px;padding:8px;background:#0a0b10;border:1px solid #27272a;border-radius:8px;font-size:10px;font-family:monospace;color:#71717a;">
      <div style="display:flex;gap:16px;flex-wrap:wrap;">
        <span>UDP pkts: <span id="vrUdpPkts" style="color:#a1a1aa;">0</span></span>
        <span>Sorgente: <span id="vrUdpSrc" style="color:#a1a1aa;">--</span></span>
        <span>Ultimo: <span id="vrUdpAge" style="color:#a1a1aa;">--</span></span>
        <span>Errori: <span id="vrUdpErr" style="color:#a1a1aa;">0</span></span>
      </div>
    </div>
  </div>

  <div class="panel" style="border-color:rgba(251,191,36,0.3);background:linear-gradient(135deg,#111318 0%,#1a170e 100%);">
    <div class="panel-title" style="color:#fbbf24;">&#128218; Guida Setup VR — Hand Tracking</div>
    <div style="font-size:12px;line-height:1.7;color:#d4d4d8;">
      <p style="margin:0 0 8px;"><strong style="color:#fbbf24;">Cosa serve:</strong> Meta Quest 2/3 + app <strong>&quot;Hand Tracking Streamer&quot; (HTS)</strong></p>
      <ol style="margin:0 0 10px;padding-left:20px;color:#e4e4e7;">
        <li>Installa <strong>HTS</strong> sul Quest da <strong>SideQuest</strong> (cerca &quot;Hand Tracking Streamer&quot;).</li>
        <li>Apri HTS sul Quest.</li>
        <li>In HTS inserisci l'<strong>IP del Jetson/server</strong>: <code id="vrGuideIp" style="background:#27272a;padding:2px 6px;border-radius:4px;color:#4ade80;font-size:13px;">caricamento...</code></li>
        <li>Imposta <strong>porta: <code style="background:#27272a;padding:2px 6px;border-radius:4px;color:#4ade80;font-size:13px;">9000</code></strong></li>
        <li>Premi <strong>&quot;Start&quot;</strong> in HTS.</li>
        <li>Torna su questa pagina, premi <strong>START VR</strong>, poi <strong>CALIBRA</strong> con le braccia rilassate.</li>
      </ol>
      <div style="padding:8px 10px;background:rgba(20,184,166,0.1);border:1px solid rgba(20,184,166,0.3);border-radius:8px;font-size:11px;color:#5eead4;">
        <strong>&#9432; Come funziona:</strong> HTS invia le posizioni delle mani <strong>dal Quest al Jetson</strong> via UDP.
        Il Quest trasmette, il Jetson riceve. Non devi trovare l'IP del Quest.
        Quest e Jetson devono essere sulla <strong>stessa rete WiFi/LAN</strong>.
      </div>
      <div id="vrSetupHint" style="margin-top:8px;padding:8px 10px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:8px;font-size:11px;color:#fca5a5;display:none;">
        <strong>&#9888; Nessun dato UDP ricevuto.</strong> Controlla che HTS sia avviato sul Quest con IP e porta corretti.
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Robot</div>
    <div class="stick-wrap"><canvas id="vrStickCanvas" width="480" height="360"></canvas></div>
  </div>

  <div class="panel">
    <div class="panel-title">Controlli</div>
    <div class="ctrl-btns">
      <button type="button" class="ctrl-btn start" id="btnVrStart" onclick="vrStart()">START VR</button>
      <button type="button" class="ctrl-btn cal" id="btnVrCal" onclick="vrCalibrate()" disabled>CALIBRA</button>
      <button type="button" class="ctrl-btn stop" id="btnVrStop" onclick="vrStop()" disabled>STOP</button>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Locomozione (controller)</div>
    <div class="loco-panel">
      <div>
        <div class="loco-label">Move</div>
        <div class="loco-circle" id="locoMoveCircle">
          <div class="loco-dot" id="locoMoveDot"></div>
        </div>
      </div>
      <div class="loco-vals">
        <div>vx: <span id="locoVx">0.00</span></div>
        <div>vy: <span id="locoVy">0.00</span></div>
        <div>vyaw: <span id="locoVyaw">0.00</span></div>
      </div>
      <div>
        <div class="loco-label">Rotate</div>
        <div class="loco-circle" id="locoRotCircle">
          <div class="loco-dot" id="locoRotDot" style="background:#a78bfa;box-shadow:0 0 8px #a78bfa40;"></div>
        </div>
      </div>
    </div>
  </div>

  <div class="log-area" id="vrLogArea"></div>
</div>

<script>
(function(){
  var logEl = document.getElementById('vrLogArea');
  function log(msg, cls) {
    var d = document.createElement('div');
    d.className = cls || '';
    d.textContent = new Date().toLocaleTimeString() + ' ' + msg;
    logEl.appendChild(d);
    if (logEl.children.length > 40) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  }

  /* ── API calls ── */
  function apiPost(path) {
    return fetch('/api/vr/' + path, { method: 'POST', headers: {'Content-Type':'application/json'} })
      .then(function(r){ return r.json(); });
  }

  window.vrStart = function() {
    log('Avvio VR...');
    apiPost('start').then(function(d) {
      if (d.ok) { log('VR avviato — apri HTS sul Quest (vedi guida sotto), poi premi CALIBRA', 'ok'); }
      else { log('Errore avvio: ' + (d.error || ''), 'err'); }
    }).catch(function(e){ log('Network: ' + e, 'err'); });
  };

  window.vrCalibrate = function() {
    log('Calibrazione posa neutra...');
    apiPost('calibrate').then(function(d) {
      if (d.ok) { log('Calibrato! Controllo VR attivo', 'ok'); }
      else { log('Errore calibrazione: ' + (d.error || ''), 'err'); }
    }).catch(function(e){ log('Network: ' + e, 'err'); });
  };

  window.vrStop = function() {
    log('Fermo VR...');
    apiPost('stop').then(function(d) {
      if (d.ok) log('VR fermato', 'ok');
    }).catch(function(e){ log('Network: ' + e, 'err'); });
  };

  window.vrEmergencyStop = function() {
    log('ARRESTO DI EMERGENZA!', 'err');
    apiPost('emergency_stop').catch(function(){});
  };

  /* ── SSE stream ── */
  var vrEvt = null;
  function connectVrSSE() {
    if (vrEvt) { try { vrEvt.close(); } catch(e){} }
    vrEvt = new EventSource('/api/vr/stream');
    vrEvt.onmessage = function(e) {
      try {
        var d = JSON.parse(e.data);
        updateVrUI(d);
        if (d.joints) {
          vrStickTarget.waist = d.joints.waist || [0,0,0];
          vrStickTarget.left = d.joints.left_arm || [0,0,0,0,0,0,0];
          vrStickTarget.right = d.joints.right_arm || [0,0,0,0,0,0,0];
        }
      } catch(err){}
    };
    vrEvt.onerror = function() { setTimeout(connectVrSSE, 3000); };
  }
  connectVrSSE();

  (function setGuideIp(){
    var el = document.getElementById('vrGuideIp');
    if (el) el.textContent = window.location.hostname || '192.168.123.161';
  })();

  function updateVrUI(d) {
    var st = d.state || 'idle';
    var sb = document.getElementById('vrStateBadge');
    sb.textContent = st;
    sb.className = 'badge' + (st === 'active' ? ' active' : st === 'error' ? ' err' : '');

    var tb = document.getElementById('vrTrackBadge');
    tb.textContent = d.tracking ? 'tracciamento OK' : 'nessun tracciamento';
    tb.className = 'badge' + (d.tracking ? ' ok' : ' warn');

    document.getElementById('vrPort').textContent = d.port || '9000';

    var hl = document.getElementById('handLeft');
    var hr = document.getElementById('handRight');
    hl.className = 'hand-card ' + (d.tracking_left ? 'tracking' : 'lost');
    hr.className = 'hand-card ' + (d.tracking_right ? 'tracking' : 'lost');

    if (d.left_wrist) {
      document.getElementById('handLeftPos').textContent = d.left_wrist.map(function(v){return v.toFixed(3);}).join(', ');
    }
    if (d.right_wrist) {
      document.getElementById('handRightPos').textContent = d.right_wrist.map(function(v){return v.toFixed(3);}).join(', ');
    }

    document.getElementById('vrUdpPkts').textContent = d.udp_packets || 0;
    document.getElementById('vrUdpSrc').textContent = d.udp_source || '--';
    document.getElementById('vrUdpErr').textContent = d.udp_errors || 0;
    var ageEl = document.getElementById('vrUdpAge');
    if (d.udp_age_ms != null) {
      var ageSec = (d.udp_age_ms / 1000).toFixed(1);
      ageEl.textContent = ageSec + 's fa';
      ageEl.style.color = d.udp_age_ms < 500 ? '#4ade80' : d.udp_age_ms < 2000 ? '#fbbf24' : '#ef4444';
    } else {
      ageEl.textContent = '--';
      ageEl.style.color = '#a1a1aa';
    }
    var hint = document.getElementById('vrSetupHint');
    hint.style.display = (!d.udp_packets && st !== 'idle') ? '' : 'none';

    document.getElementById('btnVrStart').disabled = (st !== 'idle' && st !== 'error');
    document.getElementById('btnVrCal').disabled = (st !== 'calibrating' && st !== 'active');
    document.getElementById('btnVrStop').disabled = (st === 'idle');
  }

  /* ── Manichino robot (torso + braccia realistiche) ── */
  var vrCvs = document.getElementById('vrStickCanvas');
  var vrCtx = vrCvs ? vrCvs.getContext('2d') : null;
  var vrStickTarget = {waist:[0,0,0], left:[0,0,0,0,0,0,0], right:[0,0,0,0,0,0,0]};
  var vrStickCur = {waist:[0,0,0], left:[0,0,0,0,0,0,0], right:[0,0,0,0,0,0,0]};

  function lerpArr(cur, tgt, a) { for (var i=0;i<cur.length;i++) cur[i]+=(tgt[i]-cur[i])*a; }

  function drawVrStick() {
    if (!vrCtx) return;
    var W=vrCvs.width, H=vrCvs.height, ctx=vrCtx;
    ctx.clearRect(0,0,W,H);
    lerpArr(vrStickCur.waist, vrStickTarget.waist, 0.25);
    lerpArr(vrStickCur.left, vrStickTarget.left, 0.25);
    lerpArr(vrStickCur.right, vrStickTarget.right, 0.25);

    var cx=W/2, hipY=255, neckY=105, shoulderW=50;
    var wY=vrStickCur.waist[0], wR=vrStickCur.waist[1], wP=vrStickCur.waist[2]||0;
    ctx.save();
    ctx.translate(cx, hipY);
    ctx.rotate(wR*0.3);
    var tiltX=-wY*0.15;

    /* Busto (trapezio) */
    ctx.fillStyle='#1e1e24';
    ctx.strokeStyle='#3f3f46'; ctx.lineWidth=2; ctx.lineCap='round'; ctx.lineJoin='round';
    ctx.beginPath();
    ctx.moveTo(-18, 0);
    ctx.lineTo(-shoulderW, neckY-hipY+12);
    ctx.lineTo(shoulderW, neckY-hipY+12);
    ctx.lineTo(18, 0);
    ctx.closePath();
    ctx.fill(); ctx.stroke();

    /* Colonna vertebrale */
    ctx.strokeStyle='#52525b'; ctx.lineWidth=3;
    ctx.beginPath(); ctx.moveTo(tiltX,0); ctx.lineTo(tiltX,neckY-hipY); ctx.stroke();

    /* Testa */
    var headR=20, headY=neckY-hipY-headR-5;
    ctx.fillStyle='#27272a';
    ctx.beginPath(); ctx.arc(tiltX,headY,headR,0,Math.PI*2); ctx.fill();
    ctx.strokeStyle='#52525b'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.arc(tiltX,headY,headR,0,Math.PI*2); ctx.stroke();
    /* Occhi */
    ctx.fillStyle='#14b8a6';
    ctx.beginPath(); ctx.arc(tiltX-6,headY-3,2.5,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.arc(tiltX+6,headY-3,2.5,0,Math.PI*2); ctx.fill();

    /* Spalle (cerchi piccoli) */
    var sy=neckY-hipY+12, lsx=tiltX-shoulderW, rsx=tiltX+shoulderW;
    ctx.fillStyle='#3f3f46';
    ctx.beginPath(); ctx.arc(lsx,sy,6,0,Math.PI*2); ctx.fill();
    ctx.beginPath(); ctx.arc(rsx,sy,6,0,Math.PI*2); ctx.fill();

    var UP=75, FO=68;
    function drawArm(j, side, sx) {
      var sp=j[0], sr=j[1], sy_j=j[2]||0, el=j[3], wr=j[4], wp=j[5]||0;
      /* Angolo braccio superiore */
      var ba = (side<0) ? (Math.PI/2 + sr*0.8 - sp*0.35) : (Math.PI/2 - sr*0.8 + sp*0.35);
      ba += sy_j * side * 0.15;
      var ux = sx + Math.cos(ba)*UP;
      var uy = sy + Math.sin(ba)*UP;
      /* Avambraccio */
      var ea = ba + el*0.85;
      var wx = ux + Math.cos(ea)*FO;
      var wy = uy + Math.sin(ea)*FO;

      /* Braccio superiore */
      var armColor = (side<0) ? '#14b8a6' : '#a78bfa';
      var armGlow = (side<0) ? 'rgba(20,184,166,0.15)' : 'rgba(167,139,250,0.15)';
      ctx.strokeStyle=armColor; ctx.lineWidth=8; ctx.lineCap='round';
      ctx.beginPath(); ctx.moveTo(sx,sy); ctx.lineTo(ux,uy); ctx.stroke();
      /* Avambraccio */
      ctx.lineWidth=7;
      ctx.beginPath(); ctx.moveTo(ux,uy); ctx.lineTo(wx,wy); ctx.stroke();

      /* Gomito (cerchio giallo) */
      ctx.fillStyle='#fbbf24'; ctx.strokeStyle='#a16207'; ctx.lineWidth=2;
      ctx.beginPath(); ctx.arc(ux,uy,6,0,Math.PI*2); ctx.fill(); ctx.stroke();

      /* Mano (cerchio grande con alone) */
      ctx.fillStyle=armGlow;
      ctx.beginPath(); ctx.arc(wx,wy,16,0,Math.PI*2); ctx.fill();
      ctx.fillStyle=armColor; ctx.globalAlpha=0.9;
      ctx.beginPath(); ctx.arc(wx,wy,10,0,Math.PI*2); ctx.fill();
      ctx.globalAlpha=1.0;
      ctx.strokeStyle='rgba(255,255,255,0.3)'; ctx.lineWidth=1.5;
      ctx.beginPath(); ctx.arc(wx,wy,10,0,Math.PI*2); ctx.stroke();

      /* Polso: rotazione indicata come linea sulla mano */
      var wrAngle = ea + (wr||0)*0.8;
      ctx.strokeStyle='rgba(255,255,255,0.5)'; ctx.lineWidth=2;
      ctx.beginPath();
      ctx.moveTo(wx-Math.cos(wrAngle)*7, wy-Math.sin(wrAngle)*7);
      ctx.lineTo(wx+Math.cos(wrAngle)*7, wy+Math.sin(wrAngle)*7);
      ctx.stroke();
    }
    drawArm(vrStickCur.left, -1, lsx);
    drawArm(vrStickCur.right, 1, rsx);

    /* Etichette */
    ctx.font='bold 10px monospace'; ctx.textAlign='center';
    ctx.fillStyle='#14b8a6'; ctx.fillText('SX', lsx, sy-14);
    ctx.fillStyle='#a78bfa'; ctx.fillText('DX', rsx, sy-14);

    ctx.restore();
    requestAnimationFrame(drawVrStick);
  }
  requestAnimationFrame(drawVrStick);

  /* ── Gamepad (Quest controllers) ── */
  var locoVx=0, locoVy=0, locoVyaw=0;
  var locoSendIv=null;
  var SPEED_MAX=0.5, YAW_MAX=0.8;

  function pollGamepad() {
    var gps = navigator.getGamepads ? navigator.getGamepads() : [];
    var lx=0,ly=0,rx=0,ry=0, found=false;
    for (var i=0; i<gps.length; i++) {
      var g=gps[i];
      if (!g || !g.connected) continue;
      found = true;
      if (g.axes.length >= 2) { lx=g.axes[0]||0; ly=g.axes[1]||0; }
      if (g.axes.length >= 4) { rx=g.axes[2]||0; ry=g.axes[3]||0; }
      break;
    }
    var dead=0.12;
    if (Math.abs(lx)<dead) lx=0; if (Math.abs(ly)<dead) ly=0;
    if (Math.abs(rx)<dead) rx=0;

    locoVx = Math.round(-ly * SPEED_MAX * 100) / 100;
    locoVy = Math.round(-lx * SPEED_MAX * 100) / 100;
    locoVyaw = Math.round(-rx * YAW_MAX * 100) / 100;

    document.getElementById('locoVx').textContent = locoVx.toFixed(2);
    document.getElementById('locoVy').textContent = locoVy.toFixed(2);
    document.getElementById('locoVyaw').textContent = locoVyaw.toFixed(2);

    var md=document.getElementById('locoMoveDot');
    md.style.transform='translate('+(-lx*36)+'px,'+(ly*36)+'px)';
    var rd=document.getElementById('locoRotDot');
    rd.style.transform='translate('+(-rx*36)+'px,0px)';

    requestAnimationFrame(pollGamepad);
  }
  requestAnimationFrame(pollGamepad);

  function sendLoco() {
    if (Math.abs(locoVx)>0.01 || Math.abs(locoVy)>0.01 || Math.abs(locoVyaw)>0.01) {
      fetch('/api/vr/locomotion', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({vx:locoVx, vy:locoVy, vyaw:locoVyaw})
      }).catch(function(){});
    }
  }
  locoSendIv = setInterval(sendLoco, 200);

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
    print("  Dashboard (soundboard + servizi): /dashboard/")
    print("  Robot Control (joystick+gesti): /robot-control")
    print("  VR Control (Quest 3): /vr-control")
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
