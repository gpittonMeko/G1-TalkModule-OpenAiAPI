"""Chiusura mani G1 (Dex3 / Dex1) — destra, sinistra o entrambe."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal, Optional

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "hand_grasp.json"

Side = Literal["left", "right"]

_DEX1_OPEN_DEFAULT = 0.0
_DEX1_CLOSE_DEFAULT = 4.5
_DEX3_OPEN_DEFAULT = [0.0] * 7
_DEX3_CLOSE_DEFAULT = [0.75, 0.55, 0.35, 0.85, 0.45, 0.85, 0.45]

_DEX1_TOPICS = {
    "left": ("rt/dex1/left/cmd", "rt/dex1/left/state"),
    "right": ("rt/dex1/right/cmd", "rt/dex1/right/state"),
}
_DEX3_TOPICS = {
    "left": ("rt/dex3/left/cmd", "rt/dex3/left/state"),
    "right": ("rt/dex3/right/cmd", "rt/dex3/right/state"),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or ("1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def two_hands_enabled() -> bool:
    return _env_bool("G1_PICK_TWO_HANDS", True)


def _load_config() -> dict[str, Any]:
    if _CONFIG_PATH.is_file():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[hand_grasp] config load failed: {e}", flush=True)
    return {}


def _hand_type() -> str:
    cfg = _load_config()
    return (os.getenv("G1_HAND_TYPE") or cfg.get("hand_type") or "auto").strip().lower()


def _side_cfg(side: Side) -> dict[str, Any]:
    cfg = _load_config()
    block = dict(cfg.get(side) or {})
    if side == "left" and not block:
        block = dict(cfg.get("right") or {})
    return block


def _resolve_type() -> Optional[str]:
    t = _hand_type()
    if t in ("dex3", "dex1", "none", "off"):
        return None if t in ("none", "off") else t
    try:
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import HandCmd_  # noqa: F401

        return "dex3"
    except Exception:
        pass
    try:
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_  # noqa: F401

        return "dex1"
    except Exception:
        return None


def _ensure_dds() -> None:
    from talk_module.robot_actions import _ensure_dds_init

    _ensure_dds_init()


def _ramp_dex1(side: Side, target_q: float, duration: float = 0.45) -> bool:
    try:
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__MotorCmd_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import MotorCmds_, MotorStates_
    except ImportError as e:
        print(f"[hand_grasp] dex1 SDK missing: {e}", flush=True)
        return False

    sc = _side_cfg(side)
    kp = float(sc.get("dex1_kp") or 5.0)
    kd = float(sc.get("dex1_kd") or 0.05)
    cmd_topic, state_topic = _DEX1_TOPICS[side]
    _ensure_dds()
    pub = ChannelPublisher(cmd_topic, MotorCmds_)
    pub.Init()

    start_q = 0.0
    try:
        sub = ChannelSubscriber(state_topic, MotorStates_)
        sub.Init()
        t0 = time.time()
        while time.time() - t0 < 1.0:
            st = sub.Read()
            if st is not None and st.states:
                start_q = float(st.states[0].q)
                break
            time.sleep(0.02)
    except Exception:
        pass

    steps = max(1, int(duration / 0.02))
    for i in range(steps):
        alpha = (i + 1) / steps
        q = start_q + alpha * (target_q - start_q)
        msg = MotorCmds_()
        cmd = unitree_go_msg_dds__MotorCmd_()
        cmd.q = q
        cmd.dq = 0.0
        cmd.tau = 0.0
        cmd.kp = kp
        cmd.kd = kd
        msg.cmds = [cmd]
        pub.Write(msg)
        time.sleep(0.02)
    return True


def _ramp_dex3(side: Side, target_q: list[float], duration: float = 0.45) -> bool:
    try:
        from unitree_sdk2py.core.channel import ChannelPublisher, ChannelSubscriber
        from unitree_sdk2py.idl.default import unitree_hg_msg_dds__HandCmd_
        from unitree_sdk2py.idl.unitree_hg.msg.dds_ import HandCmd_, HandState_
    except ImportError as e:
        print(f"[hand_grasp] dex3 SDK missing: {e}", flush=True)
        return False

    sc = _side_cfg(side)
    kp = float(sc.get("dex3_kp") or 1.5)
    kd = float(sc.get("dex3_kd") or 0.2)
    n = 7
    tq = (list(target_q) + [0.0] * n)[:n]
    start_q = [0.0] * n
    cmd_topic, state_topic = _DEX3_TOPICS[side]

    _ensure_dds()
    try:
        sub = ChannelSubscriber(state_topic, HandState_)
        sub.Init()
        t0 = time.time()
        while time.time() - t0 < 1.0:
            st = sub.Read()
            if st is not None:
                start_q = [float(st.motor_state[i].q) for i in range(n)]
                break
            time.sleep(0.02)
    except Exception:
        pass

    pub = ChannelPublisher(cmd_topic, HandCmd_)
    pub.Init()

    def _ris_mode(motor_id: int) -> int:
        return motor_id & 0x0F | (0x01 & 0x07) << 4

    steps = max(1, int(duration / 0.02))
    for step in range(steps):
        alpha = (step + 1) / steps
        msg = unitree_hg_msg_dds__HandCmd_()
        for i in range(n):
            q = start_q[i] + alpha * (tq[i] - start_q[i])
            msg.motor_cmd[i].mode = _ris_mode(i)
            msg.motor_cmd[i].q = q
            msg.motor_cmd[i].dq = 0.0
            msg.motor_cmd[i].tau = 0.0
            msg.motor_cmd[i].kp = kp
            msg.motor_cmd[i].kd = kd
        pub.Write(msg)
        time.sleep(0.02)
    return True


def _move_hand(side: Side, open_pose: bool) -> bool:
    ht = _resolve_type()
    if not ht:
        print("[hand_grasp] tipo mano non configurato (G1_HAND_TYPE=none?)", flush=True)
        return False
    sc = _side_cfg(side)
    if ht == "dex1":
        q = float(sc.get("dex1_open" if open_pose else "dex1_close") or (
            _DEX1_OPEN_DEFAULT if open_pose else _DEX1_CLOSE_DEFAULT
        ))
        ok = _ramp_dex1(side, q)
    else:
        q = list(
            sc.get("dex3_open" if open_pose else "dex3_close")
            or (_DEX3_OPEN_DEFAULT if open_pose else _DEX3_CLOSE_DEFAULT)
        )
        ok = _ramp_dex3(side, q)
    print(f"[hand_grasp] {'open' if open_pose else 'close'} {side} ({ht}) ok={ok}", flush=True)
    return ok


def close_hand(side: Side) -> bool:
    return _move_hand(side, open_pose=False)


def open_hand(side: Side) -> bool:
    return _move_hand(side, open_pose=True)


def close_right_hand() -> bool:
    return close_hand("right")


def open_right_hand() -> bool:
    return open_hand("right")


def close_left_hand() -> bool:
    return close_hand("left")


def open_left_hand() -> bool:
    return open_hand("left")


def close_both_hands() -> tuple[bool, bool]:
    left_ok = close_left_hand()
    right_ok = close_right_hand()
    return left_ok, right_ok


def open_both_hands() -> tuple[bool, bool]:
    left_ok = open_left_hand()
    right_ok = open_right_hand()
    return left_ok, right_ok


def _grasp_hold_sec() -> float:
    hold = float((os.getenv("G1_PICK_GRASP_HOLD_SEC") or "0.8").strip() or "0.8")
    return max(0.0, min(hold, 5.0))


def _close_for_pick() -> dict[str, Any]:
    if two_hands_enabled():
        l_ok, r_ok = close_both_hands()
        return {"ok": l_ok and r_ok, "left_ok": l_ok, "right_ok": r_ok, "both": True}
    r_ok = close_right_hand()
    return {"ok": r_ok, "right_ok": r_ok, "both": False}


def _open_for_pick() -> dict[str, Any]:
    if two_hands_enabled():
        l_ok, r_ok = open_both_hands()
        return {"ok": l_ok and r_ok, "left_ok": l_ok, "right_ok": r_ok, "both": True}
    r_ok = open_right_hand()
    return {"ok": r_ok, "right_ok": r_ok, "both": False}


def grasp_close_and_hold() -> dict[str, Any]:
    if not _env_bool("G1_PICK_GRASP", True):
        return {"ok": True, "skipped": True}
    hold = _grasp_hold_sec()
    result = _close_for_pick()
    result["hand_type"] = _resolve_type()
    result["hold_s"] = hold
    result["two_hands"] = two_hands_enabled()
    if hold > 0:
        time.sleep(hold)
    return result


def grasp_open_if_configured() -> bool:
    if not _env_bool("G1_PICK_GRASP", True):
        return True
    if not _env_bool("G1_PICK_GRASP_OPEN_ON_RETURN", True):
        return True
    return bool(_open_for_pick().get("ok"))


def pick_grasp_sequence() -> dict[str, Any]:
    if not _env_bool("G1_PICK_GRASP", True):
        return {"ok": True, "skipped": True}
    close_result = grasp_close_and_hold()
    open_result = _open_for_pick() if _env_bool("G1_PICK_GRASP_OPEN_ON_RETURN", True) else {"ok": True}
    return {**close_result, "open_ok": open_result.get("ok")}
