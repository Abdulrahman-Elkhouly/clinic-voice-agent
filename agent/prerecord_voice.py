r"""Pre-record the agent's fixed lines (greeting, fillers, silence check-ins) in the custom Gemini voice.
Run once, and again only after changing a line or the voice. One Gemini request per line not yet recorded.

    python agent\prerecord_voice.py           # record what's missing
    python agent\prerecord_voice.py --list    # show what's recorded, without any request

Clips go to assets\voice\<voice>\ and are committed (fixed clinic phrases, no patient data), so every deployment
built from the repository has them.
"""
import asyncio
import sys

from dotenv import load_dotenv

from agent import TTS_VOICE
from gemini_tts import SAMPLE_RATE, GeminiTTS, fixed_lines

load_dotenv()
LINES = fixed_lines()


async def main():
    tts = GeminiTTS(voice=TTS_VOICE)
    try:
        missing = [t for t in LINES if tts.cached_clip(t) is None]
        for text in LINES:
            clip = tts.cached_clip(text)
            print(f"  {'recorded' if clip else 'missing '}  {len(clip) / 2 / SAMPLE_RATE if clip else 0:4.1f} s  {text}")
        if "--list" in sys.argv or not missing:
            return
        print(f"Recording {len(missing)} line(s), one request each...")
        for text in missing:
            pcm = await tts.record_clip(text)
            print(f"  recorded {len(pcm) / 2 / SAMPLE_RATE:4.1f} s  {text}")
    finally:
        await tts.aclose()


if __name__ == "__main__":
    asyncio.run(main())
