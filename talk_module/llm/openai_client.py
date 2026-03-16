"""
Client OpenAI Chat per risposte LLM.
"""

from typing import Optional

from openai import OpenAI

from talk_module.config import settings

DEFAULT_SYSTEM = """Sei G1, il robot umanoide Unitree. Rispondi sempre in italiano. Risposte brevissime: max 15 parole. Tono diretto.

CONOSCENZA:
- DGS S.p.A.: azienda con sede a Roma. Sviluppa soluzioni robotiche e di automazione.
- Unitree G1: robot umanoide bimanuale. Cammina, afferra oggetti, interagisce. Si programma con Unitree SDK (Python), ROS, o API. Riconoscimento vocale, visione, manipolazione.
- Domande tipiche: ora, meteo, barzellette, "Cosa sai fare", "Come ti chiami" - rispondi in modo preciso e utile."""


class LLMClient:
    """Genera risposte tramite OpenAI Chat API."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.client = OpenAI(api_key=api_key or settings.api_key)
        self.model = model or settings.llm_model
        self.system_prompt = DEFAULT_SYSTEM
        self.history: list[dict] = []

    def chat(self, user_message: str, system: Optional[str] = None) -> str:
        """
        Invia messaggio e ottieni risposta.
        Mantiene contesto di conversazione (history).
        """
        if not user_message or not user_message.strip():
            return ""
        sys = system or self.system_prompt
        messages = [{"role": "system", "content": sys}]
        for h in self.history[-4:]:  # ultimi 2 scambi
            messages.append(h)
        messages.append({"role": "user", "content": user_message.strip()})
        try:
            kwargs = {
                "model": self.model,
                "messages": messages,
            }
            # GPT-5 series: temperature NON supportata, solo default 1. Non inviare mai.
            if not any(x in self.model for x in ("gpt-5", "o1", "o3", "o4")):
                kwargs["temperature"] = 0.5
            # Modelli recenti: max_completion_tokens
            # Per gpt-5-nano (reasoning): i reasoning_tokens consumano il budget → serve margine per output
            if any(x in self.model for x in ("gpt-5-nano", "gpt-5-mini", "gpt-5", "o1", "o3", "o4")):
                kwargs["max_completion_tokens"] = 256  # reasoning + risposta breve
            elif any(x in self.model for x in ("gpt-4o",)):
                kwargs["max_completion_tokens"] = 50
            else:
                kwargs["max_tokens"] = 50
            # GPT-5: reasoning_effort riduce i reasoning_tokens (che consumano max_completion_tokens)
            if "gpt-5" in self.model:
                kwargs["reasoning_effort"] = "minimal"
            if "gpt-5" in self.model and "gpt-5-nano" not in self.model:
                kwargs["verbosity"] = "low"
            resp = self.client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            if content:
                self.history.append({"role": "user", "content": user_message.strip()})
                self.history.append({"role": "assistant", "content": content})
            return (content or "").strip()
        except Exception as e:
            print(f"[LLM] Errore: {e}")
            return ""

    def reset_history(self) -> None:
        """Azzera la cronologia conversazione."""
        self.history.clear()
