"""Calibrazione e offset replay teaching da bbox/depth camera."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

_CALIB_PATH = Path(__file__).resolve().parent.parent / "config" / "pick_calib.json"


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class ReplayAdjustments:
    """Offset (rad) su vita + braccio(i) durante replay pick."""

    waist_yaw: float = 0.0
    shoulder_pitch: float = 0.0
    shoulder_yaw: float = 0.0
    elbow: float = 0.0
    left_shoulder_pitch: float = 0.0
    left_shoulder_yaw: float = 0.0
    left_elbow: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


# Indici in arm_sdk.ALL_CONTROLLED (vita + braccio SX + braccio DX)
_IDX_WAIST_YAW = 0
_IDX_L_SHOULDER_PITCH = 3
_IDX_L_SHOULDER_YAW = 5
_IDX_L_ELBOW = 6
_IDX_R_SHOULDER_PITCH = 10
_IDX_R_SHOULDER_YAW = 12
_IDX_R_ELBOW = 13


def _two_hands() -> bool:
    return (os.getenv("G1_PICK_TWO_HANDS") or "1").strip().lower() in ("1", "true", "yes", "on")


def apply_replay_adjustments(q: list[float], adj: Optional[ReplayAdjustments]) -> list[float]:
    if not adj or not q:
        return list(q)
    out = list(q)
    n = len(out)
    if n > _IDX_WAIST_YAW:
        out[_IDX_WAIST_YAW] += adj.waist_yaw
    if n > _IDX_R_SHOULDER_PITCH:
        out[_IDX_R_SHOULDER_PITCH] += adj.shoulder_pitch
    if n > _IDX_R_SHOULDER_YAW:
        out[_IDX_R_SHOULDER_YAW] += adj.shoulder_yaw
    if n > _IDX_R_ELBOW:
        out[_IDX_R_ELBOW] += adj.elbow
    if _two_hands():
        if n > _IDX_L_SHOULDER_PITCH:
            out[_IDX_L_SHOULDER_PITCH] += adj.left_shoulder_pitch
        if n > _IDX_L_SHOULDER_YAW:
            out[_IDX_L_SHOULDER_YAW] += adj.left_shoulder_yaw
        if n > _IDX_L_ELBOW:
            out[_IDX_L_ELBOW] += adj.left_elbow
    return out


def bbox_center_u(bbox: list[int], image_width: int) -> float:
    x, _y, w, _h = bbox
    return (x + w / 2.0) / max(image_width, 1)


def compute_adjustments(
    detection: dict[str, Any],
    ref_u: float,
    ref_depth_m: float,
    image_width: int,
) -> ReplayAdjustments:
    bbox = detection.get("bbox")
    depth_m = detection.get("depth_m")
    if not bbox or depth_m is None:
        return ReplayAdjustments()

    u = bbox_center_u(bbox, image_width)
    du = u - ref_u
    try:
        depth = float(depth_m)
    except (TypeError, ValueError):
        return ReplayAdjustments()
    dd = depth - ref_depth_m

    max_off = _env_float("G1_PICK_MAX_OFFSET", 0.45)
    waist_g = _env_float("G1_PICK_WAIST_GAIN", 0.55)
    yaw_g = _env_float("G1_PICK_YAW_GAIN", 0.85)
    pitch_g = _env_float("G1_PICK_PITCH_GAIN", 0.40)
    elbow_g = _env_float("G1_PICK_ELBOW_GAIN", 0.20)
    left_yaw_g = _env_float("G1_PICK_LEFT_YAW_GAIN", yaw_g)

    pitch = _clamp(-dd * pitch_g, -max_off, max_off)
    elbow = _clamp(-dd * elbow_g, -max_off, max_off)
    r_yaw = _clamp(du * yaw_g, -max_off, max_off)
    l_yaw = _clamp(-du * left_yaw_g, -max_off, max_off)

    return ReplayAdjustments(
        waist_yaw=_clamp(du * waist_g, -max_off, max_off),
        shoulder_yaw=r_yaw,
        shoulder_pitch=pitch,
        elbow=elbow,
        left_shoulder_yaw=l_yaw,
        left_shoulder_pitch=pitch,
        left_elbow=elbow,
    )


def load_calib() -> dict[str, Any]:
    if _CALIB_PATH.is_file():
        try:
            return json.loads(_CALIB_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    ref_u = os.getenv("G1_PICK_REF_U")
    ref_d = os.getenv("G1_PICK_REF_DEPTH_M")
    out: dict[str, Any] = {}
    if ref_u:
        try:
            out["ref_bbox_u"] = float(ref_u)
        except ValueError:
            pass
    if ref_d:
        try:
            out["ref_depth_m"] = float(ref_d)
        except ValueError:
            pass
    return out


def save_calib(ref_bbox_u: float, ref_depth_m: float, *, class_name: str = "") -> None:
    _CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ref_bbox_u": round(ref_bbox_u, 4),
        "ref_depth_m": round(ref_depth_m, 4),
        "class": class_name,
    }
    _CALIB_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_image_width() -> int:
    try:
        return int((os.getenv("G1_CAMERA_WIDTH") or "640").strip())
    except ValueError:
        return 640
