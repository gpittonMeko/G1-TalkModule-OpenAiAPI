"""
G1 Arm SDK -- low-level DDS driver for rt/arm_sdk (G1_29 DOF).

Controls ONLY arm joints (15-28) and waist joints (12-14).
Legs (0-11) are NEVER touched -- the locomotion policy stays in full control.

Key requirements (verified against xr_teleoperate + official C++ example):
  - CRC32 on every published LowCmd_ message
  - mode_pr = 0, mode_machine from rt/lowstate, motor_cmd[i].mode = 1
  - Weight parameter at motor_cmd[29].q  (kNotUsedJoint0, NOT 54)
  - DDS initialized once per process via robot_actions._ensure_dds_init()
"""

import threading
import time
from enum import IntEnum
from typing import Optional

_arm_sdk_active = False
_arm_sdk_instance: Optional["G1ArmSDK"] = None
_instance_lock = threading.Lock()


class G1_29_Joint(IntEnum):
    kLeftHipPitch = 0
    kLeftHipRoll = 1
    kLeftHipYaw = 2
    kLeftKnee = 3
    kLeftAnklePitch = 4
    kLeftAnkleRoll = 5
    kRightHipPitch = 6
    kRightHipRoll = 7
    kRightHipYaw = 8
    kRightKnee = 9
    kRightAnklePitch = 10
    kRightAnkleRoll = 11
    kWaistYaw = 12
    kWaistRoll = 13
    kWaistPitch = 14
    kLeftShoulderPitch = 15
    kLeftShoulderRoll = 16
    kLeftShoulderYaw = 17
    kLeftElbow = 18
    kLeftWristRoll = 19
    kLeftWristPitch = 20
    kLeftWristYaw = 21
    kRightShoulderPitch = 22
    kRightShoulderRoll = 23
    kRightShoulderYaw = 24
    kRightElbow = 25
    kRightWristRoll = 26
    kRightWristPitch = 27
    kRightWristYaw = 28
    kNotUsedJoint0 = 29


WAIST_INDICES = [12, 13, 14]
LEFT_ARM_INDICES = list(range(15, 22))
RIGHT_ARM_INDICES = list(range(22, 29))
ARM_INDICES = LEFT_ARM_INDICES + RIGHT_ARM_INDICES
ALL_CONTROLLED = WAIST_INDICES + ARM_INDICES  # 17 joints
WEIGHT_INDEX = 29
NUM_MOTORS = 35
CONTROL_DT = 0.02  # 50 Hz


def _safe_crc_init():
    """Init CRC with fallback to pure-Python if the native .so is missing."""
    from unitree_sdk2py.utils.crc import CRC
    try:
        crc = CRC()
        print("[ArmSDK] CRC initialized (native)", flush=True)
        return crc
    except OSError as e:
        print(f"[ArmSDK] WARNING: native CRC failed ({e}), using pure-Python fallback", flush=True)
        crc = CRC.__new__(CRC)
        crc.platform = "fallback"
        crc._CRC__packFmtHGLowCmd = '<2B2x' + 'B3x5fI' * 35 + '5I'
        crc._CRC__packFmtHGLowState = '<2I2B2xI' + '13fh2x' + 'B3x4f2hf7I' * 35 + '40B5I'
        crc._CRC__packFmtLowCmd = '<4B4IH2x' + 'B3x5f3I' * 20 + '4B' + '55Bx2I'
        crc._CRC__packFmtLowState = '<4B4IH2x' + '13fb3x' + 'B3x7fb3x3I' * 20 + '4BiH4b15H' + '8hI41B3xf2b2x2f4h2I'
        return crc


def is_arm_sdk_active() -> bool:
    return _arm_sdk_active


def get_instance() -> Optional["G1ArmSDK"]:
    return _arm_sdk_instance


class G1ArmSDK:
    """Pure DDS driver for G1 arm control via rt/arm_sdk.

    Lifecycle:
      sdk = G1ArmSDK()   # inits DDS, publisher, subscriber
      sdk.start(mode)     # starts 50Hz publish thread, sets _arm_sdk_active
      sdk.stop()          # ramps weight down, stops thread, clears _arm_sdk_active
    """

    def __init__(self):
        global _arm_sdk_instance
        from talk_module.robot_actions import _ensure_dds_init
        _ensure_dds_init()

        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import (
            LowCmd_ as hg_LowCmd,
            LowState_ as hg_LowState,
        )
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber

        self._crc = _safe_crc_init()
        self._msg = unitree_hg_msg_dds__LowCmd_()
        self._msg.mode_pr = 0

        for idx in ALL_CONTROLLED:
            self._msg.motor_cmd[idx].mode = 1

        self._pub = ChannelPublisher("rt/arm_sdk", hg_LowCmd)
        self._pub.Init()

        self._sub = ChannelSubscriber("rt/lowstate", hg_LowState)
        self._sub.Init()

        self._state_lock = threading.Lock()
        self._latest_state = None
        self._sub_thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        self._sub_thread.start()

        self._ctrl_lock = threading.Lock()
        self._running = False
        self._publish_thread: Optional[threading.Thread] = None
        self._weight = 0.0
        self._target_q = [0.0] * len(ALL_CONTROLLED)
        self._target_kp = [60.0] * len(ALL_CONTROLLED)
        self._target_kd = [1.5] * len(ALL_CONTROLLED)

        self._wait_for_state(timeout=5.0)
        self._msg.mode_machine = self._read_mode_machine()

        with _instance_lock:
            _arm_sdk_instance = self

        print("[ArmSDK] initialized, mode_machine =", self._msg.mode_machine, flush=True)

    def _subscribe_loop(self):
        while True:
            msg = self._sub.Read()
            if msg is not None:
                with self._state_lock:
                    self._latest_state = msg
            time.sleep(0.002)

    def _wait_for_state(self, timeout: float = 5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._state_lock:
                if self._latest_state is not None:
                    return
            time.sleep(0.1)
            print("[ArmSDK] waiting for rt/lowstate...", flush=True)
        raise RuntimeError("[ArmSDK] timeout waiting for rt/lowstate -- check DDS interface and robot power")

    def _read_mode_machine(self) -> int:
        with self._state_lock:
            if self._latest_state is not None:
                return self._latest_state.mode_machine
        return 0

    def get_joint_positions(self) -> Optional[list[float]]:
        """Return current positions for ALL_CONTROLLED joints (17 values), or None."""
        with self._state_lock:
            s = self._latest_state
        if s is None:
            return None
        return [s.motor_state[idx].q for idx in ALL_CONTROLLED]

    def get_arm_positions(self) -> Optional[list[float]]:
        """Return current positions for arm joints only (14 values), or None."""
        with self._state_lock:
            s = self._latest_state
        if s is None:
            return None
        return [s.motor_state[idx].q for idx in ARM_INDICES]

    def get_full_controlled_state(self) -> Optional[dict]:
        """Return dict with waist/left_arm/right_arm position arrays for UI streaming."""
        with self._state_lock:
            s = self._latest_state
        if s is None:
            return None
        return {
            "waist": [s.motor_state[idx].q for idx in WAIST_INDICES],
            "left_arm": [s.motor_state[idx].q for idx in LEFT_ARM_INDICES],
            "right_arm": [s.motor_state[idx].q for idx in RIGHT_ARM_INDICES],
        }

    # ── start / stop ──

    def start(self, mode: str = "active"):
        """Start the 50Hz publish loop.

        mode:
          'active'  -- kp=60, kd=1.5 for all controlled joints (replay)
          'passive' -- kp=0, kd=1.0 for arms; kp=60, kd=1.5 for waist (recording)
        """
        global _arm_sdk_active
        if self._running:
            return

        current_q = self.get_joint_positions()
        if current_q is None:
            raise RuntimeError("[ArmSDK] no joint state available")

        with self._ctrl_lock:
            self._target_q = list(current_q)
            if mode == "passive":
                kp_list = []
                kd_list = []
                for i, idx in enumerate(ALL_CONTROLLED):
                    if idx in WAIST_INDICES:
                        kp_list.append(60.0)
                        kd_list.append(1.5)
                    elif idx in LEFT_ARM_INDICES:
                        try:
                            from talk_module.config import settings
                            if settings.disable_left_arm:
                                kp_list.append(0.0)
                                kd_list.append(1.0)
                                continue
                        except Exception:
                            pass
                        kp_list.append(0.0)
                        kd_list.append(1.0)
                    else:
                        kp_list.append(0.0)
                        kd_list.append(1.0)
                self._target_kp = kp_list
                self._target_kd = kd_list
            else:
                kp_list = [60.0] * len(ALL_CONTROLLED)
                kd_list = [1.5] * len(ALL_CONTROLLED)
                try:
                    from talk_module.config import settings
                    if settings.disable_left_arm:
                        for i, idx in enumerate(ALL_CONTROLLED):
                            if idx in LEFT_ARM_INDICES:
                                kp_list[i] = 0.0
                                kd_list[i] = 1.0
                except Exception:
                    pass
                self._target_kp = kp_list
                self._target_kd = kd_list

        self._msg.mode_machine = self._read_mode_machine()
        self._running = True
        _arm_sdk_active = True

        self._engage_smooth(current_q, duration=2.0)

        self._publish_thread = threading.Thread(target=self._publish_loop, daemon=True)
        self._publish_thread.start()
        print(f"[ArmSDK] started mode={mode}", flush=True)

    def stop(self):
        """Ramp weight to 0 over 2s, then stop publish thread."""
        global _arm_sdk_active
        if not self._running:
            return

        self._disengage_smooth(duration=2.0)
        self._running = False
        _arm_sdk_active = False

        if self._publish_thread is not None:
            self._publish_thread.join(timeout=3.0)
            self._publish_thread = None
        print("[ArmSDK] stopped", flush=True)

    def set_targets(self, q_values: list[float]):
        """Set target joint positions for ALL_CONTROLLED (17 values).
        The publish loop will velocity-clamp towards these."""
        with self._ctrl_lock:
            if len(q_values) == len(ALL_CONTROLLED):
                merged = list(q_values)
                try:
                    from talk_module.config import settings
                    if settings.disable_left_arm:
                        current = self.get_joint_positions()
                        if current:
                            for i in range(7):
                                merged[3 + i] = current[3 + i]
                except Exception:
                    pass
                self._target_q = merged

    def set_gains(self, kp: list[float], kd: list[float]):
        """Update PD gains for ALL_CONTROLLED joints."""
        with self._ctrl_lock:
            if len(kp) == len(ALL_CONTROLLED):
                self._target_kp = list(kp)
            if len(kd) == len(ALL_CONTROLLED):
                self._target_kd = list(kd)

    # ── internal ──

    def _engage_smooth(self, start_q: list[float], duration: float = 2.0):
        """Ramp weight 0→1 while holding current positions (smooth takeover)."""
        steps = int(duration / CONTROL_DT)
        for i in range(steps):
            self._weight = min(1.0, (i + 1) / steps)
            self._write_cmd(start_q)
            time.sleep(CONTROL_DT)
        self._weight = 1.0

    def _disengage_smooth(self, duration: float = 2.0):
        """Ramp weight 1→0 (smooth release)."""
        steps = int(duration / CONTROL_DT)
        for i in range(steps):
            self._weight = max(0.0, 1.0 - (i + 1) / steps)
            current_q = self.get_joint_positions()
            if current_q:
                self._write_cmd(current_q)
            time.sleep(CONTROL_DT)
        self._weight = 0.0
        self._write_cmd(self.get_joint_positions() or [0.0] * len(ALL_CONTROLLED))

    def _publish_loop(self):
        """Main 50Hz control loop with velocity clamping."""
        max_delta = 3.0 * CONTROL_DT  # 3.0 rad/s * 0.02s = 0.06 rad/cycle

        current_cmd_q = list(self._target_q)

        while self._running:
            t0 = time.time()

            with self._ctrl_lock:
                target_q = list(self._target_q)
                kp = list(self._target_kp)
                kd = list(self._target_kd)

            for i in range(len(ALL_CONTROLLED)):
                delta = target_q[i] - current_cmd_q[i]
                clamped = max(-max_delta, min(max_delta, delta))
                current_cmd_q[i] += clamped

            self._write_cmd(current_cmd_q, kp, kd)

            elapsed = time.time() - t0
            sleep_time = max(0.0, CONTROL_DT - elapsed)
            time.sleep(sleep_time)

    def _write_cmd(
        self,
        q_values: list[float],
        kp: Optional[list[float]] = None,
        kd: Optional[list[float]] = None,
    ):
        """Build and publish a single LowCmd_ message with CRC."""
        if kp is None:
            kp = self._target_kp
        if kd is None:
            kd = self._target_kd

        self._msg.motor_cmd[WEIGHT_INDEX].q = self._weight

        for i, idx in enumerate(ALL_CONTROLLED):
            self._msg.motor_cmd[idx].q = q_values[i]
            self._msg.motor_cmd[idx].dq = 0.0
            self._msg.motor_cmd[idx].kp = kp[i]
            self._msg.motor_cmd[idx].kd = kd[i]
            self._msg.motor_cmd[idx].tau = 0.0

        self._msg.crc = self._crc.Crc(self._msg)
        self._pub.Write(self._msg)

    def destroy(self):
        """Full cleanup."""
        global _arm_sdk_instance, _arm_sdk_active
        self.stop()
        with _instance_lock:
            if _arm_sdk_instance is self:
                _arm_sdk_instance = None
        _arm_sdk_active = False
