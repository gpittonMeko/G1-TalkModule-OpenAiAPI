"""
Client OpenAI Chat per risposte LLM.
"""

from datetime import date
from typing import Optional

from talk_module.config import settings
from talk_module.openai_http import make_openai_client

DEFAULT_SYSTEM = """Sei G1, robot umanoide Unitree, host digitale in sala durante un evento aziendale. Rispondi sempre in italiano.

STILE: professionale, cordiale, chiaro e conciso. Risposte brevi (1-2 frasi, ~30 parole) salvo richiesta esplicita di dettaglio. Rispondi sempre in modo diretto e preciso alla domanda posta. Non dare consulenza personalizzata né promesse su risultati. Non inventare fatti su persone, clienti o numeri non verificabili.

CONTESTO: accoglienza, orientamento (accredito, guardaroba, sala), indicazioni pratiche. Per messaggi ufficiali scriptati suggerisci la soundboard se appropriato.

HARDWARE: Unitree G1 — umanoide bimanuale; interazione vocale e gesti; teleoperato quando serve.

MOVIMENTI DISPONIBILI (attivati automaticamente da comando vocale):
- Stretta di mano ("dai la mano", "stringi la mano")
- Saluto / ciao ("saluta", "fai ciao")
- Mani in alto, applauso, high five, abbraccio
- Cuore con le mani, bacio
- Rifiuto / no, braccia incrociate, mano destra su
- Due passi avanti / indietro ("fai due passi avanti", "vai indietro")
- Gira su te stesso ("girati", "fai un giro")
Se ti chiedono cosa sai fare o quali movimenti puoi fare, elenca quelli sopra brevemente.
Se ti chiedono di fare un movimento NON in lista (es. ballare, correre, sedersi), rispondi: "Non sono ancora programmato per questo, ma il mio team ci sta lavorando!"
NON provare a eseguire azioni non in lista. Per sicurezza, declina educatamente.

NON assumere che l'evento sia legato a una società di consulenza specifica, a una città (es. Milano) o a un anniversario aziendale, salvo che l'utente ne parli esplicitamente."""

MCKINSEY_EVENT_SUPPLEMENT = """CONTESTO AGGIUNTIVO (solo perché la domanda riguarda McKinsey o società di consulenza strategica):
- Sei host in un evento legato a McKinsey & Company. Tono professionale e caloroso, in linea con l'organizzazione.
- McKinsey & Company è un'organizzazione globale di consulenza strategica; fondata nel 1926 negli Stati Uniti. In Italia ha uffici tra cui Milano. Non citare clienti o dettagli interni.
- Su storia o centenario: racconto breve e sobrio se chiedono, senza scaricare l'ospite.
- Non rimandare a «sala stampa» o ufficio stampa: rispondi in prima persona come host in sala.

DGS: partner tecnologico su automazione e robotica, per contesto operativo del robot."""


def _needs_mckinsey_or_consulting_context(user_message: str) -> bool:
    """True solo se il testo utente riguarda McKinsey o consulenza strategica in senso stretto."""
    t = (user_message or "").lower()
    if "mckinsey" in t or "mc kinsey" in t:
        return True
    needles = (
        "società di consulenza",
        "societa di consulenza",
        "consulenza strategica",
        "management consulting",
        "firma di consulenza",
        "firme di consulenza",
        "organizzazione di consulenza",
        "big three",
        "bcg",
        "bain &",
        "bain and",
    )
    return any(n in t for n in needles)


def _dynamic_event_context(user_message: str) -> str:
    """Data odierna; nota evento McKinsey solo se la domanda è nel tema."""
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
    parts = [f"Riferimento temporale: oggi è {oggi}."]
    if _needs_mckinsey_or_consulting_context(user_message):
        parts.append(
            "Se pertinente alla domanda, il 31 marzo 2026 può essere una data simbolica nel calendario dell'evento McKinsey; "
            "non ripetere date senza necessità."
        )
    return " ".join(parts)


class LLMClient:
    """Genera risposte tramite OpenAI Chat API."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.client = make_openai_client(api_key or settings.api_key)
        self.model = model or settings.llm_model
        self.system_prompt = DEFAULT_SYSTEM
        self.history: list[dict] = []

    def _is_reasoning_model(self, model: str) -> bool:
        return any(x in model for x in ("gpt-5", "o1", "o3", "o4"))

    def _build_kwargs(self, model: str, messages: list, max_tokens_override: Optional[int] = None) -> dict:
        kwargs: dict = {"model": model, "messages": messages}
        if not self._is_reasoning_model(model):
            kwargs["temperature"] = 0.5
        cap = max(64, max_tokens_override or settings.llm_max_completion_tokens)
        if self._is_reasoning_model(model):
            kwargs["max_completion_tokens"] = max(cap, 512)
        elif any(x in model for x in ("gpt-4o", "gpt-4", "gpt-3.5", "gpt-4o-mini")):
            kwargs["max_completion_tokens"] = cap
        else:
            kwargs["max_tokens"] = cap
        if "gpt-5" in model:
            kwargs["reasoning_effort"] = "low"
        return kwargs

    def chat(self, user_message: str, system: Optional[str] = None, *,
             use_history: bool = True, model: Optional[str] = None,
             max_tokens: Optional[int] = None) -> str:
        """
        Invia messaggio e ottieni risposta.
        use_history=True: mantiene contesto conversazione (default, voce).
        use_history=False: stateless, per domande scritte indipendenti.
        model: override modello (es. settings.llm_text_model).
        max_tokens: override max_completion_tokens per questa chiamata.
        """
        if not user_message or not user_message.strip():
            return ""
        um = user_message.strip()
        effective_model = model or self.model
        base = system or self.system_prompt
        if _needs_mckinsey_or_consulting_context(um):
            base = f"{base}\n\n{MCKINSEY_EVENT_SUPPLEMENT}"
        sys = f"{base}\n\n{_dynamic_event_context(um)}"
        messages = [{"role": "system", "content": sys}]
        if use_history:
            for h in self.history[-2:]:
                messages.append(h)
        messages.append({"role": "user", "content": um})
        try:
            kwargs = self._build_kwargs(effective_model, messages, max_tokens)
            resp = self.client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            if content and use_history:
                self.history.append({"role": "user", "content": um})
                self.history.append({"role": "assistant", "content": content})
            return (content or "").strip()
        except Exception as e:
            print(f"[LLM] Errore: {e}")
            return ""

    def reset_history(self) -> None:
        """Azzera la cronologia conversazione."""
        self.history.clear()
