"""Audio/text processing helpers for the wake pipeline (phase 2).

LED updates and gestures run in background threads so the HTTP path stays responsive.
"""

import base64
import threading
import time
from typing import Optional

from talk_module.config import settings


def _led_animate(mode: str, color: tuple[int, int, int] = (255, 180, 0), speed: float = 1.0) -> None:
    """Start a LED animation without blocking; swallow all errors."""
    try:
        from talk_module.robot_actions import led_start_animation
        led_start_animation(mode=mode, color=color, speed=speed)
    except Exception:
        pass


def _led_stop() -> None:
    """Stop any running LED animation."""
    try:
        from talk_module.robot_actions import led_stop_animation
        led_stop_animation()
    except Exception:
        pass


def set_led_safe(r: int, g: int, b: int) -> None:
    """Stop animations and set a solid LED color without blocking; swallow all errors."""
    _led_stop()
    try:
        from talk_module.robot_actions import set_led_color
        threading.Thread(target=set_led_color, args=(r, g, b), daemon=True).start()
    except Exception:
        pass


def do_gesture_after_response() -> None:
    """Fire the post-response arm gesture (right_hand_up); errors are ignored."""
    try:
        from talk_module.robot_actions import execute_robot_action
        threading.Thread(target=execute_robot_action, args=("right_hand_up",), daemon=True).start()
    except Exception:
        pass


def process_after_wake(
    prompt: str,
    raw_text: str,
    t0: float,
    get_services_fn,
    check_knowledge_fn,
    run_robot_match_fn,
) -> dict:
    """Run knowledge / quick-lookup / LLM / TTS after wake; return JSON-shaped dict.

    ``get_services_fn`` must return a tuple ``(_, llm, tts, _, _)`` (STT, LLM, TTS, …).
    ``check_knowledge_fn(prompt)`` returns a response string or a falsy value if none.
    ``run_robot_match_fn(match)`` runs the matched robot action and returns the reply text.
    """
    PROMPT_HEY_G1_ACK_ONLY = "__G1_HEY_ACK_ONLY__"
    try:
        from talk_module.robot_actions import LED_THINKING, LED_SPEAKING, LED_IDLE

        _led_animate("rainbow", speed=1.2)
        _, llm, tts, _, _ = get_services_fn()
        if prompt == PROMPT_HEY_G1_ACK_ONLY:
            resp = (settings.hey_g1_ack_text or "").strip() or "Sì?"
        else:
            from talk_module.robot_actions import check_robot_action

            robot_match = None
            try:
                robot_match = check_robot_action(prompt)
            except Exception as _ra_err:
                print(f"[robot-check] error: {_ra_err}", flush=True)
            if robot_match:
                print(f"[robot-check] MATCH prompt={prompt!r} arm={robot_match.arm_action!r}", flush=True)
                resp = run_robot_match_fn(robot_match)
            else:
                print(f"[robot-check] no match for prompt={prompt!r}", flush=True)
                resp = check_knowledge_fn(prompt)
                if not resp:
                    from talk_module.quick_lookup import NOT_FOUND, is_quick_lookup_question, quick_lookup

                    if is_quick_lookup_question(prompt):
                        resp = quick_lookup(prompt)
                        if resp == NOT_FOUND:
                            resp = None
                if not resp:
                    resp = llm.chat(prompt, use_history=False)
                if resp and not robot_match:
                    try:
                        post_match = check_robot_action(resp)
                        if post_match and post_match.arm_action:
                            print(
                                f"[robot-post-llm] LLM triggered action: arm={post_match.arm_action!r}",
                                flush=True,
                            )
                            run_robot_match_fn(post_match)
                    except Exception:
                        pass
        audio_out = tts.synthesize(resp, format="mp3") if resp else b""
        if audio_out:
            _led_animate("breathe", color=LED_SPEAKING, speed=1.0)
            do_gesture_after_response()
        else:
            set_led_safe(*LED_IDLE)
        return {
            "text": raw_text,
            "response": resp or "",
            "audio_base64": base64.b64encode(audio_out).decode() if audio_out else "",
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "led_speaking": bool(audio_out),
        }
    except Exception as e:
        try:
            from talk_module.robot_actions import LED_IDLE
            set_led_safe(*LED_IDLE)
        except Exception:
            pass
        err = str(e)
        el = err.lower()
        if (
            "401" in err
            or "expired_api_key" in el
            or "invalid_api_key" in el
            or "invalid api key" in el
            or "incorrect api key" in el
        ):
            err = "Chiave API non valida o scaduta. Aggiorna .env e riavvia. " + err
        elif "invalid file format" in el or (
            "supported formats" in el and ("flac" in el or "webm" in el or "mp3" in el)
        ):
            err = "STT: formato rifiutato. pip install imageio-ffmpeg. " + err
        return {
            "text": raw_text,
            "response": "",
            "audio_base64": "",
            "message": f"Errore: {err}",
            "duration_ms": int((time.perf_counter() - t0) * 1000),
        }
