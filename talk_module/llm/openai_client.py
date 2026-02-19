"""
Client OpenAI Chat per risposte LLM.
"""

from typing import Optional

from openai import OpenAI

from talk_module.config import settings

DEFAULT_SYSTEM = """Sei l'assistente vocale del robot Unitree G1. Risposte brevissime: 1-2 frasi, max 20 parole. Tono amichevole.

CONOSCENZA:
- DGS S.p.A.: azienda con sede a Roma. Sviluppa soluzioni robotiche e di automazione.
- Unitree G1: robot umanoide bimanuale. Cammina, afferra oggetti, interagisce. Si programma con Unitree SDK (Python), framework Robot Operating System (ROS), o API. Può fare riconoscimento vocale, visione, manipolazione.
- Domande tipiche: "Che ore sono", "Che tempo fa", "Raccontami una barzelletta", "Cosa sai fare", "Come ti chiami" - rispondi in modo breve e cordiale."""


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
        for h in self.history[-6:]:  # ultimi 3 scambi
            messages.append(h)
        messages.append({"role": "user", "content": user_message.strip()})
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=80,
                temperature=0.6,
            )
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
