"""
TeachingManager -- orchestrates arm recording and replay using G1ArmSDK.

States: idle -> recording -> idle
        idle -> replaying -> idle

Recording captures joint positions at 50Hz from rt/lowstate.
Replay interpolates between recorded frames at 50Hz via rt/arm_sdk.
"""

import math
import threading
import time
from typing import Optional

from talk_module import arm_sdk, teaching_store


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
                return {"ok": False, "error": f"cannot record while {self._state}"}
            self._state = TeachingState.RECORDING

        try:
            self._sdk = arm_sdk.G1ArmSDK()
            self._sdk.start(mode="passive")
        except Exception as e:
            self._state = TeachingState.IDLE
            return {"ok": False, "error": f"SDK init failed: {e}"}

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
            return {"ok": False, "error": f"recording too short ({n} frames)"}

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

    def replay_slot(self, slot_id: int) -> dict:
        return self._replay(teaching_store.load_trajectory(slot_id))

    def _replay(self, data: Optional[dict]) -> dict:
        if data is None:
            return {"ok": False, "error": "no trajectory data"}

        with self._lock:
            if self._state != TeachingState.IDLE:
                return {"ok": False, "error": f"cannot replay while {self._state}"}
            self._state = TeachingState.REPLAYING

        frames = data["frames"]
        self._replay_thread = threading.Thread(
            target=self._replay_worker, args=(frames,), daemon=True,
        )
        self._replay_thread.start()
        return {"ok": True, "frames": len(frames), "duration_s": data["meta"]["duration_s"]}

    def _replay_worker(self, frames: list[dict]):
        try:
            sdk = arm_sdk.G1ArmSDK()
            self._sdk = sdk
            sdk.start(mode="active")

            self._cosine_move(sdk, frames[0]["q"], duration=2.0)

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
                    sdk.set_targets(frames[-1]["q"])
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
                    sdk.set_targets(interp)

                time.sleep(dt)

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

    def save_to_slot(self, slot_id: int) -> dict:
        data = teaching_store.load_temp()
        if data is None:
            return {"ok": False, "error": "no temp recording to save"}
        teaching_store.save_trajectory(slot_id, data)
        return {"ok": True, "slot_id": slot_id}

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
