"""Which speech-to-text the clinic uses, in one place: the live agent and eval/stt_real.py both call make_stt(),
so the test hears exactly what a call would.

STT_PROVIDER picks it (default soniox, setting F of eval/stt_real.py, the user's choice 2026-10-25; Soniox only,
no fallback), both bilingual Arabic + English in one stream:
- speechmatics: the LiveKit plugin (SPEECHMATICS_API_KEY). Turns end the plugin's default way: a local Silero VAD
  calls finalize() when the caller stops speaking.
- soniox: the LiveKit plugin (SONIOX_API_KEY), model stt-rt-v5. Soniox detects the end of speech itself; with
  finalize, a local Silero VAD also sends Soniox "finalize" when the caller stops speaking (manual finalization),
  so a lone "لا" isn't held back waiting for Soniox to be sure the sentence is over (up to 2 s).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROVIDER = os.environ.get("STT_PROVIDER", "soniox")
API_KEYS = {"soniox": "SONIOX_API_KEY", "speechmatics": "SPEECHMATICS_API_KEY"}
LANGUAGE = "ar_en"   # Speechmatics: one model for Gulf Arabic, English, and both mixed in a sentence

# Clinic words the STT should expect (doctor and insurer names, words callers mix in). Whether they help is
# measured by eval/stt_real.py (settings B, D and F); the agent sends them (USE_VOCAB, chosen with F).
VOCAB = [
    {"content": "Benali", "sounds_like": ["بن علي", "Ben Ali"]},
    {"content": "Al-Sulaiti", "sounds_like": ["السليطي"]},
    {"content": "Al-Kuwari", "sounds_like": ["الكواري"]},
    {"content": "Al-Mahmoud", "sounds_like": ["المحمود"]},
    {"content": "Qureshi"}, {"content": "Okafor"}, {"content": "Haddad"},
    {"content": "Izghawa"}, {"content": "Gulf Shield"}, {"content": "Doha Care"},
    {"content": "appointment"}, {"content": "booking"}, {"content": "dermatologist"},
    {"content": "موعد"}, {"content": "رقم الملف"}, {"content": "دكتور"}, {"content": "تأمين"},
]
USE_VOCAB = True

# Soniox only: a description of the conversation (its docs: start with "general"), plus Gulf words for short
# replies. Setting F of eval/stt_real.py, which the agent uses.
DESCRIPTION = {
    "domain": "Healthcare",
    "topic": "Booking and managing clinic appointments; patient file numbers, phone numbers, dates of birth",
    "setting": "Phone call to a clinic receptionist",
    "location": "Qatar",
    "organization": "Izghawa Medical Center",
    "language": "Gulf Arabic (Qatari dialect) and English, often mixed in one sentence",
}
ARABIC_TERMS = ["ايه", "ايوه"]
SONIOX_FINALIZE = True   # setting F (E and F both finalize)
SONIOX_DESCRIBE = True


def check_key(provider: str = PROVIDER):
    """Raise at startup, with the fix, if the STT's key is missing (a call would otherwise never hear the caller)."""
    if provider not in API_KEYS:
        raise ValueError(f"Unknown STT_PROVIDER: {provider}")
    if not os.environ.get(API_KEYS[provider]):
        raise RuntimeError(f"{API_KEYS[provider]} is not set: add it to .env (speech-to-text: {provider}).")


def make_stt(vocab: bool = USE_VOCAB, provider: str = PROVIDER, http_session=None,
             finalize: bool = SONIOX_FINALIZE, describe: bool = SONIOX_DESCRIBE, vad=None):
    """The caller's speech-to-text for an AgentSession. http_session: only needed outside a LiveKit job (tests).
    finalize, describe: Soniox only (see above). vad: the session's VAD, so finalize doesn't load a second one."""
    if provider == "soniox":
        from livekit.plugins import soniox
        # Soniox takes plain terms (no "sounds like"), so the same words without the spoken forms.
        terms = [v["content"] for v in VOCAB] if vocab else []
        terms += ARABIC_TERMS if describe else []
        general = [soniox.ContextGeneralItem(k, v) for k, v in DESCRIPTION.items()] if describe else None
        context = soniox.ContextObject(general=general, terms=terms or None) if terms or general else None
        params = soniox.STTOptions(language_hints=["ar", "en"], enable_language_identification=True,
                                   context=context)
        if finalize:
            if vad is None:
                from livekit.plugins.silero import VAD
                vad = VAD.load()
            return _soniox_finalizing(soniox)(http_session=http_session, params=params, vad=vad)
        return soniox.STT(http_session=http_session, params=params)
    if provider != "speechmatics":
        raise ValueError(f"Unknown STT_PROVIDER: {provider}")
    from livekit.plugins import speechmatics

    class Bilingual(speechmatics.STT):
        # The constructor turns "ar_en" into "ar-EN" (read as a region code), which the service rejects;
        # a per-stream language is passed through unchanged.
        def stream(self, *, language=None, **kwargs):
            return super().stream(language=language or LANGUAGE, **kwargs)

    # One caller per call, so no speaker labels.
    return Bilingual(language=LANGUAGE, enable_diarization=False, additional_vocab=VOCAB if vocab else [])


def _soniox_finalizing(soniox):
    """Soniox STT whose streams send "finalize" when a local VAD hears the end of speech, the way the Speechmatics
    plugin drives its finalize() (LiveKit doesn't tell the STT when the caller stopped)."""
    import asyncio

    from livekit import rtc
    from livekit.agents import utils
    from livekit.agents.vad import VADEventType
    from livekit.plugins.soniox.stt import SpeechStream

    class FinalizingStream(SpeechStream):
        def __init__(self, *, vad, **kwargs):
            super().__init__(**kwargs)
            self._vad = vad

        async def _prepare_audio_task(self) -> None:   # the plugin's version, plus the VAD
            vad_stream = self._vad.stream()

            async def on_end_of_speech():
                async for ev in vad_stream:
                    if ev.type == VADEventType.END_OF_SPEECH:
                        self.audio_queue.put_nowait('{"type": "finalize"}')   # sent in order, after the audio

            watcher = asyncio.create_task(on_end_of_speech())
            try:
                async for data in self._input_ch:
                    if isinstance(data, rtc.AudioFrame):
                        self.audio_queue.put_nowait(data.data.tobytes())
                        vad_stream.push_frame(data)
            finally:
                await utils.aio.gracefully_cancel(watcher)
                await vad_stream.aclose()

    class FinalizingSTT(soniox.STT):
        def __init__(self, *, vad, **kwargs):
            super().__init__(**kwargs)
            self._vad = vad

        def stream(self, *, language=None, conn_options=None, **kwargs):
            from livekit.agents import DEFAULT_API_CONNECT_OPTIONS
            return FinalizingStream(stt=self, vad=self._vad, conn_options=conn_options or DEFAULT_API_CONNECT_OPTIONS)

    return FinalizingSTT
