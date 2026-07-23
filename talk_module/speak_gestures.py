"""Gesti contestuali per talk e azioni robot soundboard (LED, braccio, teaching)."""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

_greeting_gesture_seq = 0
_greeting_gesture_lock = threading.Lock()
_speak_gesture_seq = 0
_speak_gesture_lock = threading.Lock()

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


def dispatch_soundboard_slot_robot(
    *,
    robot_arm: str = "",
    robot_loco: str = "",
    led_effect: str = "",
    teaching_slot=None,
    tag: str = "soundboard",
) -> None:
    """Invia comandi robot (preset o teach Explore)."""
    arm = (robot_arm or "").strip()
    loco = (robot_loco or "").strip()
    led = (led_effect or "").strip()
    teach_name = str(teaching_slot).strip() if teaching_slot is not None and str(teaching_slot).strip() else ""
    if not arm and not loco and not led and not teach_name:
        return
    if arm and teach_name:
        teach_name = ""
    try:
        if led:
            fire_named_led_effect(led)
        if arm:
            from talk_module.robot_actions import execute_robot_action

            ok, msg = execute_robot_action(arm)
            print(f"[{tag}] arm={arm!r} ok={ok} msg={msg}", flush=True)
        if teach_name:
            from talk_module.explore_teaching import play_explore_teaching

            result = play_explore_teaching(teach_name)
            print(
                f"[{tag}] teach={teach_name!r} ok={result.get('ok')} msg={result.get('message')}",
                flush=True,
            )
        if loco:
            from talk_module.robot_actions import execute_g1_loco_command, loco_command_requires_confirm

            if not loco_command_requires_confirm(loco):
                ok, msg = execute_g1_loco_command(loco)
                print(f"[{tag}] loco={loco!r} ok={ok} msg={msg}", flush=True)
    except Exception as e:
        print(f"[{tag}] robot error: {e}", flush=True)


def fire_soundboard_slot_robot(
    *,
    robot_arm: str = "",
    robot_loco: str = "",
    led_effect: str = "",
    teaching_slot=None,
    tag: str = "soundboard",
) -> None:
    """Esegue solo ciò che è configurato nello slot (thread in background)."""
    arm = (robot_arm or "").strip()
    loco = (robot_loco or "").strip()
    led = (led_effect or "").strip()
    teach_name = str(teaching_slot).strip() if teaching_slot is not None and str(teaching_slot).strip() else ""
    if not arm and not loco and not led and not teach_name:
        return

    def _run() -> None:
        dispatch_soundboard_slot_robot(
            robot_arm=arm,
            robot_loco=loco,
            led_effect=led,
            teaching_slot=teach_name,
            tag=tag,
        )

    threading.Thread(target=_run, daemon=True).start()


def _wait_until(deadline: float) -> None:
    delay = deadline - time.time()
    if delay > 0:
        time.sleep(delay)


def begin_soundboard_gesture_schedule(
    *,
    play_t0: float,
    gesture_delay_ms: int = 0,
    robot_arm: str = "",
    robot_loco: str = "",
    teaching_slot=None,
    tag: str = "soundboard",
) -> None:
    """Avvia timer gesto da Play (prima di decode/ffmpeg)."""
    arm = (robot_arm or "").strip()
    loco = (robot_loco or "").strip()
    teach_name = str(teaching_slot).strip() if teaching_slot is not None and str(teaching_slot).strip() else ""
    if arm and teach_name:
        teach_name = ""
    if not arm and not loco and not teach_name:
        return

    gesture_delay_ms = max(0, min(int(gesture_delay_ms or 0), 15000))
    gesture_deadline = play_t0 + gesture_delay_ms / 1000.0

    def _run_gesture() -> None:
        try:
            _wait_until(gesture_deadline)
            print(
                f"[{tag}] gesture fire delay={gesture_delay_ms}ms at={time.time() - play_t0:.3f}s",
                flush=True,
            )
            dispatch_soundboard_slot_robot(
                robot_arm=arm,
                robot_loco=loco,
                led_effect="",
                teaching_slot=teach_name,
                tag=tag,
            )
        except Exception as e:
            print(f"[{tag}] robot schedule error: {e}", flush=True)

    threading.Thread(target=_run_gesture, daemon=True).start()


def play_soundboard_audio_scheduled(
    audio_raw: bytes,
    audio_fmt: str,
    *,
    play_t0: float,
    audio_delay_ms: int = 0,
    slot_idx: int = -1,
    tag: str = "soundboard",
) -> bool:
    """Riproduce audio G1 con ritardo assoluto da Play (indipendente dal gesto)."""
    from talk_module.audio.g1_speaker import play_pcm_on_g1
    from talk_module.audio.soundboard_pcm_cache import get_pcm, warmup_pcm

    audio_delay_ms = max(0, min(int(audio_delay_ms or 0), 15000))
    audio_deadline = play_t0 + audio_delay_ms / 1000.0

    if slot_idx >= 0:
        warmup_pcm(slot_idx, audio_raw, audio_fmt)

    audio_result = [False]
    audio_done = threading.Event()

    def _run_audio() -> None:
        pcm_box: list[bytes | None] = [None]
        pcm_ready = threading.Event()

        def _load() -> None:
            try:
                pcm = get_pcm(slot_idx, audio_raw, audio_fmt) if slot_idx >= 0 else None
                if not pcm:
                    from talk_module.audio.soundboard_convert import soundboard_bytes_to_pcm_g1

                    pcm = soundboard_bytes_to_pcm_g1(audio_raw, audio_fmt)
                pcm_box[0] = pcm
            finally:
                pcm_ready.set()

        threading.Thread(target=_load, daemon=True).start()
        try:
            _wait_until(audio_deadline)
            pcm_ready.wait(timeout=120)
            pcm = pcm_box[0]
            if not pcm:
                print(f"[{tag}] decode produced empty PCM", flush=True)
                return
            print(
                f"[{tag}] audio start delay={audio_delay_ms}ms at={time.time() - play_t0:.3f}s",
                flush=True,
            )
            audio_result[0] = play_pcm_on_g1(pcm)
        except Exception as e:
            print(f"[{tag}] audio schedule error: {e}", flush=True)
        finally:
            audio_done.set()

    threading.Thread(target=_run_audio, daemon=True).start()
    audio_done.wait()
    return audio_result[0]


def play_soundboard_slot_synced(
    wav_g1: bytes,
    *,
    robot_arm: str = "",
    robot_loco: str = "",
    led_effect: str = "",
    teaching_slot=None,
    tag: str = "soundboard",
) -> bool:
    """Audio G1 + gesti robot sincronizzati (solo 0/0: gesto su inizio stream)."""
    from talk_module.audio.g1_speaker import _wav_to_pcm16_mono_16k, play_pcm_on_g1

    pcm = _wav_to_pcm16_mono_16k(wav_g1)
    if not pcm:
        return False

    arm = (robot_arm or "").strip()
    loco = (robot_loco or "").strip()
    led = (led_effect or "").strip()
    teach_name = str(teaching_slot).strip() if teaching_slot is not None and str(teaching_slot).strip() else ""
    if arm and teach_name:
        teach_name = ""

    if led:
        fire_named_led_effect(led)

    has_motion = bool(arm or loco or teach_name)
    if has_motion:

        def _on_stream_started() -> None:
            def _fire() -> None:
                try:
                    dispatch_soundboard_slot_robot(
                        robot_arm=arm,
                        robot_loco=loco,
                        led_effect="",
                        teaching_slot=teach_name,
                        tag=tag,
                    )
                except Exception as e:
                    print(f"[{tag}] robot during audio error: {e}", flush=True)

            threading.Thread(target=_fire, daemon=True).start()

        return play_pcm_on_g1(pcm, on_stream_started=_on_stream_started)

    return play_pcm_on_g1(pcm)


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


def _speak_gesture_actions() -> list[str]:
    """G1_SPEAK_GESTURE: face_wave (default) | nome gesto | off | lista comma-separated."""
    raw = (os.getenv("G1_SPEAK_GESTURE") or "face_wave").strip()
    if not raw or raw.lower() in ("0", "false", "no", "off", "none"):
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def start_speak_gesture(response_text: str = "") -> None:
    """Muove le braccia mentre G1 parla (TTS). Non blocca la pipeline HTTP."""
    actions = _speak_gesture_actions()
    if not actions:
        return

    global _speak_gesture_seq
    with _speak_gesture_lock:
        primary = actions[_speak_gesture_seq % len(actions)]
        _speak_gesture_seq += 1

    txt_len = len((response_text or "").strip())

    def _run() -> None:
        try:
            from talk_module.robot_actions import execute_robot_action

            ok, msg = execute_robot_action(primary)
            print(f"[speak-gesture] {primary!r} ok={ok} msg={msg}", flush=True)
            if txt_len > 100 and len(actions) > 1:
                time.sleep(3.5)
                with _speak_gesture_lock:
                    secondary = actions[_speak_gesture_seq % len(actions)]
                    _speak_gesture_seq += 1
                if secondary != primary:
                    ok2, msg2 = execute_robot_action(secondary)
                    print(f"[speak-gesture] follow-up {secondary!r} ok={ok2} msg={msg2}", flush=True)
        except Exception as e:
            print(f"[speak-gesture] error: {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


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

    start_speak_gesture(response)
