"""
REST API opzionale per integrazione con Web Interface MekoAiAccelerator.
Avvio: python -m talk_module.api_server
"""

import os
import tempfile
from pathlib import Path

# FastAPI opzionale - non obbligatorio per CLI
try:
    from fastapi import FastAPI, HTTPException, UploadFile, File, Form
    from fastapi.responses import Response
    from pydantic import BaseModel
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

from talk_module.config import settings
from talk_module.stt import WhisperClient
from talk_module.llm import create_llm_client
from talk_module.tts import TTSClient
from talk_module.audio import AudioPlayer, list_audio_devices


if HAS_FASTAPI:

    app = FastAPI(
        title="G1 Talk Module API",
        description="API per STT, LLM, TTS - integrazione robot vocale",
        version="1.0.0",
    )

    _stt: WhisperClient | None = None
    _llm = None
    _tts: TTSClient | None = None
    _player: AudioPlayer | None = None

    def get_stt() -> WhisperClient:
        global _stt
        if _stt is None:
            _stt = WhisperClient()
        return _stt

    def get_llm():
        global _llm
        if _llm is None:
            _llm = create_llm_client()
        return _llm

    def get_tts() -> TTSClient:
        global _tts
        if _tts is None:
            _tts = TTSClient()
        return _tts

    def get_player() -> AudioPlayer:
        global _player
        if _player is None:
            _player = AudioPlayer()
        return _player

    @app.get("/health")
    def health():
        return {"status": "ok", "module": "talk"}

    @app.get("/devices")
    def devices():
        return {"devices": list_audio_devices()}

    class ChatRequest(BaseModel):
        text: str

    @app.post("/chat")
    def chat(req: ChatRequest):
        """Input testo, risposta LLM in JSON."""
        if not req.text.strip():
            raise HTTPException(400, "Testo vuoto")
        resp = get_llm().chat(req.text.strip())
        return {"response": resp}

    @app.post("/tts")
    def tts_synthesize(req: ChatRequest):
        """Input testo, output MP3 audio."""
        if not req.text.strip():
            raise HTTPException(400, "Testo vuoto")
        audio = get_tts().synthesize(req.text.strip(), format="mp3")
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/stt")
    async def stt_transcribe(file: UploadFile = File(...)):
        """Input audio file (WAV/MP3), output trascrizione."""
        data = await file.read()
        if len(data) < 100:
            raise HTTPException(400, "File audio troppo piccolo")
        text = get_stt().transcribe(data)
        return {"text": text}

    @app.post("/voice-chat")
    async def voice_chat(file: UploadFile = File(...)):
        """
        Pipeline completa: audio in -> trascrizione -> LLM -> TTS -> audio out.
        Ritorna JSON con text, response e audio base64 (opzionale).
        """
        data = await file.read()
        if len(data) < 100:
            raise HTTPException(400, "File audio troppo piccolo")
        text = get_stt().transcribe(data)
        if not text:
            return {"text": "", "response": "", "message": "Nessun testo riconosciuto"}
        resp = get_llm().chat(text)
        audio_b64 = ""
        if resp:
            audio = get_tts().synthesize(resp, format="mp3")
            if audio:
                import base64
                audio_b64 = base64.b64encode(audio).decode()
        return {"text": text, "response": resp, "audio_base64": audio_b64}

    @app.post("/play-audio")
    async def play_audio(file: UploadFile = File(...)):
        """Riproduce file audio inviato (WAV/MP3)."""
        data = await file.read()
        ext = Path(file.filename or "").suffix.lower() or ".mp3"
        fmt = "mp3" if ext == ".mp3" else "wav"
        ok = get_player().play_bytes(data, format_hint=fmt)
        return {"played": ok}

    @app.post("/llm/reset")
    def llm_reset():
        """Azzera cronologia conversazione LLM."""
        get_llm().reset_history()
        return {"ok": True}


def run_server(host: str = "0.0.0.0", port: int = 8081):
    """Avvia server con uvicorn."""
    if not HAS_FASTAPI:
        print("Installa: pip install fastapi uvicorn")
        return
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    args = p.parse_args()
    if HAS_FASTAPI:
        run_server(args.host, args.port)
    else:
        print("Installa le dipendenze API: pip install fastapi uvicorn")
