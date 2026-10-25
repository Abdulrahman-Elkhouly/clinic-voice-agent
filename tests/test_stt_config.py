"""The agent's speech-to-text setup (stt_config.py): Soniox, setting F. Offline: nothing connects.

    python -m pytest tests -v
"""
import sys
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import stt_config  # noqa: E402
from stt_config import check_key, make_stt  # noqa: E402


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("SONIOX_API_KEY", "test")
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "test")


class FakeVAD:
    pass


@pytest.mark.asyncio
async def test_agent_default_is_soniox_setting_f():
    async with aiohttp.ClientSession() as http:
        stt = make_stt(http_session=http, vad=FakeVAD())
    params = stt._params
    assert stt_config.PROVIDER == "soniox" and type(stt).__name__ == "FinalizingSTT"
    assert params.model == "stt-rt-v5" and params.language_hints == ["ar", "en"]
    general = {item.key: item.value for item in params.context.general}
    assert general["setting"] == "Phone call to a clinic receptionist" and general["location"] == "Qatar"
    assert "Benali" in params.context.terms and "ايه" in params.context.terms


@pytest.mark.asyncio
async def test_finalize_uses_the_sessions_vad():
    vad = FakeVAD()
    async with aiohttp.ClientSession() as http:
        assert make_stt(http_session=http, vad=vad)._vad is vad   # no second VAD loaded


@pytest.mark.asyncio
async def test_plain_soniox_has_no_context_and_no_finalize():
    async with aiohttp.ClientSession() as http:
        stt = make_stt(vocab=False, finalize=False, describe=False, http_session=http)
    assert type(stt).__name__ == "STT" and stt._params.context is None


def test_missing_key_stops_startup(monkeypatch):
    monkeypatch.delenv("SONIOX_API_KEY")
    with pytest.raises(RuntimeError, match="SONIOX_API_KEY"):
        check_key("soniox")


def test_unknown_provider_is_refused():
    with pytest.raises(ValueError):
        check_key("deepgram")
