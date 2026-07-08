"""
TeachingManager -- orchestrates arm recording and replay using G1ArmSDK.

States: idle -> recording -> idle
        idle -> replaying -> idle

Recording captures joint positions at 50Hz from rt/lowstate.
Replay interpolates between recorded frames at 50Hz via rt/arm_sdk.
"""

import math
import os
import threading
import time
from typing import Any, Optional

from talk_module import arm_sdk, teaching_store
from talk_module.pick_adjust import ReplayAdjustments, apply_replay_adjustments

_LEFT_ARM_SLICE = slice(3, 10)


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or ("1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _finalize_pose(
    q: list[float],
    adjustments: Optional[ReplayAdjustments],
    initial_q: Optional[list[float]],
    *,
    pick_flow: bool,
) -> list[float]:
    out = apply_replay_adjustments(q, adjustments)
    freeze_left = _env_bool("G1_PICK_FREEZE_LEFT_ARM", False)
    if pick_flow and initial_q and freeze_left and not _env_bool("G1_PICK_TWO_HANDS", True):
        for j in range(_LEFT_ARM_SLICE.start, min(_LEFT_ARM_SLICE.stop, len(out))):
            if j < len(initial_q):
                out[j] = initial_q[j]
    return out


class TeachingState:
    IDLE = "idle"
    RECORDING = "recording"
    REPLAYING = "replaying"


class TeachingManager:
    def __init__(self):
        self._state = TeachingState.IDLE
        self._lock = threading.Lock()
        self._sdk: Optional[arm_sdk.G1ArmSDK] = None

        self._rec_thread: Optional[threading.Thread] = None
        self._rec_frames: list[dict] = []
        self._rec_start_t: float = 0.0

        self._replay_thread: Optional[threading.Thread] = None

    @property
    def state(self) -> str:
        return self._state

    @property
    def recording_duration(self) -> float:
        if self._state == TeachingState.RECORDING:
            return time.time() - self._rec_start_t
        return 0.0

    @property
    def recorded_frame_count(self) -> int:
        return len(self._rec_frames)

    # ── Recording ──

    def start_recording(self) -> dict:
        with self._lock:
            if self._state != TeachingState.IDLE:
                return {"ok": False, "error": f"Impossibile registrare durante {self._state}"}
            self._state = TeachingState.RECORDING

        try:
            self._sdk = arm_sdk.G1ArmSDK()
            self._sdk.start(mode="passive")
        except Exception as e:
            self._state = TeachingState.IDLE
            return {"ok": False, "error": f"Errore init SDK: {e}"}

        self._rec_frames = []
        self._rec_start_t = time.time()
        self._rec_thread = threading.Thread(target=self._recording_loop, daemon=True)
        self._rec_thread.start()
        return {"ok": True}

    def _recording_loop(self):
        dt = arm_sdk.CONTROL_DT
        while self._state == TeachingState.RECORDING:
            q = self._sdk.get_joint_positions()
            if q is not None:
                t_rel = time.time() - self._rec_start_t
                self._rec_frames.append({"t": round(t_rel, 4), "q": [round(v, 5) for v in q]})
            time.sleep(dt)

    def stop_recording(self) -> dict:
        with self._lock:
            if self._state != TeachingState.RECORDING:
                return {"ok": False, "error": "not recording"}
            self._state = TeachingState.IDLE

        if self._rec_thread:
            self._rec_thread.join(timeout=2.0)
            self._rec_thread = None

        if self._sdk:
            self._sdk.stop()

        n = len(self._rec_frames)
        if n < 2:
            return {"ok": False, "error": f"Registrazione troppo corta ({n} frame)"}

        data = {
            "meta": {
                "frames": n,
                "duration_s": round(self._rec_frames[-1]["t"], 2),
                "hz": 50,
                "joints": len(arm_sdk.ALL_CONTROLLED),
            },
            "frames": self._rec_frames,
        }
        teaching_store.save_temp(data)
        return {"ok": True, "frames": n, "duration_s": data["meta"]["duration_s"]}

    # ── Replay ──

    def replay_temp(self) -> dict:
        return self._replay(teaching_store.load_temp())

    def replay_slot(
        self,
        slot_id: int,
        adjustments: Optional[ReplayAdjustments] = None,
        *,
        grasp_after: bool = False,
    ) -> dict:
        return self._replay(teaching_store.load_trajectory(slot_id), adjustments, grasp_after=grasp_after)

    def _replay(
        self,
        data: Optional[dict],
        adjustments: Optional[ReplayAdjustments] = None,
        *,
        grasp_after: bool = False,
    ) -> dict:
        if data is None:
            return {"ok": False, "error": "Nessun dato traiettoria"}

        with self._lock:
            if self._state != TeachingState.IDLE:
                return {"ok": False, "error": f"Impossibile riprodurre durante {self._state}"}
            self._state = TeachingState.REPLAYING

        frames = data["frames"]
        pick_flow = grasp_after or adjustments is not None
        self._replay_thread = threading.Thread(
            target=self._replay_worker,
            args=(frames, adjustments, grasp_after, pick_flow),
            daemon=True,
        )
        self._replay_thread.start()
        return {"ok": True, "frames": len(frames), "duration_s": data["meta"]["duration_s"]}

    def _replay_worker(
        self,
        frames: list[dict],
        adjustments: Optional[ReplayAdjustments] = None,
        grasp_after: bool = False,
        pick_flow: bool = False,
    ):
        try:
            sdk = arm_sdk.G1ArmSDK()
            self._sdk = sdk
            sdk.start(mode="active")

            initial_q = sdk.get_joint_positions()

            first_q = _finalize_pose(frames[0]["q"], adjustments, initial_q, pick_flow=pick_flow)
            self._cosine_move(sdk, first_q, duration=2.0)

            dt = arm_sdk.CONTROL_DT
            t0 = time.time()
            total_dur = frames[-1]["t"]
            fi = 0

            while self._state == TeachingState.REPLAYING:
                t_now = time.time() - t0
                if t_now >= total_dur:
                    break

                while fi < len(frames) - 1 and frames[fi + 1]["t"] <= t_now:
                    fi += 1

                if fi >= len(frames) - 1:
                    sdk.set_targets(
                        _finalize_pose(frames[-1]["q"], adjustments, initial_q, pick_flow=pick_flow)
                    )
                else:
                    f0 = frames[fi]
                    f1 = frames[fi + 1]
                    dt_f = f1["t"] - f0["t"]
                    alpha = (t_now - f0["t"]) / dt_f if dt_f > 0 else 1.0
                    alpha = max(0.0, min(1.0, alpha))
                    interp = [
                        f0["q"][j] + alpha * (f1["q"][j] - f0["q"][j])
                        for j in range(len(f0["q"]))
                    ]
                    sdk.set_targets(
                        _finalize_pose(interp, adjustments, initial_q, pick_flow=pick_flow)
                    )

                time.sleep(dt)

            if grasp_after:
                try:
                    from talk_module.hand_grasp import grasp_close_and_hold

                    grasp_close_and_hold()
                except Exception as ge:
                    print(f"[Teaching] grasp: {ge}", flush=True)

            if initial_q:
                self._cosine_move(sdk, initial_q, duration=2.0)

            if grasp_after:
                try:
                    from talk_module.hand_grasp import grasp_open_if_configured

                    grasp_open_if_configured()
                except Exception as ge:
                    print(f"[Teaching] grasp open: {ge}", flush=True)

            sdk.stop()

        except Exception as e:
            print(f"[Teaching] replay error: {e}", flush=True)
        finally:
            self._state = TeachingState.IDLE
            self._sdk = None

    def _cosine_move(self, sdk: arm_sdk.G1ArmSDK, target_q: list[float], duration: float = 2.0):
        """Smooth cosine interpolation from current position to target."""
        start_q = sdk.get_joint_positions()
        if start_q is None:
            return
        steps = int(duration / arm_sdk.CONTROL_DT)
        for i in range(steps):
            alpha = 0.5 * (1 - math.cos(math.pi * (i + 1) / steps))
            blended = [s + alpha * (t - s) for s, t in zip(start_q, target_q)]
            sdk.set_targets(blended)
            time.sleep(arm_sdk.CONTROL_DT)

    # ── Save to slot ──

    def save_to_slot(self, slot_id: int, name: str = "") -> dict:
        data = teaching_store.load_temp()
        if data is None:
            return {"ok": False, "error": "Nessuna registrazione temporanea da salvare"}
        label = (name or "").strip()
        if label:
            data.setdefault("meta", {})["name"] = label
        teaching_store.save_trajectory(slot_id, data)
        return {"ok": True, "slot_id": slot_id, "name": label or None}

    # ── Stop any ongoing operation ──

    def emergency_stop(self) -> dict:
        prev_state = self._state
        self._state = TeachingState.IDLE
        if self._sdk:
            try:
                self._sdk.stop()
            except Exception:
                pass
        if self._rec_thread:
            self._rec_thread.join(timeout=2.0)
            self._rec_thread = None
        if self._replay_thread:
            self._replay_thread.join(timeout=3.0)
            self._replay_thread = None
        self._sdk = None
        return {"ok": True, "was": prev_state}

    def get_status(self) -> dict:
        result = {"state": self._state}
        if self._state == TeachingState.RECORDING:
            result["duration_s"] = round(self.recording_duration, 1)
            result["frames"] = self.recorded_frame_count
        if self._sdk:
            full = self._sdk.get_full_controlled_state()
            if full:
                result["joints"] = full
        return result
