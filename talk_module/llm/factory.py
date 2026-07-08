"""Factory LLM: OpenAI o Gemini da LLM_PROVIDER in .env."""

from talk_module.config import settings
from talk_module.llm.openai_client import LLMClient


def create_llm_client():
    """Ritorna un client LLM con metodi chat() e reset_history()."""
    if settings.llm_provider == "gemini":
        try:
            from talk_module.llm.gemini_client import GeminiLLMClient

            return GeminiLLMClient()
        except ImportError as e:
            print(
                f"[LLM] google-genai non installato ({e}); uso OpenAI. "
                f"Su Jetson: .venv/bin/pip install google-genai",
                flush=True,
            )
        except ValueError as e:
            print(f"[LLM] Gemini non configurato ({e}); uso OpenAI.", flush=True)
    return LLMClient()
