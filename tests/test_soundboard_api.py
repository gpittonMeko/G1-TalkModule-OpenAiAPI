"""Test endpoint soundboard (lite + slot singolo) senza avviare il server."""
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from talk_module import web_app


@pytest.fixture
def client():
    if not getattr(web_app, "HAS_FASTAPI", False) or web_app.app is None:
        pytest.skip("FastAPI non disponibile")
    return TestClient(web_app.app)


def test_soundboard_lite_no_base64_keys(client):
    r = client.get("/api/soundboard?lite=1")
    assert r.status_code == 200
    d = r.json()
    slots = d.get("slots") or []
    assert len(slots) == 20
    assert d.get("slot_count") == 20
    s0 = slots[0]
    assert "has_robot" in s0 and "has_clean" in s0
    assert s0.get("audio_base64") in (None, "")
    assert s0.get("audio_base64_clean") in (None, "")


def test_soundboard_slot_has_audio_fields(client):
    r = client.get("/api/soundboard-slot/0")
    assert r.status_code == 200
    j = r.json()
    assert "audio_base64" in j and "audio_base64_clean" in j
    assert "format" in j and "format_clean" in j


def test_soundboard_cache_second_lite_request_fast(client):
    client.get("/api/soundboard?lite=1")
    r2 = client.get("/api/soundboard?lite=1")
    assert r2.status_code == 200
    assert len(r2.json().get("slots") or []) == 20
