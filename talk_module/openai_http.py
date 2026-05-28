"""
Client OpenAI SDK con timeout HTTP espliciti.

Default: connect 20s, read 60s, 1 retry — fallimento rapido se OpenAI non risponde.
Override in .env: OPENAI_READ_TIMEOUT, OPENAI_CONNECT_TIMEOUT, OPENAI_MAX_RETRIES.
"""

from __future__ import annotations

from typing import Optional

import httpx
from openai import OpenAI

from talk_module.config import settings


def make_openai_client(api_key: Optional[str] = None) -> OpenAI:
    key = api_key if api_key is not None else settings.api_key
    timeout = httpx.Timeout(
        connect=settings.openai_connect_timeout,
        read=settings.openai_read_timeout,
        write=min(90.0, settings.openai_read_timeout),
        pool=15.0,
    )
    return OpenAI(api_key=key, timeout=timeout, max_retries=settings.openai_max_retries)
