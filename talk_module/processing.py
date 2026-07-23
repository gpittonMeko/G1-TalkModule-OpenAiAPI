"""Audio/text processing helpers for the wake pipeline (phase 2).

LED updates and gestures run in background threads so the HTTP path stays responsive.
"""

import base64
import threading
import time
from typing import Optional

from talk_module.config import settings
from talk_module.speak_gestures import start_speak_gesture


def do_gesture_after_response(response_text: str = "") -> None:
    """Alias verso start_speak_gesture (default face_wave)."""
    start_speak_gesture(response_text)


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


def route_user_prompt(
    prompt: str,
    llm,
    *,
    voice: bool = False,
    run_robot_match_fn,
    text_model: str | None = None,
    max_tokens: int | None = None,
) -> tuple[str | None, object | None]:
    """Knowledge → robot action → quick lookup → LLM. Returns (response, robot_match)."""
    from talk_module.knowledge_store import check_knowledge, check_knowledge_voice
    from talk_module.robot_actions import check_robot_action

    prompt = (prompt or "").strip()
    if not prompt:
        return None, None

    knowledge_fn = check_knowledge_voice if voice else check_knowledge
    resp = knowledge_fn(prompt)
    if resp:
        print(f"[route] knowledge hit prompt={prompt!r}", flush=True)
        return resp, None

    robot_match = None
    try:
        robot_match = check_robot_action(prompt)
    except Exception as err:
        print(f"[robot-check] error: {err}", flush=True)
    if robot_match:
        print(
            f"[route] robot action prompt={prompt!r} arm={robot_match.arm_action!r}",
            flush=True,
        )
        return run_robot_match_fn(robot_match), robot_match

    from talk_module.quick_lookup import NOT_FOUND, is_quick_lookup_question, quick_lookup

    if is_quick_lookup_question(prompt):
        resp = quick_lookup(prompt)
        if resp != NOT_FOUND:
            print(f"[route] quick-lookup hit prompt={prompt!r}", flush=True)
            return resp, None

    tokens = max_tokens if max_tokens is not None else settings.llm_voice_max_tokens
    if text_model:
        resp = llm.chat(prompt, use_history=False, model=text_model, max_tokens=tokens)
    else:
        resp = llm.chat(prompt, use_history=False, max_tokens=tokens)
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
    return resp, None


def set_led_safe(r: int, g: int, b: int) -> None:
    """Stop animations and set a solid LED color without blocking; swallow all errors."""
    _led_stop()
    try:
        from talk_module.robot_actions import set_led_color
        threading.Thread(target=set_led_color, args=(r, g, b), daemon=True).start()
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
        from talk_module.robot_actions import LED_SPEAKING, LED_IDLE

        _led_animate("rainbow", speed=1.2)
        _, llm, tts, _, _ = get_services_fn()
        robot_match = None
        if prompt == PROMPT_HEY_G1_ACK_ONLY:
            from talk_module.visitor_context import get_hey_g1_ack_response

            resp = get_hey_g1_ack_response()
        else:
            resp, robot_match = route_user_prompt(
                prompt,
                llm,
                voice=True,
                run_robot_match_fn=run_robot_match_fn,
            )
        audio_out = tts.synthesize(resp, format="mp3") if resp else b""
        if audio_out:
            _led_animate("breathe", color=LED_SPEAKING, speed=1.0)
            if not robot_match:
                from talk_module.speak_gestures import start_talk_gesture

                start_talk_gesture(prompt, resp or "", had_robot_match=bool(robot_match))
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
