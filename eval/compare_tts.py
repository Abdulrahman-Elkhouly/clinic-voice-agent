r"""Compare text-to-speech voices on the agent's own Arabic and English sentences, and write a report to listen to.

    python eval\compare_tts.py run       # synthesize every sentence with every setting (skips ones already done)
    python eval\compare_tts.py report    # write data\tts\report.html (open it in a browser)

Gemini is called directly (GEMINI_API_KEY), so its speaking style can be steered with an instruction.
The other providers go through LiveKit Inference (skipped with an error if the gateway can't be reached).
Everything goes to data\tts\ (gitignored).
"""
import asyncio
import base64
import json
import os
import sys
import time
import wave
from html import escape
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "tts"
AUDIO = OUT / "audio"
RESULTS = OUT / "results.json"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_GAP = 7.0   # seconds between Gemini requests (free tier rate limit)

# What the agent actually says: greeting, a booking read-back, digits, mixed language, safety, English
SENTENCES = [
    ("greeting", "مرحباً بك في مركز إزغاوة الطبي، كيف أقدر أساعدك اليوم؟"),
    ("booking", "تمام، الموعد مع الدكتور عمر بن علي يوم السبت سبعة عشر أكتوبر الساعة تسعة وعشرين دقيقة الصبح. "
                "هل هذا صحيح؟"),
    ("file_number", "رقم ملفك هو: أربعة، ثمانية، اثنين، واحد، ثلاثة، خمسة."),
    ("mixed", "موعدك مع Dr. Omar Benali، وتقدر تلغي الـ appointment قبل أربع ساعات بدون رسوم."),
    ("emergency", "إذا كان عنده ألم قوي في الصدر، اتصل على تسعة تسعة تسعة فوراً أو روح أقرب طوارئ."),
    ("english", "Your appointment with Dr. Omar Benali is confirmed for Saturday at nine twenty in the morning."),
]

GULF_STYLE = ("Read this aloud as a warm, calm, professional receptionist at a medical clinic in Qatar. "
              "Speak Arabic with a natural Gulf accent, at a relaxed pace, and read numbers clearly:")

# name -> settings. provider "gemini": model, voice, style (None = no instruction).
# provider "livekit": model, voice, language.
SETTINGS = {
    "gemini flash · Kore (plain)": {"provider": "gemini", "model": "gemini-3.8-flash-tts", "voice": "Kore",
                                    "style": None},
    "gemini flash · Kore (Gulf receptionist)": {"provider": "gemini", "model": "gemini-3.8-flash-tts",
                                                "voice": "Kore", "style": GULF_STYLE},
    "gemini flash · Charon (Gulf receptionist)": {"provider": "gemini", "model": "gemini-3.8-flash-tts",
                                                  "voice": "Charon", "style": GULF_STYLE},
    "gemini flash-lite · Kore (Gulf receptionist)": {"provider": "gemini", "model": "gemini-3.8-flash-lite-tts",
                                                     "voice": "Kore", "style": GULF_STYLE},
    # The user's custom voice (Voice design), through the Interactions API; text sent as is, no style
    "gemini flash · custom voice_g4nw2fwy9yn8": {"provider": "gemini-interactions", "model": "gemini-3.8-flash-tts",
                                                 "voice": "voice_g4nw2fwy9yn8"},
    # Through LiveKit Inference (add once the gateway is reachable again):
    # "cartesia sonic-3": {"provider": "livekit", "model": "cartesia/sonic-3", "voice": None, "language": "ar"},
    # "fish audio drama-3": {"provider": "livekit", "model": "fishaudio/drama-3", "voice": None, "language": "ar"},
    # "xai tts-1 · ara (ar-AE)": {"provider": "livekit", "model": "xai/tts-1", "voice": "ara", "language": "ar-AE"},
}


def save_wav(path: Path, pcm: bytes, rate: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def file_id(setting: str, sentence: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in setting).strip("_")
    return f"{safe}__{sentence}"


async def gemini(client: httpx.AsyncClient, s: dict, text: str) -> tuple[bytes, int, float]:
    """Streams the response, so 'first audio' is when the first audio chunk arrives. Returns pcm, rate, first ms."""
    prompt = f"{s['style']}\n{text}" if s["style"] else text
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": s["voice"]}}}}}
    for attempt in range(4):
        t0 = time.perf_counter()
        first, chunks, rate = None, [], 24000
        async with client.stream("POST", f"{GEMINI_URL}/{s['model']}:streamGenerateContent", params={"alt": "sse"},
                                 json=body, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}) as r:
            if r.status_code == 429:
                await r.aread()
                wait = 30 * (attempt + 1)
                print(f"   (rate limited, waiting {wait} s)")
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:200]!r}")
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                for part in json.loads(line[6:]).get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        first = first or time.perf_counter()
                        chunks.append(base64.b64decode(part["inlineData"]["data"]))
                        mime = part["inlineData"].get("mimeType", "")
                        if "rate=" in mime:
                            rate = int(mime.split("rate=")[1].split(";")[0])
        if not chunks:
            raise RuntimeError("no audio in the response")
        return b"".join(chunks), rate, (first - t0) * 1000
    raise RuntimeError("rate limited 4 times")


INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def _find_audio(node):
    """The first base64 audio payload anywhere in the response, with its mime type if given."""
    if isinstance(node, dict):
        for key in ("data", "audio", "inlineData", "inline_data"):
            value = node.get(key)
            if isinstance(value, str) and len(value) > 1000:
                return value, node.get("mime_type") or node.get("mimeType") or ""
        for value in node.values():
            found = _find_audio(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_audio(value)
            if found:
                return found
    return None


async def gemini_interactions(client: httpx.AsyncClient, s: dict, text: str, raw_path: Path) -> tuple[bytes, int, float]:
    """Custom voices (voice_...) go through the Interactions API. The raw response is saved before parsing, so a
    surprise in its format costs no extra request."""
    body = {"model": s["model"],
            "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": s["voice"]}]}}
    t0 = time.perf_counter()
    r = await client.post(INTERACTIONS_URL, json=body, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    total_ms = (time.perf_counter() - t0) * 1000
    raw_path.write_text(r.text, encoding="utf-8")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    found = _find_audio(r.json())
    if not found:
        raise RuntimeError(f"no audio found in the response (saved to {raw_path.name})")
    return (*audio_from(*found), total_ms)   # not streamed: "first audio" = the whole request


def audio_from(data: str, mime: str) -> tuple[bytes, int]:
    """Base64 audio -> (raw PCM, sample rate). The Interactions API returns a whole WAV file (mime audio/wav), the
    streaming API raw PCM (mime audio/L16;rate=...)."""
    import io
    blob = base64.b64decode(data)
    if blob[:4] == b"RIFF":
        with wave.open(io.BytesIO(blob)) as w:
            return w.readframes(w.getnframes()), w.getframerate()
    return blob, int(mime.split("rate=")[1].split(";")[0]) if "rate=" in mime else 24000


async def livekit(session, s: dict, text: str) -> tuple[bytes, int, float]:
    from livekit import rtc
    from livekit.agents import inference
    kwargs = {"language": s["language"]} | ({"voice": s["voice"]} if s["voice"] else {})
    tts = inference.TTS(model=s["model"], http_session=session, **kwargs)
    t0 = time.perf_counter()
    first, frames = None, []
    async for ev in tts.synthesize(text):
        first = first or time.perf_counter()
        frames.append(ev.frame)
    audio = rtc.combine_audio_frames(frames)
    return bytes(audio.data), audio.sample_rate, (first - t0) * 1000


async def run_all():
    import aiohttp
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    AUDIO.mkdir(parents=True, exist_ok=True)
    results = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    only_settings = [a for a in args if a not in dict(SENTENCES)]      # e.g. "custom"
    only_sentences = [a for a in args if a in dict(SENTENCES)]         # e.g. "greeting booking"
    async with httpx.AsyncClient(timeout=120) as client, aiohttp.ClientSession() as session:
        for name, s in SETTINGS.items():
            if only_settings and not any(o in name for o in only_settings):
                continue
            for sid, text in SENTENCES:
                if only_sentences and sid not in only_sentences:
                    continue
                key = file_id(name, sid)
                if results.get(key, {}).get("ok") and "--redo" not in sys.argv:
                    continue
                t0 = time.perf_counter()
                try:
                    if s["provider"] == "gemini":
                        pcm, rate, first_ms = await gemini(client, s, text)
                    elif s["provider"] == "gemini-interactions":
                        pcm, rate, first_ms = await gemini_interactions(client, s, text, OUT / f"{key}.raw.json")
                    else:
                        pcm, rate, first_ms = await livekit(session, s, text)
                    save_wav(AUDIO / f"{key}.wav", pcm, rate)
                    seconds = len(pcm) / 2 / rate
                    results[key] = {"ok": True, "first_ms": first_ms, "total_ms": (time.perf_counter() - t0) * 1000,
                                    "seconds": seconds}
                    print(f"  {name:45} {sid:12} first audio {first_ms:5.0f} ms, {seconds:4.1f} s of speech")
                except Exception as e:
                    results[key] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
                    print(f"  {name:45} {sid:12} FAILED {results[key]['error']}")
                RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
                if s["provider"].startswith("gemini"):
                    await asyncio.sleep(GEMINI_GAP)


def report():
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    head = "".join(f"<th>{escape(n)}</th>" for n in SETTINGS)
    summary = []
    for name in SETTINGS:
        rs = [results.get(file_id(name, sid), {}) for sid, _ in SENTENCES]
        firsts = [r["first_ms"] for r in rs if r.get("ok")]
        summary.append(f"<tr><td>{escape(name)}</td><td>{sum(r.get('ok', False) for r in rs)}/{len(rs)}</td>"
                       f"<td>{f'{np.median(firsts):.0f} ms' if firsts else '–'}</td></tr>")
    rows = []
    for sid, text in SENTENCES:
        cells = []
        for name in SETTINGS:
            key = file_id(name, sid)
            r = results.get(key, {})
            if r.get("ok"):
                cells.append(f"<td><audio controls preload='none' src='audio/{key}.wav'></audio>"
                             f"<div class='m'>first audio {r['first_ms']:.0f} ms · {r['seconds']:.1f} s</div></td>")
            else:
                cells.append(f"<td class='m'>{escape(r.get('error', 'not run'))}</td>")
        rows.append(f"<tr><td><b>{escape(sid)}</b><div class='t' dir='auto'>{escape(text)}</div></td>"
                    f"{''.join(cells)}</tr>")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>TTS comparison</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --head:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f2f2f7; --muted:#a1a1a6; --line:#3a3a3c; --head:#2c2c2e; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:24px; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:32px; }}
th, td {{ border:1px solid var(--line); padding:8px; vertical-align:top; text-align:start; }}
th {{ background:var(--head); }} .t {{ font-size:17px; margin-top:4px; }}
.m {{ color:var(--muted); font-size:13px; margin-top:4px; }} audio {{ width:220px; }}
</style></head><body>
<h1>Text-to-speech comparison</h1>
<p>Listen for: a natural Gulf-friendly accent, correct pronunciation of names (Benali, Izghawa), digits read one by one,
English words inside Arabic, and a calm receptionist tone. "First audio" is how long until the voice starts:
the caller waits that long after the agent decides what to say.</p>
<p><b>Gulf receptionist</b> settings give Gemini this instruction before the text: <i>{escape(GULF_STYLE)}</i></p>
<h2>Summary</h2>
<table><tr><th>Setting</th><th>Sentences</th><th>First audio (median)</th></tr>{''.join(summary)}</table>
<h2>Sentences</h2>
<table><tr><th>What the agent says</th>{head}</tr>{''.join(rows)}</table>
</body></html>"""
    (OUT / "report.html").write_text(html, encoding="utf-8")
    print(f"Report: {OUT / 'report.html'}")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    if step == "run":
        asyncio.run(run_all())
    elif step == "report":
        report()
    else:
        print(__doc__)
