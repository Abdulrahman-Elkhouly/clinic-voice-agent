r"""Test speech-to-text on Gulf Arabic, mixed Arabic-English, and clinic phrases, and write a report to listen to.

    python eval\compare_stt.py fetch    # download clips and generate the clinic ones (once)
    python eval\compare_stt.py run      # transcribe every clip with each STT setting
    python eval\compare_stt.py report   # write data\stt\report.html (open it in a browser)
    python eval\compare_stt.py run --quick deepgram groq   # only some settings, on the smaller set (QUICK)
    python eval\compare_stt.py report --quick

Everything goes to data\stt\ (gitignored). The Mixat and SADA clips are CC BY-NC-SA (non-commercial): used here only to
evaluate providers internally, never committed or published.
"""
import asyncio
import json
import random
import re
import sys
import time
import wave
from html import escape
from pathlib import Path

import av
import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "stt"
CLIPS = OUT / "clips"
MANIFEST = OUT / "manifest.json"
RESULTS = OUT / "results.json"
RATE = 16000
ROWS_API = "https://datasets-server.huggingface.co/rows"

# STT settings to compare: name -> (model, language or None for the provider's default).
# "groq/..." models aren't streaming: the whole utterance is sent after the caller stops (latency = that request).
# Dropped by the user after the first rounds: ink-whisper (unset), groq whisper-large-v3 (unset / ar).
# "speechmatics" uses the LiveKit plugin (SPEECHMATICS_API_KEY); a third value is the domain.
SETTINGS = {
    "ink-whisper (ar)": ("cartesia/ink-whisper", "ar"),
    "deepgram nova-3 (ar-QA)": ("deepgram/nova-3", "ar-QA"),
    "speechmatics (ar_en)": ("speechmatics", "ar_en"),
    # "speechmatics (ar_en, medical)": rejected by the agent endpoint the plugin uses
    #   ("Domain medical is not supported for ar_en-linden-1").
    # Not in agents 1.8.3's model list, but the gateway serves it; it needs 24 kHz (LiveKit resamples).
    "gpt-live-transcribe (unset)": ("openai/gpt-live-transcribe", None, {"sample_rate": 24000}),
    # Soniox plugin (SONIOX_API_KEY): one model for both languages, with language identification
    "soniox (ar+en)": ("soniox", "ar,en"),
    # Speechmatics' own real-time API directly (SPEECHMATICS_API_KEY; the plugin only reaches the "agent" endpoint,
    # which rejected the medical domain). Third value: TranscriptionConfig options.
    "speechmatics RT enhanced (ar_en)": ("speechmatics-rt", "ar_en", {"model": "enhanced"}),
    "speechmatics RT enhanced medical (ar_en)": ("speechmatics-rt", "ar_en", {"model": "enhanced", "domain": "medical"}),
}

# Clinic words for Speechmatics' additional_vocab (the vocabulary variant, once the probe shows which model works)
CLINIC_VOCAB = ["Benali", "Al-Kuwari", "Al-Sulaiti", "Okafor", "Qureshi", "Izghawa", "موعد", "رقم الملف", "دكتور",
                "appointment", "booking"]
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_GAP = 3.5   # seconds between Groq requests (free tier: about 20 requests a minute)
LEADING_SILENCE = 0.5    # seconds of quiet line before the speech
TRAILING_SILENCE = 2.0   # seconds of quiet line after it, as when a caller stops talking
NOISE_FLOOR = 60         # amplitude of the line's background hiss (int16), well below speech
PARALLEL = 4             # streams at once
WAIT_AFTER = 5.0         # seconds to wait for final transcripts after the audio ends

# Clinic phrases (generated with Cartesia's Arabic voice). "expect" is what a correct transcript must contain.
CLINIC = [
    ("book", "أبي أحجز موعد عند دكتور العظام يوم السبت الصبح", "أحجز موعد عند دكتور العظام يوم السبت الصبح"),
    ("file_number", "رقم ملفي أربعة، ثمانية، اثنين، واحد، ثلاثة، خمسة", "482135 (as digits or words)"),
    ("phone", "رقم تلفوني خمسة خمسة واحد اثنين، ثلاثة أربعة خمسة ستة", "55123456 (as digits or words)"),
    ("dob", "تاريخ ميلادي الرابع عشر من مارس ألف وتسعمائة وتسعين", "14 March 1990"),
    ("mixed_doctor", "عندي appointment مع Doctor Omar Benali بس أبي أغيره", "appointment, Doctor Omar Benali, أغيره"),
    ("mixed_cancel", "لو سمحت أبي أكنسل الbooking حقي يوم الأحد", "أكنسل, booking, الأحد"),
    ("emergency", "زوجي عنده ألم قوي في صدره وما يقدر يتنفس", "ألم قوي في صدره, ما يقدر يتنفس"),
    ("english", "Can I book an appointment with a dermatologist on Sunday?", "the English sentence, in English"),
    ("yes_ee", "ايه", "ايه (yes)"),
    ("yes_naam", "نعم", "نعم (yes)"),
    ("no", "لا", "لا (no)"),
    ("ok", "تمام", "تمام (ok)"),
]

# A smaller set for quick comparisons: run --quick / report --quick.
# Mixat dropped for this round (the user's call); 10 SADA clips instead of 5.
QUICK = ([f"sada_{i:02d}" for i in range(10)] + [f"clinic_{name}" for name, _, _ in CLINIC] + ["silence", "noise"])


# --- Text normalization and error rate ---

DIACRITICS = re.compile("[ؗ-ًؚ-ْـ]")   # harakat and tatweel
EASTERN_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def normalize(text: str) -> str:
    """Spelling variants that don't change meaning are unified, so they don't count as errors."""
    t = DIACRITICS.sub("", text).translate(EASTERN_DIGITS).lower()
    t = re.sub("[إأآٱ]", "ا", t).replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    t = re.sub(r"[^\w\s]", " ", t)          # punctuation, and Mixat's [brackets] around English words
    return " ".join(t.split())


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate: edits needed to turn the hypothesis into the reference, per reference character."""
    r, h = normalize(reference), normalize(hypothesis)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


# --- Audio ---

def load_pcm(path: Path) -> np.ndarray:
    """Any audio file -> 16 kHz mono int16."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    chunks = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks).astype(np.int16)


def save_wav(path: Path, pcm: np.ndarray, rate: int = RATE):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


# --- fetch ---

def dataset_rows(dataset: str, offset: int, length: int = 100) -> list[dict]:
    r = httpx.get(ROWS_API, params={"dataset": dataset, "config": "default", "split": "test",
                                    "offset": offset, "length": length}, timeout=120)
    r.raise_for_status()
    return [row["row"] for row in r.json()["rows"]]


def looks_like_speech(text: str) -> bool:
    words = text.split()
    return len(text) >= 15 and len(set(words)) >= 0.6 * len(words)   # skips music like "دوم تتك دوم تتك"


def download(url: str, path: Path):
    path.write_bytes(httpx.get(url, timeout=120).content)


def fetch_datasets(manifest: list[dict]):
    rng = random.Random(7)
    picked = {"mixat_cs": [], "mixat_ar": [], "sada": []}
    for offset in rng.sample(range(0, 1500, 100), 6):
        for row in dataset_rows("sqrk/mixat-tri", offset):
            kind = "mixat_cs" if row["language"] == "CS" else "mixat_ar"
            if 3000 <= row["duration_ms"] <= 15000 and len(picked[kind]) < (20 if kind == "mixat_cs" else 10):
                picked[kind].append(row)
    for offset in rng.sample(range(0, 6100, 100), 8):
        for row in dataset_rows("MohamedRashad/SADA22", offset):
            if row["speaker_dialect"] == "Khaliji" and looks_like_speech(row["cleaned_text"]) and len(picked["sada"]) < 20:
                picked["sada"].append(row)
    for kind, rows in picked.items():
        for i, row in enumerate(rows):
            clip_id = f"{kind}_{i:02d}"
            download(row["audio"][0]["src"], CLIPS / f"{clip_id}.wav")
            reference = row.get("transcript") or row.get("cleaned_text")
            manifest.append({"id": clip_id, "group": kind, "reference": reference})
            print(f"  {clip_id}")


async def fetch_clinic(manifest: list[dict]):
    import aiohttp
    from dotenv import load_dotenv
    from livekit import rtc
    from livekit.agents import inference
    load_dotenv(ROOT / ".env")
    async with aiohttp.ClientSession() as session:
        tts = inference.TTS(model="cartesia/sonic-3", language="ar", http_session=session)
        for name, text, expect in CLINIC:
            frames = [ev.frame async for ev in tts.synthesize(text)]
            audio = rtc.combine_audio_frames(frames)
            save_wav(CLIPS / f"clinic_{name}.wav", np.frombuffer(audio.data, dtype=np.int16), audio.sample_rate)
            manifest.append({"id": f"clinic_{name}", "group": "clinic", "reference": text, "expect": expect})
            print(f"  clinic_{name}")
    rng = np.random.default_rng(0)
    save_wav(CLIPS / "silence.wav", np.zeros(3 * RATE))
    save_wav(CLIPS / "noise.wav", rng.normal(0, 300, 3 * RATE).clip(-32768, 32767))   # quiet background hiss
    manifest += [{"id": "silence", "group": "silence", "reference": "", "expect": "nothing"},
                 {"id": "noise", "group": "silence", "reference": "", "expect": "nothing"}]


def fetch():
    CLIPS.mkdir(parents=True, exist_ok=True)
    manifest = []
    print("Datasets:")
    fetch_datasets(manifest)
    print("Clinic phrases:")
    asyncio.run(fetch_clinic(manifest))
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(manifest)} clips in {CLIPS}")


# --- run ---

def padded(pcm: np.ndarray) -> tuple[np.ndarray, int]:
    """A real line is never digitally silent: with exact zeros after the speech, Ink-Whisper dropped the last word.
    So every clip gets a quiet noise floor before and after, as on a call. Returns the audio and where speech ends."""
    rng = np.random.default_rng(len(pcm))
    lead, tail = (rng.normal(0, NOISE_FLOOR, int(s * RATE)).astype(np.int16)
                  for s in (LEADING_SILENCE, TRAILING_SILENCE))
    return np.concatenate([lead, pcm, tail]), len(lead) + len(pcm)


_groq_lock = asyncio.Lock()


async def transcribe_groq(model: str, language: str | None, pcm: np.ndarray) -> dict:
    """Not streaming: the whole utterance goes in one request once the caller stops. Latency = that request."""
    import io
    import os
    audio, end_of_speech = padded(pcm)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio[:end_of_speech + RATE // 2].tobytes())   # VAD would cut about 0.5 s after speech
    data = {"model": model.split("/", 1)[1], "response_format": "verbose_json", "temperature": "0"}
    if language:
        data["language"] = language
    async with _groq_lock:   # one at a time, spaced out, for the free tier's rate limit
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(3):
                t0 = time.perf_counter()
                r = await client.post(GROQ_URL, data=data, files={"file": ("clip.wav", buf.getvalue(), "audio/wav")},
                                      headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
                latency = (time.perf_counter() - t0) * 1000
                if r.status_code != 429:
                    break
                await asyncio.sleep(float(r.headers.get("retry-after", 10)))
        await asyncio.sleep(GROQ_GAP)
    if r.status_code != 200:
        return {"text": "", "languages": [], "latency_ms": None, "error": f"HTTP {r.status_code}: {r.text[:150]}"}
    body = r.json()
    return {"text": body.get("text", "").strip(), "languages": [body.get("language") or "?"],
            "latency_ms": latency, "error": None}


async def transcribe_speechmatics_rt(language: str, options: dict, pcm: np.ndarray) -> dict:
    """Speechmatics' real-time API directly, streamed at real-time speed. Latency = end of speech -> last final."""
    import os
    from speechmatics.rt import (AsyncClient, AudioEncoding, AudioFormat, ServerMessageType, TranscriptionConfig,
                                 TranscriptResult)
    audio, end_of_speech = padded(pcm)
    texts, state = [], {"last_final": None, "speech_end": None}
    try:
        async with AsyncClient(api_key=os.environ["SPEECHMATICS_API_KEY"]) as client:
            @client.on(ServerMessageType.ADD_TRANSCRIPT)
            def on_final(message):
                text = TranscriptResult.from_message(message).metadata.transcript.strip()
                if text:
                    texts.append(text)
                    state["last_final"] = time.perf_counter()

            await client.start_session(
                transcription_config=TranscriptionConfig(language=language, enable_partials=False, max_delay=1.0,
                                                         **options),
                audio_format=AudioFormat(encoding=AudioEncoding.PCM_S16LE, sample_rate=RATE, chunk_size=640))
            step = RATE // 50   # 20 ms
            start = time.perf_counter()
            for i in range(0, len(audio), step):
                await client.send_audio(audio[i:i + step].tobytes())
                if i + step >= end_of_speech and state["speech_end"] is None:
                    state["speech_end"] = time.perf_counter()
                await asyncio.sleep(max(0, start + (i + step) / RATE - time.perf_counter()))
            await asyncio.wait_for(client.stop_session(), timeout=WAIT_AFTER + 5)
        error = None
    except Exception as e:   # a rejected setting (e.g. an unsupported domain) is a result too
        error = f"{type(e).__name__}: {e}"[:200]
    latency = ((state["last_final"] - state["speech_end"]) * 1000
               if state["last_final"] and state["speech_end"] else None)
    return {"text": " ".join(texts), "languages": [language], "latency_ms": latency, "error": error}


async def transcribe(session, setting: str, pcm: np.ndarray) -> dict:
    """Stream the clip at real-time speed, like a live caller; latency = end of speech -> last final transcript."""
    from livekit import rtc
    from livekit.agents import inference, stt as lk_stt
    model, language, *extra = SETTINGS[setting]
    if model.startswith("groq/"):
        return await transcribe_groq(model, language, pcm)
    if model == "speechmatics-rt":
        return await transcribe_speechmatics_rt(language, extra[0] if extra else {}, pcm)
    if model == "speechmatics":
        from livekit.plugins import speechmatics
        engine = speechmatics.STT(language=language, enable_diarization=False, **(extra[0] if extra else {}))
        # The constructor turns "ar_en" into "ar-EN" (read as a region code), which the service rejects;
        # a per-stream language is passed through unchanged.
        stream = engine.stream(language=language)
    elif model == "soniox":
        from livekit.plugins import soniox
        engine = soniox.STT(http_session=session, params=soniox.STTOptions(
            language_hints=language.split(","), enable_language_identification=True))
        stream = engine.stream()
    else:
        kwargs = ({"language": language} if language else {}) | (extra[0] if extra else {})
        engine = inference.STT(model=model, http_session=session, **kwargs)
        stream = engine.stream()
    texts, languages, last_final = [], set(), None
    speech_end = None

    async def push():
        nonlocal speech_end
        step = RATE // 50   # 20 ms frames
        audio, end_of_speech = padded(pcm)
        start = time.perf_counter()
        for i in range(0, len(audio), step):
            chunk = audio[i:i + step]
            stream.push_frame(rtc.AudioFrame(chunk.tobytes(), RATE, 1, len(chunk)))
            if i + step >= end_of_speech and speech_end is None:
                speech_end = time.perf_counter()
            await asyncio.sleep(max(0, start + (i + step) / RATE - time.perf_counter()))
        stream.end_input()

    async def collect():
        nonlocal last_final
        async for ev in stream:
            if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT and ev.alternatives[0].text.strip():
                texts.append(ev.alternatives[0].text.strip())
                languages.add(ev.alternatives[0].language or "?")
                last_final = time.perf_counter()

    pusher = asyncio.create_task(push())
    collector = asyncio.create_task(collect())
    error = None
    try:
        await pusher
        # The stream may stay open after the audio ends; whatever hasn't arrived WAIT_AFTER seconds later is lost
        await asyncio.wait_for(asyncio.shield(collector), timeout=WAIT_AFTER)
    except asyncio.TimeoutError:
        collector.cancel()
    except Exception as e:   # a provider error is a result too
        error = f"{type(e).__name__}: {e}"[:200]
        collector.cancel()
    await stream.aclose()
    latency = (last_final - speech_end) * 1000 if last_final and speech_end else None
    return {"text": " ".join(texts), "languages": sorted(languages), "latency_ms": latency, "error": error}


async def run_all(quick: bool, only: list[str]):
    import aiohttp
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    manifest = clips_for(quick)
    settings = only or list(SETTINGS)
    results = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    limit = asyncio.Semaphore(PARALLEL)
    one_at_a_time = asyncio.Semaphore(1)   # Speechmatics' free account allows few sessions at once
    async with aiohttp.ClientSession() as session:
        async def one(clip, setting):
            async with (one_at_a_time if SETTINGS[setting][0].startswith("speechmatics") else limit):
                r = await transcribe(session, setting, load_pcm(CLIPS / f"{clip['id']}.wav"))
                r["cer"] = cer(clip["reference"], r["text"]) if clip["reference"] else None
                results.setdefault(clip["id"], {})[setting] = r
                print(f"  {clip['id']:22} {setting:22} cer={r['cer'] if r['cer'] is None else round(r['cer'], 2)}"
                      f" latency={r['latency_ms'] and round(r['latency_ms'])} {r['error'] or ''}")
        def done(clip, setting):   # already transcribed without an error: keep it (use --redo to run again)
            r = results.get(clip["id"], {}).get(setting)
            return r is not None and not r["error"] and "--redo" not in sys.argv
        await asyncio.gather(*(one(c, s) for c in manifest for s in settings if not done(c, s)))
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


# --- report ---

GROUPS = {"mixat_cs": "Emirati, mixed Arabic and English (Mixat)", "mixat_ar": "Emirati Arabic (Mixat)",
          "sada": "Gulf Arabic, Khaliji (SADA)", "clinic": "Clinic phrases (generated voice)",
          "silence": "Silence and noise (should produce nothing)"}


def summary(manifest, results) -> list[tuple]:
    rows = []
    for group in GROUPS:
        clips = [c for c in manifest if c["group"] == group]
        if not clips:
            continue
        for setting in SETTINGS:
            rs = [results.get(c["id"], {}).get(setting) for c in clips]
            rs = [r for r in rs if r]
            cers = [r["cer"] for r in rs if r["cer"] is not None]
            lats = [r["latency_ms"] for r in rs if r["latency_ms"] is not None]
            rows.append((GROUPS[group], setting, len(rs),
                         f"{np.median(cers):.0%}" if cers else "–",
                         f"{np.median(lats):.0f} ms" if lats else "–",
                         sum(bool(r["text"]) for r in rs) if group == "silence" else "",
                         sum(bool(r["error"]) for r in rs)))
    return rows


def clips_for(quick: bool) -> list[dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return [c for c in manifest if c["id"] in QUICK] if quick else manifest


def report(quick: bool):
    manifest = clips_for(quick)
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    head = "".join(f"<th>{escape(s)}</th>" for s in SETTINGS)
    summary_rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in row) + "</tr>" for row in summary(manifest, results))
    sections = []
    for group, title in GROUPS.items():
        rows = []
        if not any(c["group"] == group for c in manifest):
            continue
        for c in [c for c in manifest if c["group"] == group]:
            cells = []
            for setting in SETTINGS:
                r = results.get(c["id"], {}).get(setting, {})
                meta = []
                if r.get("cer") is not None:
                    meta.append(f"errors {r['cer']:.0%}")
                if r.get("latency_ms") is not None:
                    meta.append(f"{r['latency_ms']:.0f} ms")
                if r.get("languages"):
                    meta.append("lang " + ",".join(r["languages"]))
                if r.get("error"):
                    meta.append("ERROR " + r["error"])
                cells.append(f"<td><div class='t' dir='auto'>{escape(r.get('text') or '—')}</div>"
                             f"<div class='m'>{escape(' · '.join(meta))}</div></td>")
            expect = f"<div class='m'>must contain: {escape(c['expect'])}</div>" if c.get("expect") else ""
            rows.append(f"<tr><td class='id'>{escape(c['id'])}<audio controls preload='none' "
                        f"src='clips/{c['id']}.wav'></audio></td>"
                        f"<td><div class='t' dir='auto'>{escape(c['reference'] or '(nothing)')}</div>{expect}</td>"
                        f"{''.join(cells)}</tr>")
        sections.append(f"<h2>{escape(title)}</h2><table><tr><th>Clip</th><th>What was said</th>{head}</tr>"
                        f"{''.join(rows)}</table>")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>STT comparison</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --head:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f2f2f7; --muted:#a1a1a6; --line:#3a3a3c; --head:#2c2c2e; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:24px; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:32px; }}
th, td {{ border:1px solid var(--line); padding:8px; vertical-align:top; text-align:start; }}
th {{ background:var(--head); }}
.t {{ font-size:17px; }} .m {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.id {{ white-space:nowrap; font-size:13px; }} audio {{ display:block; width:220px; margin-top:6px; }}
</style></head><body>
<h1>Speech-to-text comparison</h1>
<p>Errors = character error rate after normalizing spelling variants (diacritics, alef/ya/ta marbuta forms, digits).
Latency = from the end of speech to the final transcript. Listen to each clip and judge the transcripts yourself:
the error rate is a guide, not the verdict (word order, numbers written as words, and English in Arabic letters all
count as "errors" here).</p>
<h2>Summary (medians)</h2>
<table><tr><th>Group</th><th>Setting</th><th>Clips</th><th>Errors</th><th>Latency</th>
<th>Invented text on silence</th><th>Failures</th></tr>{summary_rows}</table>
{''.join(sections)}
</body></html>"""
    (OUT / "report.html").write_text(html, encoding="utf-8")
    print(f"Report: {OUT / 'report.html'}")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    quick = "--quick" in sys.argv
    only = [s for s in SETTINGS if any(arg.lower() in s for arg in sys.argv[2:] if not arg.startswith("--"))]
    if step == "fetch":
        fetch()
    elif step == "run":
        asyncio.run(run_all(quick, only))   # e.g. run --quick deepgram groq
    elif step == "report":
        report(quick)
    else:
        print(__doc__)
