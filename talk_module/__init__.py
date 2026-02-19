"""
G1 Talk Module - Voice interaction with robot via OpenAI STT/LLM/TTS.

Usage:
    from talk_module import TalkPipeline
    pipeline = TalkPipeline()
    pipeline.run_conversation()
"""

from talk_module.config import settings

def __getattr__(name):
    if name == "TalkPipeline":
        from talk_module.pipeline import TalkPipeline
        return TalkPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__version__ = "1.0.0"
__all__ = ["TalkPipeline", "settings"]
