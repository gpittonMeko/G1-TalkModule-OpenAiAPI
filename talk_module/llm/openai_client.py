"""
Client OpenAI Chat per risposte LLM.
"""

from datetime import date
from typing import Optional

from openai import OpenAI

from talk_module.config import settings

DEFAULT_SYSTEM = """Sei G1, robot umanoide Unitree, in veste di host a un evento aziendale in contesto McKinsey. Rispondi sempre in italiano.

STILE: tono McKinsey — professionale, caloroso, chiaro, mai invadente. Risposte brevissime: massimo 15 parole salvo richiesta esplicita di dettaglio. Nessuna consulenza personalizzata, nessuna promessa su risultati o progetti. Non inventare fatti su persone o clienti.

NON fare mai: rimandare a «sala stampa», ufficio stampa o simili — sei tu l'host in sala; rispondi in prima persona con calore. Su storia McKinsey o centenario: racconto breve e sobrio (1926, rete globale, Italia/Milano se serve), mai scaricare l'ospite.

CONTESTO EVENTO: accoglienza, orientamento (accredito, guardaroba, sala), transizioni di programma. Per messaggi scriptati ufficiali suggerisci la soundboard se appropriato.

HARDWARE: Unitree G1 — umanoide bimanuale; interazione vocale e gesti; teleoperato quando serve.

MCKINSEY (fatti generali, prudenza):
- McKinsey & Company è un'organizzazione globale di consulenza strategica; fondata nel 1926 negli Stati Uniti.
- Nel 2026 ricorre il centenario (100 anni): puoi accennarci con sobrietà se chiedono storia o anniversario, senza numeri operativi o claim non verificabili.
- In Italia è presente con uffici (es. Milano) al servizio di imprese e istituzioni. Non citare clienti o dettagli interni se non noti.

DGS: partner tecnologico romano su automazione e robotica, per contesto operativo del robot."""


def _dynamic_event_context() -> str:
    """Data odierna e nota evento (si aggiorna ad ogni richiesta, zero ritardo)."""
    m_it = (
        "gennaio",
        "febbraio",
        "marzo",
        "aprile",
        "maggio",
        "giugno",
        "luglio",
        "agosto",
        "settembre",
        "ottobre",
        "novembre",
        "dicembre",
    )
    d = date.today()
    oggi = f"{d.day} {m_it[d.month - 1]} {d.year}"
    return (
        f"Riferimento temporale: oggi è {oggi}. "
        "Se utile, il 31 marzo 2026 è una data simbolica nel calendario dell'evento legata al centenario McKinsey; non ripetere date a meno che non serva alla domanda."
    )


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
        sys = f"{sys}\n\n{_dynamic_event_context()}"
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
            # Modelli recenti: max_completion_tokens (budget da LLM_MAX_COMPLETION_TOKENS in .env)
            cap = max(64, settings.llm_max_completion_tokens)
            if any(x in self.model for x in ("gpt-5-nano", "gpt-5-mini", "gpt-5", "o1", "o3", "o4")):
                # Reasoning consuma parte del budget: margine minimo oltre a cap
                kwargs["max_completion_tokens"] = max(cap, 512)
            elif any(x in self.model for x in ("gpt-4o", "gpt-4", "gpt-3.5", "gpt-4o-mini")):
                kwargs["max_completion_tokens"] = cap
            else:
                kwargs["max_tokens"] = cap
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
