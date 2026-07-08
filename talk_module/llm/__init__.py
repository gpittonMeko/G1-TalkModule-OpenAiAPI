"""LLM: OpenAI Chat o Google Gemini (LLM_PROVIDER in .env)."""

from talk_module.llm.factory import create_llm_client
from talk_module.llm.gemini_client import GeminiLLMClient
from talk_module.llm.openai_client import LLMClient

__all__ = ["LLMClient", "GeminiLLMClient", "create_llm_client"]
