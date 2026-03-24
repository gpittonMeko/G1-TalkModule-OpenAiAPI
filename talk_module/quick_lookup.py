"""
Ricerca veloce online per domande base: ora, meteo, domande fattuali.
Scraping leggero con timeout breve. Se non trova: "Non ho trovato."
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from talk_module.config import settings

NOT_FOUND = "Non ho trovato."
_TIMEOUT = getattr(settings, "quick_lookup_timeout", 4) or 4

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
    r"^chi\s+[eè]\s*",
    r"^cos['']?\s*[eè]\s*",
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


def _fetch(url: str) -> Optional[str]:
    """GET veloce con timeout."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "G1-Talk/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def _lookup_time() -> str:
    """Ora attuale da timeapi.io (Europe/Rome). Fallback: orologio server."""
    raw = _fetch("https://www.timeapi.io/api/Time/current/zone?timeZone=Europe/Rome")
    if raw:
        try:
            d = json.loads(raw)
            h, m = d.get("hour", 0), d.get("minute", 0)
            return f"Sono le {h:02d}:{m:02d}."
        except Exception:
            pass
    try:
        tz = ZoneInfo("Europe/Rome")
        now = datetime.now(tz)
        return f"Sono le {now.hour:02d}:{now.minute:02d}."
    except Exception:
        pass
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
    return "Roma"


def _lookup_weather(text: str) -> str:
    """Meteo via wttr.in (scraping veloce). format=3: 'Roma: +18°C Sereno'."""
    city = _extract_city(text)
    try:
        import requests
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3"
        r = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "curl/7.68.0"})
        if r.status_code == 200 and r.text.strip():
            txt = r.text.strip()
            if len(txt) < 100 and "error" not in txt.lower():
                return txt
    except Exception:
        pass
    raw = _fetch(f"https://wttr.in/{urllib.parse.quote(city)}?format=%l:+%C+%t&lang=it")
    if raw and len(raw) < 150 and "error" not in raw.lower():
        return raw.strip()
    return NOT_FOUND


def _lookup_duckduckgo(text: str) -> str:
    """Risposta rapida: DuckDuckGo Instant Answer API (no key) o ddgs."""
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(text)}&format=json"
    raw = _fetch(url)
    if raw:
        try:
            d = json.loads(raw)
            for key in ("AbstractText", "Answer"):
                val = (d.get(key) or "").strip()
                if val and 10 < len(val) < 250:
                    return val[:120] + ("..." if len(val) > 120 else "")
        except Exception:
            pass
    try:
        from ddgs import DDGS
        results = list(DDGS().text(text, max_results=1))
        if results and results[0].get("body"):
            body = results[0]["body"].strip()[:120]
            return body + ("..." if len(results[0]["body"]) > 120 else "")
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
