"""Tests for the custom-voice TTS adapter (agent/gemini_tts.py) against a fake Gemini server: no network, no quota.
They check what fights the delay: sentences generated ahead and played in order, the filler when the first sound is
slow, and the fallback voice when the custom voice fails or is silent.

    python -m pytest tests -v
"""
import asyncio
import base64
import json
import sys
import time
import wave
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import gemini_tts  # noqa: E402
from gemini_tts import GREETING_TEXT, GeminiTTS, _pcm_chunks_from_sse  # noqa: E402

S1 = "This is the first sentence of the reply."
S2 = "And this is the second sentence that follows it."
TONE = {S1: b"\x01\x00", S2: b"\x02\x00"}   # each sentence's audio is a distinct sample value


def sse(objs) -> bytes:
    return b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objs)


def audio_event(pcm: bytes) -> dict:          # Interactions API (custom voice)
    return {"event_type": "step.delta", "delta": {"type": "audio", "data": base64.b64encode(pcm).decode()}}


def inline_event(pcm: bytes) -> dict:         # streamGenerateContent (fallback voice)
    return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;rate=24000",
                                                                  "data": base64.b64encode(pcm).decode()}}]}}]}


class FakeGemini:
    """Answers both endpoints. `delays` = seconds before the custom voice's first sound, per sentence text."""

    def __init__(self, delays=None, custom_status=200, fallback_samples=b"\x09\x00"):
        self.delays = delays or {}
        self.custom_status = custom_status
        self.fallback_samples = fallback_samples
        self.log = []   # (endpoint, text, start time, end time)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/interactions"):
            text = body["input"][0]["content"][0]["text"]
            if self.custom_status != 200:
                self.log.append(("custom", text, time.perf_counter(), time.perf_counter()))
                return httpx.Response(self.custom_status, json={"error": "nope"})
            entry = ["custom", text, time.perf_counter(), None]
            self.log.append(entry)

            async def stream():
                await asyncio.sleep(self.delays.get(text, 0))
                for _ in range(3):
                    yield sse([audio_event(TONE.get(text, b"\x05\x00") * 2400)])   # 3 x 0.1 s
                entry[3] = time.perf_counter()
            return httpx.Response(200, content=stream(), headers={"content-type": "text/event-stream"})
        text = body["contents"][0]["parts"][0]["text"]
        self.log.append(("fallback", text, time.perf_counter(), time.perf_counter()))
        return httpx.Response(200, content=sse([inline_event(self.fallback_samples * 2400)]),
                              headers={"content-type": "text/event-stream"})


def make_tts(fake: FakeGemini, tmp_path) -> GeminiTTS:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return GeminiTTS(voice="voice_test", api_key="test", client=client, cache_dir=tmp_path)


def record(tts: GeminiTTS, text: str, sample: bytes = b"\x07\x00", seconds: float = 0.2):
    path = tts._clip_path(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(gemini_tts.SAMPLE_RATE)
        w.writeframes(sample * int(seconds * gemini_tts.SAMPLE_RATE))


async def speak(tts: GeminiTTS, *sentences: str) -> bytes:
    stream = tts.stream()
    for s in sentences:
        stream.push_text(s + " ")
    stream.end_input()
    audio = b"".join([bytes(ev.frame.data) async for ev in stream])
    await stream.aclose()
    return audio


def order_of(audio: bytes) -> list[bytes]:
    """The sequence of distinct sample values, e.g. [filler, sentence 1, sentence 2]."""
    seq = []
    for i in range(0, len(audio), 2):
        s = audio[i:i + 2]
        if s != b"\x00\x00" and (not seq or seq[-1] != s):
            seq.append(s)
    return seq


# --- parsing ---

def test_audio_is_found_in_both_response_formats():
    lines = ["event: step.delta", "data: " + json.dumps(audio_event(b"ab")), "data: [DONE]",
             "data: " + json.dumps(inline_event(b"cd"))]
    assert _pcm_chunks_from_sse(lines) == [b"ab", b"cd"]


# --- one sentence ---

@pytest.mark.asyncio
async def test_custom_voice_is_used_when_it_works(tmp_path):
    fake = FakeGemini()
    tts = make_tts(fake, tmp_path)
    assert order_of(await tts.full_audio(S1)) == [TONE[S1]]
    assert [e[0] for e in fake.log] == ["custom"]


@pytest.mark.asyncio
async def test_an_error_falls_back_to_the_prebuilt_voice(tmp_path):
    fake = FakeGemini(custom_status=429)
    tts = make_tts(fake, tmp_path)
    assert order_of(await tts.full_audio(S1)) == [b"\x09\x00"]
    assert [e[0] for e in fake.log] == ["custom", "fallback"]


@pytest.mark.asyncio
async def test_silence_past_the_timeout_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_tts, "FIRST_AUDIO_TIMEOUT", 0.1)
    fake = FakeGemini(delays={S1: 1.0})
    tts = make_tts(fake, tmp_path)
    assert order_of(await tts.full_audio(S1)) == [b"\x09\x00"]


# --- a reply of several sentences ---

@pytest.mark.asyncio
async def test_the_next_sentence_generates_while_the_first_is_still_coming(tmp_path):
    fake = FakeGemini(delays={S1: 0.4, S2: 0.0})
    tts = make_tts(fake, tmp_path)
    audio = await speak(tts, S1, S2)
    starts = {text: start for _, text, start, _ in fake.log}
    first_done = next(end for _, text, _, end in fake.log if text == S1)
    assert starts[S2] < first_done                       # requested before sentence 1 finished
    assert order_of(audio) == [TONE[S1], TONE[S2]]       # but played in order


@pytest.mark.asyncio
async def test_a_slow_first_sound_gets_a_filler_in_the_reply_language(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_tts, "FILLER_AFTER", 0.05)
    fake = FakeGemini(delays={S1: 0.4})
    tts = make_tts(fake, tmp_path)
    record(tts, gemini_tts.FILLERS["en"][0], sample=b"\x07\x00")
    record(tts, gemini_tts.FILLERS["ar"][0], sample=b"\x08\x00")
    assert order_of(await speak(tts, S1, S2)) == [b"\x07\x00", TONE[S1], TONE[S2]]   # English filler, once


@pytest.mark.asyncio
async def test_a_fast_first_sound_gets_no_filler(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_tts, "FILLER_AFTER", 0.5)
    fake = FakeGemini()
    tts = make_tts(fake, tmp_path)
    record(tts, gemini_tts.FILLERS["en"][0])
    assert order_of(await speak(tts, S1)) == [TONE[S1]]


def test_fillers_follow_the_language_and_never_repeat_back_to_back(tmp_path):
    tts = make_tts(FakeGemini(), tmp_path)
    assert tts.next_filler("Your appointment is booked.") is None   # nothing recorded yet
    for i, text in enumerate(gemini_tts.FILLERS["ar"][:2]):
        record(tts, text, sample=bytes([10 + i, 0]))
    picks = [tts.next_filler("تمام، حجزت لك الموعد")[:2] for _ in range(4)]
    assert picks == [b"\x0a\x00", b"\x0b\x00", b"\x0a\x00", b"\x0b\x00"]


def test_the_greeting_plays_from_disk_once_recorded(tmp_path):
    tts = make_tts(FakeGemini(), tmp_path)
    assert tts.greeting_frames() is None
    record(tts, GREETING_TEXT)
    assert tts.greeting_frames() is not None


@pytest.mark.asyncio
async def test_an_interrupted_reply_stops_its_requests(tmp_path):
    fake = FakeGemini(delays={S1: 5.0, S2: 5.0})
    tts = make_tts(fake, tmp_path)
    stream = tts.stream()
    stream.push_text(S1 + " " + S2 + " ")
    stream.end_input()
    await asyncio.sleep(0.2)
    t0 = time.perf_counter()
    await stream.aclose()                      # what LiveKit does when the caller interrupts
    assert time.perf_counter() - t0 < 1.0      # didn't wait for the 5 s responses
