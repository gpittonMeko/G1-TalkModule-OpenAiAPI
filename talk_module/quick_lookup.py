"""
Ricerca veloce per domande base: ora, meteo, domande fattuali.
Prima di chiamare l'LLM, tenta una lookup online rapida.
Se non trova nulla: "Non ho trovato."
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from talk_module.config import settings

NOT_FOUND = "Non ho trovato."

# Pattern per domande "base" (ora, meteo, fattuali)
_TIME_PATTERNS = (
    r"che\s+or[ae]\s*(sono|è)?",
    r"orario",
    r"ora\s+attuale",
    r"che\s+ora\s+fanno",
)
_METEO_PATTERNS = (
    r"meteo",
    r"tempo\s+(fa|a)?",
    r"temperatura",
    r"che\s+tempo\s+fa",
    r"previsioni",
    r"fa\s+freddo",
    r"fa\s+caldo",
)
_FACTUAL_PATTERNS = (
    r"^chi\s+è\b",
    r"^cos['']?\s*è\b",
    r"^quando\s+",
    r"^dove\s+",
)

_TIME_RE = re.compile("|".join(f"({p})" for p in _TIME_PATTERNS), re.IGNORECASE)
_METEO_RE = re.compile("|".join(f"({p})" for p in _METEO_PATTERNS), re.IGNORECASE)
_FACTUAL_RE = re.compile("|".join(_FACTUAL_PATTERNS), re.IGNORECASE)


def is_quick_lookup_question(text: str) -> bool:
    """Rileva se la domanda è una 'domanda base' (ora, meteo, fattuale)."""
    if not text or not text.strip():
        return False
    if not getattr(settings, "quick_lookup_enabled", True):
        return False
    txt = text.strip().lower()
    return bool(_TIME_RE.search(txt) or _METEO_RE.search(txt) or _FACTUAL_RE.search(txt))


def _lookup_time() -> str:
    """Ora attuale in Italia (Europe/Rome). Nessuna chiamata web."""
    try:
        tz = ZoneInfo("Europe/Rome")
        now = datetime.now(tz)
        h, m = now.hour, now.minute
        return f"Sono le {h}:{m:02d}."
    except Exception:
        return NOT_FOUND


def _extract_city(text: str) -> str:
    """Estrae città dalla domanda (es. 'meteo a Milano' -> Milan)."""
    txt = text.strip().lower()
    # "meteo a X", "tempo a X", "che tempo fa a X"
    m = re.search(r"(?:meteo|tempo|previsioni)\s+(?:a|ad|in)\s+(\w+)", txt)
    if m:
        city = m.group(1).strip()
        # Mappatura italiana -> wttr.in
        city_map = {
            "roma": "Rome",
            "milano": "Milan",
            "napoli": "Naples",
            "torino": "Turin",
            "firenze": "Florence",
            "bologna": "Bologna",
            "venezia": "Venice",
            "genova": "Genoa",
            "palermo": "Palermo",
        }
        return city_map.get(city, city.capitalize())
    return getattr(settings, "default_weather_city", "Rome")


def _lookup_weather(text: str) -> str:
    """Meteo via wttr.in."""
    try:
        import requests

        city = _extract_city(text)
        url = f"https://wttr.in/{city}?format=3"
        timeout = getattr(settings, "quick_lookup_timeout", 3)
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "curl/7.68.0"})
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception:
        pass
    return NOT_FOUND


def _lookup_duckduckgo(text: str) -> str:
    """Ricerca DuckDuckGo per domande fattuali."""
    try:
        from ddgs import DDGS

        results = list(DDGS().text(text, max_results=1))
        if results and results[0].get("body"):
            body = results[0]["body"].strip()
            # Tronca se troppo lunga (max 80 caratteri per risposta breve)
            if len(body) > 80:
                body = body[:77] + "..."
            return body
    except Exception:
        pass
    return NOT_FOUND


def quick_lookup(text: str) -> Optional[str]:
    """
    Esegue lookup veloce per domande base.
    Ritorna risposta o None se non applicabile.
    """
    if not text or not text.strip():
        return None
    if not getattr(settings, "quick_lookup_enabled", True):
        return None

    txt = text.strip().lower()

    # Ora
    if _TIME_RE.search(txt):
        return _lookup_time()

    # Meteo
    if _METEO_RE.search(txt):
        return _lookup_weather(text)

    # Domande fattuali (chi è, cos'è, quando, dove)
    if _FACTUAL_RE.search(txt):
        return _lookup_duckduckgo(text)

    return None
