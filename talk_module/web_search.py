"""
Ricerca veloce online per domande base (ora, meteo, ecc.).
Timeout breve, se non trova: None.
"""

import json
import re
import urllib.parse
import urllib.request
from typing import Optional

TIMEOUT = 4
TIMEZONE = "Europe/Rome"


def _fetch(url: str) -> Optional[dict | str]:
    """GET con timeout breve."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "G1-Talk/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def get_current_time() -> Optional[str]:
    """Ora attuale (Europe/Rome). Ritorna es. 'Sono le 17:45' o None."""
    raw = _fetch(f"https://www.timeapi.io/api/Time/current/zone?timeZone={TIMEZONE}")
    if not raw:
        return None
    try:
        d = json.loads(raw)
        h = d.get("hour", 0)
        m = d.get("minute", 0)
        return f"Sono le {h:02d}:{m:02d}"
    except Exception:
        return None


def get_weather(city: str = "Roma") -> Optional[str]:
    """Meteo da wttr.in. Ritorna es. 'Roma: Sereno, 18°C' o None."""
    city_clean = re.sub(r"[^\w\s-]", "", city.strip())[:30] or "Roma"
    url = f"https://wttr.in/{urllib.parse.quote(city_clean)}?format=%l:+%C+%t&lang=it"
    raw = _fetch(url)
    if not raw or len(raw) > 200:
        return None
    raw = raw.strip()
    if raw and not raw.startswith("<?") and "error" not in raw.lower():
        return raw
    return None


def get_instant_answer(query: str) -> Optional[str]:
    """Risposta rapida da DuckDuckGo Instant Answer. Ritorna AbstractText o None."""
    url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json"
    raw = _fetch(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        abstract = (d.get("AbstractText") or "").strip()
        if abstract and len(abstract) > 5 and len(abstract) < 300:
            return abstract
        answer = (d.get("Answer") or "").strip()
        if answer and len(answer) > 5 and len(answer) < 200:
            return answer
        return None
    except Exception:
        return None


def search_query(query: str, search_type: str) -> Optional[str]:
    """
    Esegue ricerca in base al tipo. Ritorna risposta o None.
    search_type: time, weather, general
    """
    q = query.strip().lower()
    if search_type == "time":
        return get_current_time()
    if search_type == "weather":
        city = "Roma"
        if "roma" in q or "rome" in q:
            city = "Roma"
        elif "milano" in q or "milan" in q:
            city = "Milano"
        elif "napoli" in q or "naples" in q:
            city = "Napoli"
        return get_weather(city)
    if search_type == "general":
        return get_instant_answer(query)
    return None
