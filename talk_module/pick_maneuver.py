"""Manovra di presa «safe reach»: evita che la mano strisci sul tavolo (fasi alzata → indietro → avanti)."""

from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from talk_module import arm_sdk
from talk_module.pick_adjust import ReplayAdjustments, apply_replay_adjustments

# Indici in arm_sdk.ALL_CONTROLLED (vita + braccio destro)
_R_SP, _R_SR, _R_SY, _R_E, _R_WR, _R_WP, _R_WY = 10, 11, 12, 13, 14, 15, 16

_maneuver_lock = threading.Lock()
_maneuver_running = False


@dataclass(frozen=True)
class ManeuverPhase:
    name: str
    deltas: dict[int, float]
    duration: float = 1.1
    hold_s: float = 0.0
    use_vision: bool = False


def _default_phases() -> list[ManeuverPhase]:
    """Sequenza approccio tavolo: alza avambraccio, tira gomito indietro, forearm su, poi estendi."""
    return [
        ManeuverPhase(
            "lift_forearm",
            {_R_WP: 0.28, _R_E: -0.20, _R_SP: -0.06},
            duration=1.0,
        ),
        ManeuverPhase(
            "elbow_back",
            {_R_E: -0.50, _R_SP: -0.10, _R_SR: 0.04},
            duration=1.1,
        ),
        ManeuverPhase(
            "forearm_up",
            {_R_WP: 0.45, _R_E: -0.15, _R_SP: -0.04},
            duration=1.1,
        ),
        ManeuverPhase(
            "reach_forward",
            {_R_E: 0.62, _R_SP: 0.26},
            duration=1.35,
            use_vision=True,
        ),
        ManeuverPhase(
            "touch",
            {_R_E: 0.14, _R_SP: 0.10},
            duration=0.85,
            hold_s=0.3,
            use_vision=True,
        ),
    ]


def _load_phases() -> list[ManeuverPhase]:
    path = Path(__file__).resolve().parent.parent / "config" / "pick_maneuver.json"
    if not path.is_file():
        return _default_phases()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        out: list[ManeuverPhase] = []
        for p in raw.get("phases") or []:
            deltas = {int(k): float(v) for k, v in (p.get("deltas") or {}).items()}
            out.append(
                ManeuverPhase(
                    name=str(p.get("name") or "phase"),
                    deltas=deltas,
                    duration=float(p.get("duration") or 1.1),
                    hold_s=float(p.get("hold_s") or 0),
                    use_vision=bool(p.get("use_vision")),
                )
            )
        return out or _default_phases()
    except Exception as e:
        print(f"[pick_maneuver] config load failed: {e}", flush=True)
        return _default_phases()


def is_maneuver_running() -> bool:
    with _maneuver_lock:
        return _maneuver_running


def _cosine_move(sdk: arm_sdk.G1ArmSDK, target_q: list[float], duration: float) -> None:
    start_q = sdk.get_joint_positions()
    if start_q is None:
        return
    steps = max(1, int(duration / arm_sdk.CONTROL_DT))
    for i in range(steps):
        alpha = 0.5 * (1.0 - math.cos(math.pi * (i + 1) / steps))
        blended = [s + alpha * (t - s) for s, t in zip(start_q, target_q)]
        sdk.set_targets(blended)
        time.sleep(arm_sdk.CONTROL_DT)


def _maneuver_worker(adj: Optional[ReplayAdjustments]) -> dict[str, Any]:
    global _maneuver_running
    phases = _load_phases()
    sdk: Any = None
    try:
        sdk = arm_sdk.G1ArmSDK()
        sdk.start(mode="active")
        neutral = sdk.get_joint_positions()
        if neutral is None:
            return {"ok": False, "error": "Stato joint non disponibile"}

        pose = list(neutral)
        print("[pick_maneuver] approach start", flush=True)
        for ph in phases:
            for idx, delta in ph.deltas.items():
                if 0 <= idx < len(pose):
                    pose[idx] += delta
            target = apply_replay_adjustments(pose, adj if ph.use_vision else None)
            _cosine_move(sdk, target, ph.duration)
            if ph.hold_s > 0:
                time.sleep(ph.hold_s)

        grasp_hold = float((os.getenv("G1_PICK_GRASP_HOLD_SEC") or "0").strip() or "0")
        if grasp_hold > 0:
            time.sleep(min(grasp_hold, 3.0))

        print("[pick_maneuver] retract start", flush=True)
        for ph in reversed(phases):
            for idx, delta in ph.deltas.items():
                if 0 <= idx < len(pose):
                    pose[idx] -= delta
            target = apply_replay_adjustments(pose, adj if ph.use_vision else None)
            _cosine_move(sdk, target, ph.duration)

        _cosine_move(sdk, neutral, 1.2)
        sdk.stop()
        return {"ok": True, "mode": "safe_reach", "phases": len(phases)}
    except Exception as e:
        print(f"[pick_maneuver] error: {e}", flush=True)
        if sdk is not None:
            try:
                sdk.stop()
            except Exception:
                pass
        return {"ok": False, "error": str(e)}
    finally:
        with _maneuver_lock:
            _maneuver_running = False


def start_safe_reach(adjustments: Optional[ReplayAdjustments] = None) -> dict[str, Any]:
    """Avvia manovra in thread (non blocca la camera)."""
    global _maneuver_running
    with _maneuver_lock:
        if _maneuver_running:
            return {"ok": False, "error": "Manovra già in corso"}
        _maneuver_running = True

    from talk_module.teaching import TeachingState
    from talk_module.teaching_api import get_teaching_manager

    if get_teaching_manager().state != TeachingState.IDLE:
        with _maneuver_lock:
            _maneuver_running = False
        return {"ok": False, "error": f"Teaching occupato ({get_teaching_manager().state})"}

    t = threading.Thread(
        target=lambda: _maneuver_worker(adjustments),
        name="pick-safe-reach",
        daemon=True,
    )
    t.start()
    return {"ok": True, "mode": "safe_reach", "started": True}


def get_pick_mode() -> str:
    return (os.getenv("G1_PICK_MODE") or "teaching").strip().lower()
