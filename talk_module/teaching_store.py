"""
Teaching trajectory storage -- save/load arm recordings as JSON.

Directory: config/teachings/
Format: { "meta": {...}, "frames": [ { "t": seconds, "q": [17 floats] } ] }
"""

import json
import os
from pathlib import Path
from typing import Optional

TEACHINGS_DIR = Path(__file__).resolve().parent.parent / "config" / "teachings"


def _ensure_dir():
    TEACHINGS_DIR.mkdir(parents=True, exist_ok=True)


def save_trajectory(slot_id: int, data: dict) -> Path:
    """Save a trajectory dict to config/teachings/slot_{slot_id}.json."""
    _ensure_dir()
    p = TEACHINGS_DIR / f"slot_{slot_id}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[TeachingStore] saved {p} ({len(data.get('frames', []))} frames)", flush=True)
    return p


def save_temp(data: dict) -> Path:
    """Save a 'temporary' trajectory that hasn't been assigned to a slot yet."""
    _ensure_dir()
    p = TEACHINGS_DIR / "_temp.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return p


def load_trajectory(slot_id: int) -> Optional[dict]:
    """Load a trajectory for a given slot, or None."""
    p = TEACHINGS_DIR / f"slot_{slot_id}.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_temp() -> Optional[dict]:
    p = TEACHINGS_DIR / "_temp.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_trajectory(slot_id: int) -> bool:
    p = TEACHINGS_DIR / f"slot_{slot_id}.json"
    if p.exists():
        os.remove(p)
        return True
    return False


def delete_temp() -> bool:
    p = TEACHINGS_DIR / "_temp.json"
    if p.exists():
        os.remove(p)
        return True
    return False


def find_slot_by_name(name: str) -> Optional[int]:
    """Trova slot teaching per meta.name (es. «spiegazione»)."""
    key = (name or "").strip().lower()
    if not key:
        return None
    _ensure_dir()
    for f in TEACHINGS_DIR.glob("slot_*.json"):
        try:
            slot_id = int(f.stem.split("_")[1])
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            meta = data.get("meta") or {}
            slot_name = str(meta.get("name") or "").strip().lower()
            if slot_name == key:
                return slot_id
        except Exception:
            pass
    return None


def list_teachings() -> list[dict]:
    """Return list of {slot_id, name, frames, duration_s} for all saved trajectories."""
    _ensure_dir()
    result = []
    for f in TEACHINGS_DIR.glob("slot_*.json"):
        try:
            slot_id = int(f.stem.split("_")[1])
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            frames = data.get("frames", [])
            duration = frames[-1]["t"] - frames[0]["t"] if len(frames) > 1 else 0
            meta = data.get("meta") or {}
            result.append(
                {
                    "slot_id": slot_id,
                    "name": str(meta.get("name") or "").strip(),
                    "frames": len(frames),
                    "duration_s": round(duration, 2),
                }
            )
        except Exception:
            pass
    return sorted(result, key=lambda x: x["slot_id"])
