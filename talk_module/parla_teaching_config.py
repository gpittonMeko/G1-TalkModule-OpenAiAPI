"""Parla tab: up to 3 Explore teachings played randomly during spoken answers."""

from __future__ import annotations

import json
from pathlib import Path

MAX_PARLA_TEACHING_GESTURES = 3


def parla_teaching_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "parla_teaching_gestures.json"


def load_parla_teaching_gestures() -> list[str]:
    path = parla_teaching_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("gestures") if isinstance(data, dict) else []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= MAX_PARLA_TEACHING_GESTURES:
            break
    return out


def save_parla_teaching_gestures(gestures: list[str] | None) -> list[str]:
    clean: list[str] = []
    for item in gestures or []:
        name = str(item or "").strip()
        if name and name not in clean:
            clean.append(name)
        if len(clean) >= MAX_PARLA_TEACHING_GESTURES:
            break
    path = parla_teaching_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"gestures": clean}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return clean
