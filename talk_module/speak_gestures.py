"""Gesti contestuali per talk e azioni robot soundboard (LED, braccio, teaching)."""

from __future__ import annotations

import os
import threading
from typing import Optional

_greeting_gesture_seq = 0
_greeting_gesture_lock = threading.Lock()

_LED_EFFECT_MAP: dict[str, tuple] = {
    "rainbow": ("rainbow", (255, 180, 0), 1.0),
    "breathe_blue": ("breathe", (0, 120, 255), 1.0),
    "breathe_green": ("breathe", (0, 200, 80), 1.0),
    "breathe_red": ("breathe", (255, 60, 60), 1.0),
    "breathe_purple": ("breathe", (168, 85, 247), 1.0),
    "blink_red": ("blink", (255, 40, 40), 1.2),
    "blink_blue": ("blink", (0, 120, 255), 1.2),
    "solid_blue": ("solid", (0, 120, 255), 0),
    "solid_green": ("solid", (0, 200, 80), 0),
    "solid_red": ("solid", (255, 60, 60), 0),
    "solid_amber": ("solid", (255, 180, 0), 0),
    "solid_purple": ("solid", (168, 85, 247), 0),
    "solid_cyan": ("solid", (0, 220, 220), 0),
    "solid_white": ("solid", (255, 255, 255), 0),
}

_GREETING_WORDS = (
    "buongiorno",
    "buon giorno",
    "buonasera",
    "buona sera",
    "salve",
    "ciao",
    "benvenuti",
    "benvenuto",
    "salutami",
    "mi saluti",
    "un saluto",
)

_EXPLANATION_PROMPT_WORDS = (
    "spiega",
    "spiegami",
    "racconta",
    "raccontami",
    "presenta",
    "presentami",
    "dimmi di",
    "parlami di",
    "cos'è",
    "cos e",
    "cosa fa",
    "cosa sono",
    "come funziona",
    "che cos'è",
    "descrivi",
)


def parse_teaching_slot(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        slot = int(value)
        return slot if slot >= 0 else None
    except (TypeError, ValueError):
        return None


def fire_named_led_effect(effect_name: str) -> None:
    effect = (effect_name or "").strip().lower()
    if not effect:
        return
    entry = _LED_EFFECT_MAP.get(effect)
    if not entry:
        return
    mode, color, speed = entry
    try:
        from talk_module.robot_actions import led_start_animation, led_stop_animation, set_led_color

        if mode == "solid":
            led_stop_animation()
            set_led_color(*color)
        else:
            led_start_animation(mode=mode, color=color, speed=speed)
    except Exception as e:
        print(f"[soundboard-robot] led: {e}", flush=True)


def fire_soundboard_slot_robot(
    *,
    robot_arm: str = "",
    robot_loco: str = "",
    led_effect: str = "",
    teaching_slot=None,
    tag: str = "soundboard",
) -> None:
    """Esegue solo ciò che è configurato nello slot (nessun gesto di default)."""
    arm = (robot_arm or "").strip()
    loco = (robot_loco or "").strip()
    led = (led_effect or "").strip()
    teach = parse_teaching_slot(teaching_slot)
    if not arm and not loco and not led and teach is None:
        return

    def _run() -> None:
        try:
            if led:
                fire_named_led_effect(led)
            if teach is not None:
                from talk_module.teaching_api import get_teaching_manager

                result = get_teaching_manager().replay_slot(teach)
                print(f"[{tag}] teaching_slot={teach} -> {result}", flush=True)
            if arm:
                from talk_module.robot_actions import execute_robot_action

                ok, msg = execute_robot_action(arm)
                print(f"[{tag}] arm={arm!r} ok={ok} msg={msg}", flush=True)
            if loco:
                from talk_module.robot_actions import execute_g1_loco_command, loco_command_requires_confirm

                if not loco_command_requires_confirm(loco):
                    ok, msg = execute_g1_loco_command(loco)
                    print(f"[{tag}] loco={loco!r} ok={ok} msg={msg}", flush=True)
        except Exception as e:
            print(f"[{tag}] robot error: {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


def _greeting_gestures_from_profile() -> list[str]:
    try:
        from talk_module.visitor_context import get_active_visitor_profile

        prof = get_active_visitor_profile() or {}
        raw = prof.get("greeting_gestures") or prof.get("greeting_gesture") or ""
        if isinstance(raw, list):
            gestures = [str(g).strip() for g in raw if str(g).strip()]
            if gestures:
                return gestures
        if isinstance(raw, str) and raw.strip():
            return [g.strip() for g in raw.split(",") if g.strip()]
    except Exception:
        pass
    env = (os.getenv("G1_GREETING_GESTURES") or "face_wave").strip()
    return [g.strip() for g in env.split(",") if g.strip()]


def _explanation_gesture() -> str:
    try:
        from talk_module.visitor_context import get_active_visitor_profile

        prof = get_active_visitor_profile() or {}
        gesture = str(prof.get("explanation_gesture") or "").strip()
        if gesture:
            return gesture
    except Exception:
        pass
    return (os.getenv("G1_EXPLANATION_GESTURE") or "right_hand_up").strip() or "right_hand_up"


def is_talk_greeting(prompt: str, response: str = "") -> bool:
    try:
        from talk_module.visitor_context import check_visitor_greeting

        if check_visitor_greeting(prompt or ""):
            return True
    except Exception:
        pass
    txt = (prompt or "").strip().lower()
    if not txt:
        return False
    words = txt.split()
    if len(words) > 12:
        return False
    for word in _GREETING_WORDS:
        if word in txt:
            return True
    return False


def is_talk_explanation(prompt: str, response: str = "") -> bool:
    if is_talk_greeting(prompt, response):
        return False
    txt = (prompt or "").strip().lower()
    resp = (response or "").strip()
    if any(w in txt for w in _EXPLANATION_PROMPT_WORDS):
        return True
    if len(resp) >= 140 and len(txt.split()) >= 3:
        return True
    if len(resp) >= 220:
        return True
    return False


def _next_greeting_gesture() -> str:
    global _greeting_gesture_seq
    gestures = _greeting_gestures_from_profile()
    if not gestures:
        return "face_wave"
    with _greeting_gesture_lock:
        gesture = gestures[_greeting_gesture_seq % len(gestures)]
        _greeting_gesture_seq += 1
    return gesture


def start_talk_gesture(prompt: str, response: str = "", *, had_robot_match: bool = False) -> None:
    """Gesti durante TTS: saluto → ciao (viso); spiegazione → mano dx su."""
    if had_robot_match:
        return

    if is_talk_greeting(prompt, response):

        def _greet() -> None:
            try:
                from talk_module.robot_actions import execute_robot_action

                gesture = _next_greeting_gesture()
                ok, msg = execute_robot_action(gesture)
                print(f"[talk-gesture] greeting {gesture!r} ok={ok} msg={msg}", flush=True)
            except Exception as e:
                print(f"[talk-gesture] greeting error: {e}", flush=True)

        threading.Thread(target=_greet, daemon=True).start()
        return

    if is_talk_explanation(prompt, response):

        def _explain() -> None:
            try:
                from talk_module.robot_actions import execute_robot_action

                gesture = _explanation_gesture()
                ok, msg = execute_robot_action(gesture)
                print(f"[talk-gesture] explanation {gesture!r} ok={ok} msg={msg}", flush=True)
            except Exception as e:
                print(f"[talk-gesture] explanation error: {e}", flush=True)

        threading.Thread(target=_explain, daemon=True).start()
        return

    from talk_module.processing import start_speak_gesture

    start_speak_gesture(response)
