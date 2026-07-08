"""Visione → replay teaching: quando YOLO vede un oggetto in fascia depth, avvia presa registrata."""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

from talk_module.pick_adjust import (
    ReplayAdjustments,
    bbox_center_u,
    compute_adjustments,
    get_image_width,
    load_calib,
    save_calib,
)

_lock = threading.Lock()
_service: Optional["PickOnDetectService"] = None


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default


class PickOnDetectService:
    def __init__(self) -> None:
        self._reload_config()
        self._stable_count = 0
        self._last_trigger_ts = 0.0
        self._trigger_count = 0
        self._last_detection: Optional[dict[str, Any]] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_adjustments: Optional[dict[str, float]] = None
        self.ref_bbox_u: Optional[float] = None
        self.ref_depth_m: Optional[float] = None
        self._load_reference()

    def _load_reference(self) -> None:
        cal = load_calib()
        self.ref_bbox_u = cal.get("ref_bbox_u")
        self.ref_depth_m = cal.get("ref_depth_m")

    def _reload_config(self) -> None:
        self.pick_mode = (os.getenv("G1_PICK_MODE") or "teaching").strip().lower()
        self.enabled = (os.getenv("G1_PICK_ENABLED", "0") or "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        raw = (os.getenv("G1_PICK_CLASS") or "bottle").strip()
        self.target_classes = {c.strip().lower() for c in raw.split(",") if c.strip()}
        self.depth_min = _env_float("G1_PICK_DEPTH_MIN", 0.35)
        self.depth_max = _env_float("G1_PICK_DEPTH_MAX", 0.75)
        self.teaching_slot = _env_int("G1_PICK_TEACHING_SLOT", 0)
        self.cooldown_sec = _env_float("G1_PICK_COOLDOWN_SEC", 20.0)
        self.stable_frames = max(1, _env_int("G1_PICK_STABLE_FRAMES", 4))
        self.two_hands = (os.getenv("G1_PICK_TWO_HANDS") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self.image_width = get_image_width()

    def set_enabled(self, enabled: bool) -> None:
        with _lock:
            self.enabled = enabled
            if not enabled:
                self._stable_count = 0

    def status(self) -> dict[str, Any]:
        with _lock:
            return {
                "enabled": self.enabled,
                "pick_mode": self.pick_mode,
                "two_hands": self.two_hands,
                "target_classes": sorted(self.target_classes),
                "depth_min_m": self.depth_min,
                "depth_max_m": self.depth_max,
                "teaching_slot": self.teaching_slot,
                "cooldown_sec": self.cooldown_sec,
                "stable_frames": self.stable_frames,
                "stable_count": self._stable_count,
                "trigger_count": self._trigger_count,
                "last_trigger_ts": self._last_trigger_ts or None,
                "last_detection": self._last_detection,
                "last_result": self._last_result,
                "last_error": self._last_error,
                "last_adjustments": self._last_adjustments,
                "ref_bbox_u": self.ref_bbox_u,
                "ref_depth_m": self.ref_depth_m,
                "calibrated": self.ref_bbox_u is not None and self.ref_depth_m is not None,
                "cooldown_remaining_s": max(
                    0.0, round(self.cooldown_sec - (time.time() - self._last_trigger_ts), 1)
                )
                if self._last_trigger_ts
                else 0.0,
            }

    def calibrate_from_detection(self, detection: dict[str, Any]) -> dict[str, Any]:
        bbox = detection.get("bbox")
        depth_m = detection.get("depth_m")
        if not bbox or depth_m is None:
            return {"ok": False, "error": "Serve bbox e depth_m dalla camera"}
        u = bbox_center_u(bbox, self.image_width)
        depth = float(depth_m)
        cls = str(detection.get("class", ""))
        save_calib(u, depth, class_name=cls)
        with _lock:
            self.ref_bbox_u = u
            self.ref_depth_m = depth
        return {
            "ok": True,
            "ref_bbox_u": round(u, 4),
            "ref_depth_m": round(depth, 4),
            "class": cls,
        }

    def calibrate_from_camera(self) -> dict[str, Any]:
        try:
            from talk_module.camera_yolo import get_camera_service

            dets = get_camera_service().status().get("detections") or []
        except Exception as e:
            return {"ok": False, "error": f"Camera: {e}"}
        best = self._best_match(dets)
        if not best:
            return {
                "ok": False,
                "error": "Nessun oggetto in fascia. Metti la bottiglia in vista e avvia la camera.",
            }
        return self.calibrate_from_detection(best)

    def on_detections(self, detections: list[dict[str, Any]]) -> None:
        with _lock:
            if not self.enabled:
                self._stable_count = 0
                return

            now = time.time()
            if self._last_trigger_ts and (now - self._last_trigger_ts) < self.cooldown_sec:
                return

            best = self._best_match(detections)
            if best is None:
                self._stable_count = 0
                return

            self._stable_count += 1
            if self._stable_count < self.stable_frames:
                return

            self._stable_count = 0
            slot = self.teaching_slot
            det = dict(best)
            ref_u = self.ref_bbox_u
            ref_d = self.ref_depth_m

        self._run_replay(slot, det, ref_u=ref_u, ref_depth_m=ref_d, pick_mode=self.pick_mode)

    def trigger_manual(self) -> dict[str, Any]:
        with _lock:
            slot = self.teaching_slot
            ref_u = self.ref_bbox_u
            ref_d = self.ref_depth_m
            det = self._last_detection
            mode = self.pick_mode
        return self._run_replay(slot, det, ref_u=ref_u, ref_depth_m=ref_d, force=True, pick_mode=mode)

    def _best_match(self, detections: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        best: Optional[dict[str, Any]] = None
        best_conf = -1.0
        for d in detections:
            cls = str(d.get("class", "")).strip().lower()
            if cls not in self.target_classes:
                continue
            dm = d.get("depth_m")
            if dm is None:
                continue
            try:
                depth = float(dm)
            except (TypeError, ValueError):
                continue
            if depth < self.depth_min or depth > self.depth_max:
                continue
            conf = float(d.get("confidence") or 0.0)
            if conf > best_conf:
                best_conf = conf
                best = d
        return best

    def _compute_adj(
        self,
        detection: Optional[dict[str, Any]],
        ref_u: Optional[float],
        ref_depth_m: Optional[float],
    ) -> Optional[ReplayAdjustments]:
        if not detection or ref_u is None or ref_depth_m is None:
            return None
        return compute_adjustments(detection, ref_u, ref_depth_m, self.image_width)

    def _run_replay(
        self,
        slot: int,
        detection: Optional[dict[str, Any]],
        *,
        ref_u: Optional[float] = None,
        ref_depth_m: Optional[float] = None,
        force: bool = False,
        pick_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        try:
            from talk_module.teaching import TeachingState
            from talk_module.teaching_api import get_teaching_manager

            mgr = get_teaching_manager()
            if mgr.state != TeachingState.IDLE:
                result = {"ok": False, "error": f"Teaching occupato ({mgr.state})"}
                with _lock:
                    self._last_error = result["error"]
                    self._last_result = result
                return result

            if not force:
                now = time.time()
                with _lock:
                    if self._last_trigger_ts and (now - self._last_trigger_ts) < self.cooldown_sec:
                        return {"ok": False, "error": "cooldown attivo"}

            adj = self._compute_adj(detection, ref_u, ref_depth_m)
            mode = (pick_mode or self.pick_mode).strip().lower()

            from talk_module.pick_maneuver import is_maneuver_running, start_safe_reach

            if is_maneuver_running():
                result = {"ok": False, "error": "Manovra già in corso"}
            elif mode in ("teaching", "replay", "slot"):
                result = mgr.replay_slot(slot, adjustments=adj, grasp_after=True)
            else:
                result = start_safe_reach(adj)
            with _lock:
                self._last_result = result
                self._last_adjustments = adj.as_dict() if adj else None
                if result.get("ok"):
                    self._last_trigger_ts = time.time()
                    self._trigger_count += 1
                    self._last_detection = detection
                    self._last_error = None
                    extra = ""
                    if detection:
                        extra = f" det={detection.get('class')} {detection.get('depth_m')}m"
                    if adj:
                        extra += f" adj={adj.as_dict()}"
                    print(f"[pick] replay slot {slot} ok{extra}", flush=True)
                else:
                    self._last_error = str(result.get("error") or "replay fallito")
                    print(f"[pick] replay slot {slot} FAIL: {self._last_error}", flush=True)
            return result
        except Exception as e:
            err = str(e)
            with _lock:
                self._last_error = err
                self._last_result = {"ok": False, "error": err}
            print(f"[pick] errore: {err}", flush=True)
            return {"ok": False, "error": err}


def get_pick_service() -> PickOnDetectService:
    global _service
    if _service is None:
        _service = PickOnDetectService()
    return _service
