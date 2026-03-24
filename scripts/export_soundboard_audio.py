#!/usr/bin/env python3
"""
Estrae gli audio dalla soundboard in file separati.
Legge config/soundboard.json e scrive in una cartella (default: dist/soundboard_audio).

Uso:
  python scripts/export_soundboard_audio.py
  python scripts/export_soundboard_audio.py -o dist/DACONdividereG1Talk/audio
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
SOUNDBOARD_PATH = _root / "config" / "soundboard.json"
SCRIPT_PATH = _root / "config" / "soundboard_script.json"
DEFAULT_OUT = _root / "dist" / "soundboard_audio"


def _slug(s: str, max_len: int = 40) -> str:
    """Estrae slug da label per nome file."""
    s = re.sub(r"[^\w\s\-]", "", s.lower())
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:max_len] if s else "slot"


def main() -> int:
    ap = argparse.ArgumentParser(description="Esporta audio soundboard in file")
    ap.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="Cartella output")
    args = ap.parse_args()
    out_dir = Path(args.output)

    if not SOUNDBOARD_PATH.exists():
        print("Manca", SOUNDBOARD_PATH)
        return 1

    data = json.loads(SOUNDBOARD_PATH.read_text(encoding="utf-8"))
    slots = data.get("slots") or []
    script_entries = []
    if SCRIPT_PATH.exists():
        script_data = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
        script_entries = script_data.get("entries") or []
    out_dir.mkdir(parents=True, exist_ok=True)

    lines = ["# Testi generati per la soundboard", "# Corrispondono ai file audio in questa cartella", ""]
    count = 0
    for i, slot in enumerate(slots):
        label = str(slot.get("text", f"slot_{i+1}")).strip()
        testo = ""
        if i < len(script_entries):
            testo = str(script_entries[i].get("testo_tts", label)).strip()
        else:
            testo = label
        slug = _slug(label) or f"slot_{i+1:02d}"
        prefix = f"{i+1:02d}_{slug}"
        lines.append(f"---\n{i+1}. {label}\n{testo}")
        lines.append("")

        for key, suffix in [("audio_base64", "robot"), ("audio_base64_clean", "naturale")]:
            b64 = slot.get(key)
            fmt = slot.get("format" if key == "audio_base64" else "format_clean", "mp3")
            if not b64 or len(b64) < 100:
                continue
            ext = "mp3" if fmt == "mp3" else "wav"
            path = out_dir / f"{prefix}_{suffix}.{ext}"
            raw = base64.b64decode(b64)
            path.write_bytes(raw)
            count += 1
            print(f"  {path.name}")

    # Scrivi file con tutti i testi
    txt_path = out_dir / "testi_soundboard.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  {txt_path.name}")

    print(f"\nEsportati {count} file + testi in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
