#!/usr/bin/env python3
"""
Esporta gli audio della soundboard da config/soundboard.json in file WAV.

Esporta **solo** la traccia clean (`audio_base64_clean` + `format_clean`), non la versione
«robot» (`audio_base64`). Slot senza clean vengono saltati.

La soundboard sul server/Jetson è nel JSON, non in cartelle .wav. Decodifica + ffmpeg opzionale.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SB = ROOT / "config" / "soundboard.json"
JETSON_SB = ROOT / "config" / "soundboard.json.from_jetson"
DEFAULT_OUT = ROOT / "exports" / "soundboard_wav"

# Caratteri non ammessi nei nomi file su Windows
_WIN_FORBIDDEN = frozenset('<>:"/\\|?*')


def _ui_label(slot: dict, index: int) -> str:
    """Stessa logica della griglia web: (s.text || 'Comando ' + (i+1))."""
    t = str(slot.get("text") or "").strip()
    return t if t else f"Comando {index + 1}"


def _safe_filename_stem(label: str, max_len: int = 72) -> str:
    """Pulisce il testo visibile in app per usarlo come parte del nome file."""
    parts: list[str] = []
    for ch in label:
        if ch in _WIN_FORBIDDEN or ord(ch) < 32:
            parts.append("_")
        else:
            parts.append(ch)
    s = "".join(parts)
    s = re.sub(r"[\s_]+", "_", s).strip(" ._")
    if len(s) > max_len:
        s = s[:max_len].rstrip(" ._")
    return s or "audio"


def _out_basename(index: int, label: str, suffix: str) -> str:
    """es. 03_Buonasera_ospiti.wav"""
    stem = _safe_filename_stem(label)
    return f"{index:02d}_{stem}{suffix}"


def _suffix_for_raw(raw: bytes, fmt: str) -> str:
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return ".wav"
    fl = (fmt or "").lower().split(";")[0].strip()
    if "webm" in fl:
        return ".webm"
    if "wav" in fl:
        return ".wav"
    if "mp3" in fl or fl in ("mpeg", "audio/mpeg"):
        return ".mp3"
    if len(raw) >= 4 and raw[:4] == b"\x1a\x45\xdf\xa3":
        return ".webm"
    if len(raw) >= 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ".webm"


def _write_wav(raw: bytes, fmt: str, out_wav: Path) -> tuple[str, Path | None]:
    """
    Returns (status, path_written).
    status: 'wav' | 'ffmpeg' | 'raw' | 'empty'
    """
    if not raw or len(raw) < 8:
        return "empty", None
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        out_wav.write_bytes(raw)
        return "wav", out_wav
    suf = _suffix_for_raw(raw, fmt)
    if suf == ".wav":
        out_wav.write_bytes(raw)
        return "wav", out_wav
    with tempfile.NamedTemporaryFile(suffix=suf, delete=False) as f:
        f.write(raw)
        inp = Path(f.name)
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
                str(out_wav),
            ],
            capture_output=True,
            timeout=120,
        )
        if r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 64:
            return "ffmpeg", out_wav
    except FileNotFoundError:
        pass
    finally:
        inp.unlink(missing_ok=True)
    raw_path = out_wav.with_suffix(suf)
    raw_path.write_bytes(raw)
    return "raw", raw_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Esporta soundboard JSON → WAV (o raw se manca ffmpeg)")
    ap.add_argument("--json", type=Path, default=None, help="Path soundboard.json (default: da Jetson se esiste .from_jetson, altrimenti config/soundboard.json)")
    ap.add_argument(
        "--from-jetson",
        action="store_true",
        help="Usa config/soundboard.json.from_jetson (dopo pull_soundboard_from_jetson.ps1) e scrive in exports/soundboard_wav_jetson",
    )
    ap.add_argument("--out", type=Path, default=None, help="Cartella output")
    args = ap.parse_args()
    if args.from_jetson:
        sb_path = JETSON_SB
        out_dir = args.out or (ROOT / "exports" / "soundboard_wav_jetson")
    elif args.json is not None:
        sb_path = args.json
        out_dir = args.out or DEFAULT_OUT
    else:
        sb_path = JETSON_SB if JETSON_SB.exists() else DEFAULT_SB
        out_dir = args.out or (
            ROOT / "exports" / "soundboard_wav_jetson" if sb_path == JETSON_SB else DEFAULT_OUT
        )

    if not sb_path.exists():
        print(f"Manca file: {sb_path}", file=sys.stderr)
        return 1

    data = json.loads(sb_path.read_text(encoding="utf-8"))
    slots = data.get("slots")
    if not isinstance(slots, list):
        print("JSON senza lista 'slots'", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        sb_rel = sb_path.relative_to(ROOT)
    except ValueError:
        sb_rel = sb_path
    readme = out_dir / "README.txt"
    readme.write_text(
        f"Origine JSON: {sb_rel}\n"
        "Solo traccia clean (audio_base64_clean). Nomi = indice + testo pulsante come in web /client.\n"
        "Sul robot l'audio resta nel JSON, non in cartelle .wav dedicate.\n",
        encoding="utf-8",
    )

    exported = 0
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            continue
        label = _ui_label(s, i)
        cb64 = str(s.get("audio_base64_clean") or "").strip()
        if not cb64:
            continue
        try:
            raw = base64.b64decode(cb64)
        except Exception as e:
            print(f"slot {i}: audio_base64_clean non valido: {e}", file=sys.stderr)
            continue
        cfmt = str(s.get("format_clean") or s.get("format") or "mp3")
        out_wav = out_dir / _out_basename(i, label, ".wav")
        how, path = _write_wav(raw, cfmt, out_wav)
        if path:
            print(f"{label!r}  ({how})  ->  {path.relative_to(ROOT)}  [{path.stat().st_size} byte]")
            exported += 1

    print(f"\nTotale file scritti: {exported}\nCartella: {out_dir}")
    return 0 if exported else 1


if __name__ == "__main__":
    raise SystemExit(main())
