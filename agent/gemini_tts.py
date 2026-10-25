"""Text-to-speech with the clinic's custom Gemini voice (Voice design), streamed through the Interactions API.

Custom voices only work through the Interactions API, where the first sound took ~2.8 s (measured 2026-10-21).
The delay is fought in three ways:
- Sentences are prepared ahead: every sentence's request starts as soon as the sentence exists, so while one plays
  the next is already generating; only the first sentence of a reply waits.
- If a reply's first sound isn't ready after FILLER_AFTER seconds, a short pre-recorded filler in the reply's
  language plays first ("لحظة من فضلك" / "One moment"), at most once per reply, never the same twice in a row.
- A sentence that errors, hits a limit, or has no sound after FIRST_AUDIO_TIMEOUT seconds is spoken with the
  prebuilt fallback voice through the fast endpoint, so the call never goes silent.
Fixed lines (the greeting, the fillers) are pre-recorded once with the custom voice and played from disk.
"""
import asyncio
import base64
import json
import logging
import os
import uuid
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from livekit import rtc
from livekit.agents import tokenize, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

log = logging.getLogger("gemini_tts")

SAMPLE_RATE = 24000
MODEL = "gemini-3.8-flash-tts"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
FIRST_AUDIO_TIMEOUT = 6.0   # seconds without any sound before a sentence falls back to the prebuilt voice
FILLER_AFTER = 1.0          # seconds without the reply's first sound before a filler plays (the user's call)
PREFETCH = 3                # sentences generating at once, ahead of the one playing
# Pre-recorded fixed lines are committed (no patient data), so every deployment built from the repo has them
CACHE_DIR = Path(__file__).resolve().parent.parent / "assets" / "voice"

# Pre-recorded fixed lines, per language. The greeting is bilingual (decided 2026-10-19), so it's one clip.
GREETING_TEXT = ("مركز ازغوى الطبي، شلون اقدر اساعدك؟ "   # the user's wording (2026-10-21)
                 "Welcome to Izghawa Medical Center, how can I help you?")
FILLERS = {
    "ar": ["لحظة من فضلك.", "ثواني بس.", "خلني أشوف لك."],
    "en": ["One moment, please.", "Just a second.", "Let me check that for you."],
}


# The agent can't hear the caller any more (speech-to-text down after LiveKit's retries): the user's wording
# (2026-10-25). Played before transferring (Stage 6; until then the call ends).
STT_DOWN = {
    "ar": "عذراً، عندنا مشكلة تقنية، بحولك لموظف.",
    "en": "Sorry, we have a technical problem, I'll transfer you to a member of staff.",
}


def fixed_lines() -> list[str]:
    """Every line the agent plays from a recording: greeting, fillers, silence check-ins, the STT-down line."""
    from silence import ALL_LINES as SILENCE_LINES
    return [GREETING_TEXT] + FILLERS["ar"] + FILLERS["en"] + SILENCE_LINES + [STT_DOWN["ar"], STT_DOWN["en"]]


def is_arabic(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return sum("؀" <= ch <= "ۿ" for ch in letters) > len(letters) / 2


def _pcm_chunks_from_sse(lines: list[str]) -> list[bytes]:
    """Audio pieces in one server-sent-events message: any {"type": "audio", "data": base64} (Interactions API) or
    {"inlineData": {"data": base64}} (generateContent) anywhere in the JSON."""
    chunks, stack = [], []
    for line in lines:
        if not line.startswith("data: "):
            continue
        try:
            stack.append(json.loads(line[6:]))
        except json.JSONDecodeError:   # e.g. the end-of-stream marker
            continue
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            if node.get("type") == "audio" and isinstance(node.get("data"), str):
                chunks.append(base64.b64decode(node["data"]))
            elif isinstance(node.get("inlineData"), dict) and isinstance(node["inlineData"].get("data"), str):
                chunks.append(base64.b64decode(node["inlineData"]["data"]))
            else:
                stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return chunks


class GeminiTTS(tts.TTS):
    def __init__(self, voice: str, *, fallback_voice: str = "Kore", api_key: str | None = None,
                 client: httpx.AsyncClient | None = None, cache_dir: Path = CACHE_DIR):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=True), sample_rate=SAMPLE_RATE, num_channels=1)
        self.voice = voice
        self.fallback_voice = fallback_voice
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._client = client
        self._cache_dir = cache_dir / voice
        self._last_filler: str | None = None

    @property
    def model(self) -> str:
        return MODEL

    @property
    def provider(self) -> str:
        return "google-gemini"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:   # one connection per call, reused by every sentence
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), http2=False)
        return self._client

    def prewarm(self) -> None:
        self._http()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def synthesize(self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
                   ) -> "GeminiChunkedStream":
        return GeminiChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS) -> "GeminiStream":
        return GeminiStream(tts=self, conn_options=conn_options)

    # --- one sentence ---

    async def _stream_sse(self, url: str, body: dict, params: dict | None = None) -> AsyncIterator[bytes]:
        async with self._http().stream("POST", url, json=body, params=params,
                                       headers={"x-goog-api-key": self._api_key}) as r:
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200]!r}")
            message: list[str] = []
            async for line in r.aiter_lines():
                if line:
                    message.append(line)
                    continue
                for chunk in _pcm_chunks_from_sse(message):   # a blank line ends one event
                    yield chunk
                message = []
            for chunk in _pcm_chunks_from_sse(message):
                yield chunk

    def _custom_voice(self, text: str) -> AsyncIterator[bytes]:
        body = {"model": MODEL, "stream": True,
                "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
                "response_format": {"type": "audio"},
                "generation_config": {"speech_config": [{"voice": self.voice}]}}
        return self._stream_sse(f"{BASE_URL}/interactions", body)

    def _fallback(self, text: str) -> AsyncIterator[bytes]:
        body = {"contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": self.fallback_voice}}}}}
        return self._stream_sse(f"{BASE_URL}/models/{MODEL}:streamGenerateContent", body, params={"alt": "sse"})

    async def sentence_audio(self, text: str, out: asyncio.Queue) -> None:
        """Puts the sentence's PCM pieces on `out`, then None. Custom voice first; the fallback voice if it fails
        or stays silent for FIRST_AUDIO_TIMEOUT seconds. Sentence text is never logged (it may hold patient data)."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        sent_any = False
        stream = self._custom_voice(text)
        try:
            first = await asyncio.wait_for(stream.__anext__(), FIRST_AUDIO_TIMEOUT)
            log.info("custom voice: first sound after %.0f ms", (loop.time() - started) * 1000)
            await out.put(first)
            sent_any = True
            async for chunk in stream:
                await out.put(chunk)
        except (asyncio.TimeoutError, StopAsyncIteration, RuntimeError, httpx.HTTPError) as e:
            if sent_any:   # failed mid-sentence: can't splice another voice in, so the sentence just ends
                log.warning("custom voice failed mid-sentence: %s", type(e).__name__)
            else:
                log.warning("custom voice failed (%s): using %s for this sentence", type(e).__name__,
                            self.fallback_voice)
                try:
                    async for chunk in self._fallback(text):
                        await out.put(chunk)
                except (RuntimeError, httpx.HTTPError) as e2:
                    log.error("fallback voice failed too: %s", type(e2).__name__)
        finally:
            await stream.aclose()   # closes the HTTP connection of an abandoned request
            await out.put(None)

    async def full_audio(self, text: str) -> bytes:
        queue: asyncio.Queue = asyncio.Queue()
        await self.sentence_audio(text, queue)
        pieces = []
        while (chunk := queue.get_nowait()) is not None:
            pieces.append(chunk)
        return b"".join(pieces)

    # --- pre-recorded fixed lines ---

    def _clip_path(self, text: str) -> Path:
        return self._cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, text).hex}.wav"

    def cached_clip(self, text: str) -> bytes | None:
        path = self._clip_path(text)
        if not path.exists():
            return None
        with wave.open(str(path)) as w:
            return w.readframes(w.getnframes())

    async def record_clip(self, text: str) -> bytes:
        """Generate a fixed line once with the custom voice and save it (prerecord_voice.py calls this)."""
        pcm = await self.full_audio(text)
        path = self._clip_path(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        return pcm

    def next_filler(self, reply_text: str) -> bytes | None:
        """A pre-recorded filler in the reply's language, never the same as last time; None if none recorded."""
        recorded = [t for t in FILLERS["ar" if is_arabic(reply_text) else "en"] if self.cached_clip(t) is not None]
        if not recorded:
            return None
        nxt = (recorded.index(self._last_filler) + 1) % len(recorded) if self._last_filler in recorded else 0
        if recorded[nxt] == self._last_filler:   # only one recorded: don't repeat it back to back
            return None
        self._last_filler = recorded[nxt]
        return self.cached_clip(self._last_filler)

    def clip_frames(self, text: str) -> AsyncIterator[rtc.AudioFrame] | None:
        """A pre-recorded line as audio frames for session.say, or None if it isn't recorded."""
        pcm = self.cached_clip(text)
        return None if pcm is None else _frames(pcm)

    def greeting_frames(self) -> AsyncIterator[rtc.AudioFrame] | None:
        return self.clip_frames(GREETING_TEXT)

    def missing_clips(self) -> list[str]:
        """Fixed lines with no recording (a deployment without them works, but slower and with no fillers)."""
        return [t for t in fixed_lines() if not self._clip_path(t).exists()]


async def _frames(pcm: bytes, ms: int = 100) -> AsyncIterator[rtc.AudioFrame]:
    step = SAMPLE_RATE * ms // 1000 * 2
    for i in range(0, len(pcm), step):
        piece = pcm[i:i + step]
        yield rtc.AudioFrame(piece, SAMPLE_RATE, 1, len(piece) // 2)


class GeminiChunkedStream(tts.ChunkedStream):
    """One whole text at once (used by session.say and for fixed lines)."""

    def __init__(self, *, tts: GeminiTTS, input_text: str, conn_options: APIConnectOptions):
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._gemini = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=SAMPLE_RATE, num_channels=1,
                                  mime_type="audio/pcm")
        pcm = self._gemini.cached_clip(self.input_text) or await self._gemini.full_audio(self.input_text)
        output_emitter.push(pcm)
        output_emitter.flush()


class GeminiStream(tts.SynthesizeStream):
    """One reply: text arrives token by token from the LLM, is split into sentences (LiveKit's default splitter),
    and every sentence starts generating at once (up to PREFETCH ahead) while audio plays in order."""

    def __init__(self, *, tts: GeminiTTS, conn_options: APIConnectOptions):
        super().__init__(tts=tts, conn_options=conn_options)
        self._gemini = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(request_id=utils.shortuuid(), sample_rate=SAMPLE_RATE, num_channels=1,
                                  mime_type="audio/pcm", stream=True)
        sentences = tokenize.blingfire.SentenceTokenizer().stream()
        pending: asyncio.Queue = asyncio.Queue()   # (sentence text, its audio queue), in reply order
        slots = asyncio.Semaphore(PREFETCH)
        workers: list[asyncio.Task] = []

        async def read_input() -> None:
            async for data in self._input_ch:
                if isinstance(data, self._FlushSentinel):
                    sentences.flush()
                else:
                    sentences.push_text(data)
            sentences.end_input()

        async def start_sentences() -> None:
            async for ev in sentences:
                text = ev.token.strip()
                if not text:
                    continue
                audio: asyncio.Queue = asyncio.Queue()
                await slots.acquire()

                async def work(text=text, audio=audio) -> None:
                    try:
                        await self._gemini.sentence_audio(text, audio)
                    finally:
                        slots.release()
                workers.append(asyncio.create_task(work()))
                await pending.put((text, audio))
            await pending.put(None)

        async def play() -> None:
            segment_started = False
            first_of_reply = True
            while (item := await pending.get()) is not None:
                text, audio = item
                if not segment_started:
                    output_emitter.start_segment(segment_id=utils.shortuuid())
                    self._mark_started()
                    segment_started = True
                if first_of_reply:
                    first_of_reply = False
                    try:
                        chunk = await asyncio.wait_for(audio.get(), FILLER_AFTER)
                    except asyncio.TimeoutError:
                        filler = self._gemini.next_filler(text)
                        if filler:
                            log.info("filler played (voice not ready after %.1f s)", FILLER_AFTER)
                            output_emitter.push(filler)
                        chunk = await audio.get()
                    if chunk is not None:
                        output_emitter.push(chunk)
                    else:
                        continue
                while (chunk := await audio.get()) is not None:
                    output_emitter.push(chunk)
            if segment_started:
                output_emitter.end_segment()

        tasks = [asyncio.create_task(read_input()), asyncio.create_task(start_sentences()),
                 asyncio.create_task(play())]
        try:
            await asyncio.gather(*tasks)
        finally:   # also on interruption: stop every request still generating
            await sentences.aclose()
            await utils.aio.cancel_and_wait(*tasks, *workers)
