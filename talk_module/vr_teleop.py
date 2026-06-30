"""
VR Teleoperation manager for G1 -- receives hand tracking from Quest 3
via Hand Tracking Streamer (UDP), computes geometric IK, and drives
arm_sdk.G1ArmSDK to move the robot arms in real-time.

Flow:
  Quest 3 HTS app  -->  UDP :9000  -->  VRTeleopManager  -->  arm_sdk  -->  rt/arm_sdk

Coordinate conventions:
  HTS uses Unity left-handed coords. We convert to right-handed (Z-forward, Y-up, X-right)
  before IK. The "set neutral" calibration captures the rest pose so all control is relative.
"""

import math
import os
import socket
import threading
import time
from typing import Optional

from talk_module import arm_sdk

HTS_PORT = int(os.getenv("VR_HTS_PORT", "9000"))

# G1 approximate arm segment lengths (meters)
UPPER_ARM_LEN = 0.25
FOREARM_LEN = 0.25

# Joint limits for G1 29-DOF arms (radians, conservative)
ARM_JOINT_LIMITS = [
    (-2.87, 2.87),   # shoulder pitch
    (-1.55, 2.35),   # shoulder roll
    (-2.87, 2.87),   # shoulder yaw
    (-1.83, 0.09),   # elbow (negative = flexion for G1)
    (-1.57, 1.57),   # wrist roll
    (-0.52, 0.52),   # wrist pitch
    (-0.52, 0.52),   # wrist yaw
]

EMA_ALPHA = float(os.getenv("VR_EMA_ALPHA", "0.3"))
TRACKING_TIMEOUT = 0.5  # seconds without data before "tracking lost"


class VRState:
    IDLE = "idle"
    CALIBRATING = "calibrating"
    ACTIVE = "active"
    ERROR = "error"


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _ema(prev: float, cur: float, alpha: float) -> float:
    return prev + alpha * (cur - prev)


class WristPose:
    __slots__ = ("x", "y", "z", "qx", "qy", "qz", "qw")

    def __init__(self, x=0.0, y=0.0, z=0.0, qx=0.0, qy=0.0, qz=0.0, qw=1.0):
        self.x, self.y, self.z = x, y, z
        self.qx, self.qy, self.qz, self.qw = qx, qy, qz, qw

    def to_right_handed(self) -> "WristPose":
        """Convert from Unity left-handed (Y-up, Z-forward, X-right) to right-handed."""
        return WristPose(self.x, self.y, -self.z, -self.qx, -self.qy, self.qz, self.qw)

    def relative_to(self, origin: "WristPose") -> tuple[float, float, float]:
        """Position delta relative to an origin pose."""
        return (self.x - origin.x, self.y - origin.y, self.z - origin.z)


class VRTeleopManager:
    def __init__(self):
        self._state = VRState.IDLE
        self._lock = threading.Lock()
        self._sdk: Optional[arm_sdk.G1ArmSDK] = None

        self._udp_thread: Optional[threading.Thread] = None
        self._ctrl_thread: Optional[threading.Thread] = None
        self._running = False

        self._left_wrist = WristPose()
        self._right_wrist = WristPose()
        self._left_smooth = WristPose()
        self._right_smooth = WristPose()
        self._last_udp_time = 0.0

        self._neutral_left: Optional[WristPose] = None
        self._neutral_right: Optional[WristPose] = None
        self._neutral_joints: Optional[list[float]] = None

        self._tracking_left = False
        self._tracking_right = False

        self._udp_packet_count = 0
        self._udp_source_ip: Optional[str] = None
        self._udp_parse_errors = 0

    @property
    def state(self) -> str:
        return self._state

    @property
    def tracking_ok(self) -> bool:
        return (time.time() - self._last_udp_time) < TRACKING_TIMEOUT if self._last_udp_time > 0 else False

    # ── Start / Stop ──

    def start(self) -> dict:
        with self._lock:
            if self._state not in (VRState.IDLE, VRState.ERROR):
                return {"ok": False, "error": f"cannot start from state {self._state}"}
            self._state = VRState.CALIBRATING

        try:
            self._sdk = arm_sdk.G1ArmSDK()
            self._sdk.start(mode="active")
        except Exception as e:
            self._state = VRState.ERROR
            return {"ok": False, "error": f"Errore init SDK: {e}"}

        self._neutral_joints = self._sdk.get_joint_positions()
        self._running = True
        self._udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
        self._udp_thread.start()

        print(f"[VR Teleop] started, waiting for calibration (HTS port {HTS_PORT})", flush=True)
        return {"ok": True, "state": self._state, "port": HTS_PORT}

    def calibrate(self) -> dict:
        if self._state != VRState.CALIBRATING and self._state != VRState.ACTIVE:
            return {"ok": False, "error": f"Impossibile calibrare nello stato {self._state}"}
        if not self.tracking_ok:
            return {"ok": False, "error": "Nessun dato hand tracking. Apri HTS sul Quest, inserisci IP Jetson (es. 192.168.123.161) e porta 9000, poi premi Start in HTS."}

        self._neutral_left = WristPose(
            self._left_smooth.x, self._left_smooth.y, self._left_smooth.z,
            self._left_smooth.qx, self._left_smooth.qy, self._left_smooth.qz, self._left_smooth.qw,
        )
        self._neutral_right = WristPose(
            self._right_smooth.x, self._right_smooth.y, self._right_smooth.z,
            self._right_smooth.qx, self._right_smooth.qy, self._right_smooth.qz, self._right_smooth.qw,
        )
        self._neutral_joints = self._sdk.get_joint_positions()

        if self._state == VRState.CALIBRATING:
            self._state = VRState.ACTIVE
            self._ctrl_thread = threading.Thread(target=self._control_loop, daemon=True)
            self._ctrl_thread.start()

        print("[VR Teleop] calibrated -- control active", flush=True)
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        prev = self._state
        self._running = False
        self._state = VRState.IDLE

        if self._udp_thread:
            self._udp_thread.join(timeout=2.0)
            self._udp_thread = None
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=3.0)
            self._ctrl_thread = None
        if self._sdk:
            try:
                self._sdk.stop()
            except Exception:
                pass
            self._sdk = None

        self._neutral_left = None
        self._neutral_right = None
        print(f"[VR Teleop] stopped (was {prev})", flush=True)
        return {"ok": True}

    def emergency_stop(self) -> dict:
        self._running = False
        self._state = VRState.IDLE
        if self._sdk:
            try:
                self._sdk.stop()
            except Exception:
                pass
            self._sdk = None
        return {"ok": True}

    # ── UDP receiver ──

    def _udp_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", HTS_PORT))
        sock.settimeout(1.0)
        print(f"[VR Teleop] UDP listening on :{HTS_PORT}", flush=True)

        buf = ""
        while self._running:
            try:
                data, addr = sock.recvfrom(65535)
                self._udp_source_ip = addr[0]
                self._udp_packet_count += 1
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    self._parse_hts_line(line)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[VR Teleop] UDP error: {e}", flush=True)
                time.sleep(0.1)

        sock.close()

    def _parse_hts_line(self, line: str):
        """Parse a single line from Hand Tracking Streamer.
        Format: 'Left wrist: x,y,z,qx,qy,qz,qw' or 'Right wrist: ...'
        """
        try:
            if line.startswith("Left wrist:"):
                vals = [float(v) for v in line[len("Left wrist:"):].strip().split(",")]
                if len(vals) >= 7:
                    raw = WristPose(*vals[:7]).to_right_handed()
                    self._left_smooth = WristPose(
                        _ema(self._left_smooth.x, raw.x, EMA_ALPHA),
                        _ema(self._left_smooth.y, raw.y, EMA_ALPHA),
                        _ema(self._left_smooth.z, raw.z, EMA_ALPHA),
                        raw.qx, raw.qy, raw.qz, raw.qw,
                    )
                    self._left_wrist = raw
                    self._tracking_left = True
                    self._last_udp_time = time.time()

            elif line.startswith("Right wrist:"):
                vals = [float(v) for v in line[len("Right wrist:"):].strip().split(",")]
                if len(vals) >= 7:
                    raw = WristPose(*vals[:7]).to_right_handed()
                    self._right_smooth = WristPose(
                        _ema(self._right_smooth.x, raw.x, EMA_ALPHA),
                        _ema(self._right_smooth.y, raw.y, EMA_ALPHA),
                        _ema(self._right_smooth.z, raw.z, EMA_ALPHA),
                        raw.qx, raw.qy, raw.qz, raw.qw,
                    )
                    self._right_wrist = raw
                    self._tracking_right = True
                    self._last_udp_time = time.time()
        except Exception:
            self._udp_parse_errors += 1

    # ── Control loop ──

    def _control_loop(self):
        dt = arm_sdk.CONTROL_DT
        while self._running and self._state == VRState.ACTIVE:
            t0 = time.time()

            if not self.tracking_ok:
                time.sleep(dt)
                continue

            if self._neutral_left is None or self._neutral_joints is None:
                time.sleep(dt)
                continue

            left_delta = self._left_smooth.relative_to(self._neutral_left)
            right_delta = self._right_smooth.relative_to(self._neutral_right)

            left_q = self._geometric_ik(left_delta, self._left_smooth, side="left")
            right_q = self._geometric_ik(right_delta, self._right_smooth, side="right")

            base = list(self._neutral_joints)
            # Waist stays at neutral (indices 0-2 in ALL_CONTROLLED are waist)
            for i in range(3):
                base[i] = self._neutral_joints[i]
            try:
                from talk_module.robot_actions import left_arm_disabled
                freeze_left = left_arm_disabled()
            except Exception:
                freeze_left = True
            # Left arm: indices 3-9 — congelato se SX guasto
            for i in range(7):
                if freeze_left:
                    base[3 + i] = self._neutral_joints[3 + i]
                else:
                    base[3 + i] = left_q[i]
            # Right arm: indices 10-16 in ALL_CONTROLLED
            for i in range(7):
                base[10 + i] = right_q[i]

            self._sdk.set_targets(base)

            elapsed = time.time() - t0
            time.sleep(max(0.0, dt - elapsed))

    def _geometric_ik(
        self,
        delta: tuple[float, float, float],
        wrist: WristPose,
        side: str,
    ) -> list[float]:
        """Geometric IK: map wrist position delta to 7 joint angles.

        Uses 2-link arm model (upper arm + forearm) with shoulder-elbow coupling.
        Right-handed coords after conversion: X=right, Y=up, Z=forward.
        """
        dx, dy, dz = delta
        sign = 1.0 if side == "right" else -1.0

        neutral_j = self._neutral_joints
        if neutral_j is None:
            return [0.0] * 7

        arm_offset = 3 if side == "left" else 10

        sp_base = neutral_j[arm_offset + 0]
        sr_base = neutral_j[arm_offset + 1]
        sy_base = neutral_j[arm_offset + 2]
        el_base = neutral_j[arm_offset + 3]

        reach = math.sqrt(dx * dx + dy * dy + dz * dz)
        max_reach = UPPER_ARM_LEN + FOREARM_LEN
        reach_ratio = min(reach / max_reach, 1.0) if max_reach > 0 else 0.0

        shoulder_pitch = sp_base - dy * 2.0 + dz * 1.8
        shoulder_roll = sr_base + dx * sign * 2.5
        shoulder_yaw = sy_base + dx * sign * 0.6

        elbow = el_base - reach_ratio * 1.2 - dy * 1.5
        if dy > 0:
            shoulder_pitch -= dy * 0.8

        qx, qy, qz, qw = wrist.qx, wrist.qy, wrist.qz, wrist.qw
        wrist_roll = math.atan2(2.0 * (qw * qx + qy * qz), 1.0 - 2.0 * (qx * qx + qy * qy))
        wrist_pitch = math.asin(_clamp(2.0 * (qw * qy - qz * qx), -1.0, 1.0))
        wrist_yaw = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))

        wr_base = neutral_j[arm_offset + 4]
        wp_base = neutral_j[arm_offset + 5]
        wy_base = neutral_j[arm_offset + 6]

        if self._neutral_left is not None:
            nw = self._neutral_left if side == "left" else self._neutral_right
            n_qx, n_qy, n_qz, n_qw = nw.qx, nw.qy, nw.qz, nw.qw
            n_wr = math.atan2(2.0 * (n_qw * n_qx + n_qy * n_qz), 1.0 - 2.0 * (n_qx * n_qx + n_qy * n_qy))
            n_wp = math.asin(_clamp(2.0 * (n_qw * n_qy - n_qz * n_qx), -1.0, 1.0))
            n_wy = math.atan2(2.0 * (n_qw * n_qz + n_qx * n_qy), 1.0 - 2.0 * (n_qy * n_qy + n_qz * n_qz))
            wrist_roll_out = wr_base + (wrist_roll - n_wr) * 0.6
            wrist_pitch_out = wp_base + (wrist_pitch - n_wp) * 0.5
            wrist_yaw_out = wy_base + (wrist_yaw - n_wy) * 0.5
        else:
            wrist_roll_out = wr_base
            wrist_pitch_out = wp_base
            wrist_yaw_out = wy_base

        joints = [
            shoulder_pitch,
            shoulder_roll,
            shoulder_yaw,
            elbow,
            wrist_roll_out,
            wrist_pitch_out,
            wrist_yaw_out,
        ]

        for i in range(7):
            lo, hi = ARM_JOINT_LIMITS[i]
            joints[i] = _clamp(joints[i], lo, hi)

        return joints

    # ── Status ──

    def get_status(self) -> dict:
        try:
            from talk_module.robot_actions import left_arm_disabled
            _left_off = left_arm_disabled()
        except Exception:
            _left_off = True
        result = {
            "state": self._state,
            "tracking": self.tracking_ok,
            "tracking_left": self._tracking_left,
            "tracking_right": self._tracking_right,
            "disable_left_arm": _left_off,
            "port": HTS_PORT,
            "udp_packets": self._udp_packet_count,
            "udp_source": self._udp_source_ip,
            "udp_errors": self._udp_parse_errors,
        }
        if self._last_udp_time > 0:
            result["udp_age_ms"] = round((time.time() - self._last_udp_time) * 1000)
        if self.tracking_ok:
            result["left_wrist"] = [
                round(self._left_smooth.x, 3),
                round(self._left_smooth.y, 3),
                round(self._left_smooth.z, 3),
            ]
            result["right_wrist"] = [
                round(self._right_smooth.x, 3),
                round(self._right_smooth.y, 3),
                round(self._right_smooth.z, 3),
            ]
        if self._sdk:
            full = self._sdk.get_full_controlled_state()
            if full:
                result["joints"] = full
        return result
