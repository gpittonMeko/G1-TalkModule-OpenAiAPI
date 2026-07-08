"""
Client Google Gemini per risposte LLM (stessa interfaccia di LLMClient OpenAI).
"""

from typing import Optional

from talk_module.config import settings
from talk_module.visitor_context import get_visitor_system_supplement
from talk_module.llm.openai_client import (
    DEFAULT_SYSTEM,
    MCKINSEY_EVENT_SUPPLEMENT,
    _dynamic_event_context,
    _needs_mckinsey_or_consulting_context,
)


class GeminiLLMClient:
    """Genera risposte tramite Gemini API (google-genai)."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        try:
            from google import genai
        except ImportError as e:
            raise ImportError(
                "google-genai non installato. Esegui: pip install google-genai"
            ) from e
        key = api_key or settings.gemini_api_key
        if not key:
            raise ValueError("GEMINI_API_KEY non configurata in .env")
        self.client = genai.Client(api_key=key)
        self.model = model or settings.gemini_model
        self.system_prompt = DEFAULT_SYSTEM
        self.history: list[dict] = []

    def chat(
        self,
        user_message: str,
        system: Optional[str] = None,
        *,
        use_history: bool = True,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        from google.genai import types

        if not user_message or not user_message.strip():
            return ""
        um = user_message.strip()
        effective_model = model or self.model
        base = system or self.system_prompt
        visitor_sup = get_visitor_system_supplement()
        if visitor_sup:
            base = f"{base}\n\n{visitor_sup}"
        if _needs_mckinsey_or_consulting_context(um):
            base = f"{base}\n\n{MCKINSEY_EVENT_SUPPLEMENT}"
        sys = f"{base}\n\n{_dynamic_event_context(um)}"

        contents: list = []
        if use_history:
            for h in self.history[-2:]:
                role = "user" if h["role"] == "user" else "model"
                contents.append(
                    types.Content(role=role, parts=[types.Part.from_text(text=h["content"])])
                )
        contents.append(
            types.Content(role="user", parts=[types.Part.from_text(text=um)])
        )

        cap = max(64, max_tokens or settings.llm_max_completion_tokens)
        try:
            resp = self.client.models.generate_content(
                model=effective_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=sys,
                    temperature=0.5,
                    max_output_tokens=cap,
                ),
            )
            text = (resp.text or "").strip()
            if text and use_history:
                self.history.append({"role": "user", "content": um})
                self.history.append({"role": "assistant", "content": text})
            return text
        except Exception as e:
            print(f"[LLM:Gemini] Errore: {e}", flush=True)
            return ""

    def reset_history(self) -> None:
        self.history.clear()
