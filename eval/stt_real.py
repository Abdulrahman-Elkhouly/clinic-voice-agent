r"""Test speech-to-text on the user's own voice, as heard over a phone call: Gulf Arabic, English, mixed, file and
phone numbers, dates, short replies.

    python eval\stt_real.py record             # opens the recorder in the browser (any line can be redone there)
    python eval\stt_real.py phone              # make the phone-line versions to listen to (offline, free)
    python eval\stt_real.py run                # transcribe every clip with each setting (skips finished ones)
    python eval\stt_real.py run soniox         # only some settings (by name); --redo to run again
    python eval\stt_real.py report             # write data\stt\real\report.html (open it in a browser)

Everything goes to data\stt\real\ (gitignored): the clips are the user's voice.

Exactly as on a call: each clip goes through the phone line in code (phone_line), then into the agent's own
speech-to-text (stt_config.make_stt) in 10 ms frames at real-time speed, as LiveKit delivers them.
The report has no scores: the user listens and judges (2026-10-25).
"""
import asyncio
import json
import sys
import time
import wave
from html import escape
from pathlib import Path

import av
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "eval"))
from compare_stt import RATE, load_pcm, padded  # noqa: E402

BASE = ROOT / "data" / "stt" / "real"
# --generated: the all-generated set (the user's call, 2026-10-25), in its own folder with its own results and
# report, so the recordings and their results stay as they are.
ALL_GENERATED = "--generated" in sys.argv
OUT = BASE / "generated" if ALL_GENERATED else BASE
CLIPS = OUT / "clips"
PHONE = OUT / "phone"   # what the agent receives after the phone line, to listen to
# One results file per provider, so providers can run at the same time without overwriting each other
RESULTS = {"speechmatics": OUT / "results.json", "soniox": OUT / "results_soniox.json"}


def load_results(provider: str) -> dict:
    path = RESULTS[provider]
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

# name -> (provider, send the clinic word list stt_config.VOCAB, other make_stt options). Everything else is the
# agent's make_stt().
SETTINGS = {
    "A: speechmatics (ar_en)": ("speechmatics", False),
    "B: speechmatics (ar_en, vocab)": ("speechmatics", True),
    "C: soniox (ar+en)": ("soniox", False, {"finalize": False, "describe": False}),
    "D: soniox (ar+en, vocab)": ("soniox", True, {"finalize": False, "describe": False}),
    # From here on, the line stays open after each clip (see transcribe); A-D ran before that fix.
    "E: soniox + finalize": ("soniox", False, {"finalize": True}),
    "F: soniox + finalize, description, words": ("soniox", True, {"finalize": True, "describe": True}),   # the agent's setting
}
AGENT_RATE = 24000   # AgentSession's room input rate (RoomInputOptions.audio_sample_rate); the plugin resamples
FRAME = AGENT_RATE // 100   # LiveKit delivers 10 ms frames
WAIT_AFTER = 5.0     # seconds to wait for final transcripts after the audio ends

GROUPS = {"gulf": "Gulf Arabic", "english": "English", "mixed": "Mixed Arabic and English",
          "numbers": "File and phone numbers", "dates": "Dates and times", "short": "Short replies"}

# (id, group, what to read, how to say it, what a correct transcript holds). Not scored now (the user judges);
# kept as a note of what each line tests:
#   {"must": [[alternatives], ...]}   every group has one alternative in the transcript (after normalize)
#   {"digits": "482135"}              the agent's digit conversion gives exactly this
#   {"digits_in": "457688"}           ...contains this (a caller correcting themselves)
#   {"exact": [alternatives]}         the whole transcript is one of these (short replies)
LINES = [
    ("gulf_book", "gulf", "أبي أحجز موعد عند دكتور العظام يوم الأحد العصر", "",
     {"must": [["احجز"], ["موعد"], ["عظام"], ["الاحد"], ["العصر"]]}),
    ("gulf_cancel", "gulf", "ودي ألغي موعدي اللي باكر، ما بقدر أجي", "",
     {"must": [["الغي"], ["موعد"], ["باكر", "بكره", "بكرا"]]}),
    ("gulf_move", "gulf", "ممكن تأخر موعدي لين الأسبوع الجاي؟", "",
     {"must": [["موعد"], ["الاسبوع"], ["الجاي"]]}),
    ("gulf_child", "gulf", "ولدي عنده حرارة وكحة من يومين، أي دكتور يشوفه؟", "",
     {"must": [["حراره"], ["كحه"], ["دكتور"]]}),
    ("gulf_emergency", "gulf", "أبوي طاح وما يقدر يحرك يده اليمين وكلامه ثقيل", "",
     {"must": [["طاح"], ["يحرك"], ["يده", "ايده"], ["ثقيل"]]}),
    ("gulf_hours", "gulf", "شو مواعيدكم يوم الجمعة؟ تفتحون الصبح؟", "",
     {"must": [["الجمعه"], ["الصبح"]]}),
    ("gulf_parking", "gulf", "وين أوقف السيارة لما أوصل عندكم؟", "",
     {"must": [["اوقف"], ["السياره"]]}),
    ("gulf_results", "gulf", "طلعت نتايج التحاليل حقتي ولا بعد؟", "",
     {"must": [["نتايج"], ["تحاليل"]]}),
    ("gulf_female_doctor", "gulf", "أبي دكتورة حريم للجلدية، عندكم؟", "",
     {"must": [["دكتوره"], ["جلديه"]]}),

    ("en_book", "english", "I'd like to book an appointment with Doctor Noura Al-Sulaiti next Tuesday morning.", "",
     {"must": [["book"], ["appointment"], ["noura", "nora"], ["sulaiti", "sulaity", "suleiti", "sulaity"],
               ["tuesday"], ["morning"]]}),
    ("en_cancel", "english", "I need to cancel my appointment tomorrow, something came up.", "",
     {"must": [["cancel"], ["appointment"], ["tomorrow"]]}),
    ("en_insurance", "english", "Do you accept Gulf Shield Insurance?", "",
     {"must": [["gulf shield"], ["insurance"]]}),
    ("en_child", "english", "My son has an ear infection, which doctor should he see?", "",
     {"must": [["ear"], ["infection"], ["doctor"]]}),
    ("en_emergency", "english", "My wife is having chest pain and she can't breathe.", "",
     {"must": [["chest pain"], ["breathe"]]}),

    ("mix_cancel", "mixed", "لو سمحت أبي أكنسل الـ appointment حقي", "",
     {"must": [["كنسل", "cancel"], ["appointment", "ابوينتمنت", "ابوينتمينت", "ابوينمنت"]]}),
    ("mix_doctor", "mixed", "عندي booking مع Doctor Omar Benali يوم الإثنين", "",
     {"must": [["booking", "بوكنج", "بوكينج"], ["omar", "عمر"], ["benali", "بن علي", "بنعلي"],
               ["الاثنين", "monday"]]}),
    ("mix_insurance", "mixed", "تاخذون Doha Care ولا لازم أدفع cash؟", "",
     {"must": [["doha", "دوحه"], ["care", "كير"], ["cash", "كاش"]]}),
    ("mix_time", "mixed", "أبي الموعد at four thirty لو فاضي", "",
     {"must": [["موعد"], ["four thirty", "4 30", "430"]]}),
    ("mix_reschedule", "mixed", "Can I change my موعد to يوم الخميس؟", "",
     {"must": [["change"], ["موعد"], ["الخميس", "thursday"]]}),
    ("mix_report", "mixed", "I need a sick leave report، ضروري اليوم", "",
     {"must": [["sick leave"], ["report"], ["ضروري"], ["اليوم"]]}),
    ("mix_dermatology", "mixed", "عندي حساسية في جلدي، do you have a dermatologist؟", "",
     {"must": [["حساسيه"], ["جلد"], ["dermatologist"]]}),

    ("num_file_ar", "numbers", "رقم ملفي ٤٨٢١٣٥", "digit by digit in Arabic: أربعة ثمانية اثنين واحد ثلاثة خمسة",
     {"digits": "482135"}),
    ("num_file_pause", "numbers", "رقم الملف ٧٠٣ ... ٩٦٢", "Arabic digit words, pause about a second after the third",
     {"digits": "703962"}),
    ("num_file_en", "numbers", "My file number is 5 1 9 0 0 7", "digit by digit in English", {"digits": "519007"}),
    ("num_file_mixed", "numbers", "رقم ملفي three eight one two nine four", "Arabic start, English digits",
     {"digits": "381294"}),
    ("num_phone_ar", "numbers", "رقمي ٥٥١٢٣٤٥٦", "digit by digit in Arabic", {"digits": "55123456"}),
    ("num_phone_gulf", "numbers", "رقمي ثلاثة ثلاثة ثنين ثمان، ثمان صفر واحد سبعة", "Gulf forms: ثنين, ثمان",
     {"digits": "33288017"}),
    ("num_phone_pairs", "numbers", "رقمي خمسة وخمسين، اثنعش، أربعة وثلاثين، تسعين", "in pairs: 55 12 34 90",
     {"digits": "55123490"}),
    ("num_phone_en", "numbers", "My number is 7 0 4 3 5 8 1 6", "digit by digit in English", {"digits": "70435816"}),
    ("num_phone_double", "numbers", "My mobile is double six, double zero, one one, two two", "as written",
     {"digits": "66001122"}),
    ("num_correction", "numbers", "رقم ملفي ٤٥٦٧... لا لا، ٤٥٧٦٨٨", "Arabic digit words, correcting yourself",
     {"digits_in": "457688"}),

    ("date_dob_ar", "dates", "تاريخ ميلادي ١٤ مارس ١٩٩٠", "say: أربعطعش مارس ألف وتسعمية وتسعين",
     {"must": [["14", "اربعطعش", "اربعتعش", "اربعه عشر"], ["مارس", "march"], ["1990"]]}),
    ("date_dob_numeric", "dates", "مواليد ٩ / ٩ / ٢٠٠١", "say: تسعة تسعة ألفين وواحد",
     {"must": [["9", "تسعه"], ["2001", "الفين وواحد"]]}),
    ("date_dob_en", "dates", "I was born on the fourteenth of March, nineteen ninety", "",
     {"must": [["14", "fourteenth"], ["march"], ["1990", "nineteen ninety"]]}),
    ("date_hijri", "dates", "مواليد ١٧ شعبان ١٤١٠", "say: سبعطعش شعبان ألف وأربعمية وعشرة",
     {"must": [["17", "سبعطعش", "سبعتعش", "سبعه عشر"], ["شعبان"], ["1410"]]}),
    ("date_relative_ar", "dates", "أبي موعد الأحد الجاي الساعة عشر الصبح", "",
     {"must": [["الاحد"], ["الجاي"], ["10", "عشر"], ["الصبح"]]}),
    ("date_relative_en", "dates", "Can I come next Thursday at 4:20 in the afternoon?", "say: four twenty",
     {"must": [["next thursday"], ["4 20", "four twenty", "420"], ["afternoon"]]}),

    ("short_ee", "short", "ايه", "Gulf yes, short", {"exact": ["ايه", "اي", "ايوه"]}),
    ("short_aywa", "short", "ايوه", "", {"exact": ["ايوه", "ايوا", "ايه"]}),
    ("short_la", "short", "لا", "", {"exact": ["لا"]}),
    ("short_tamam", "short", "تمام", "", {"exact": ["تمام"]}),
    ("short_naam", "short", "نعم", "", {"exact": ["نعم"]}),
    ("short_yes", "short", "Yes", "", {"exact": ["yes", "yeah"]}),
    ("short_no_thanks", "short", "لا شكراً", "", {"must": [["لا"], ["شكرا"]]}),
]
# How a generated clip is worded, so the voice says the digits one by one as a caller would ("٤٨٢١٣٥" would be read
# as one big number). Only the lines the user chose to generate (2026-10-25); the rest are the user's voice.
SPOKEN = {
    "num_file_ar": "رقم ملفي أربعة، ثمانية، اثنين، واحد، ثلاثة، خمسة",
    "num_file_pause": "رقم الملف سبعة، صفر، ثلاثة... تسعة، ستة، اثنين",
    "num_file_en": "My file number is five, one, nine, zero, zero, seven.",
    "num_file_mixed": "رقم ملفي three, eight, one, two, nine, four",
    "num_phone_ar": "رقمي خمسة، خمسة، واحد، اثنين، ثلاثة، أربعة، خمسة، ستة",
    "num_phone_gulf": "رقمي ثلاثة، ثلاثة، ثنين، ثمان، ثمان، صفر، واحد، سبعة",
    "num_phone_pairs": "رقمي خمسة وخمسين، اثنعش، أربعة وثلاثين، تسعين",
    "num_phone_en": "My number is seven, zero, four, three, five, eight, one, six.",
    "num_phone_double": "My mobile is double six, double zero, one one, two two.",
    "num_correction": "رقم ملفي أربعة، خمسة، ستة، سبعة... لا لا، أربعة، خمسة، سبعة، ستة، ثمانية، ثمانية",
    "short_ee": "ايه", "short_aywa": "ايوه", "short_la": "لا", "short_tamam": "تمام", "short_naam": "نعم",
    "short_yes": "Yes.", "short_no_thanks": "لا، شكراً",
}
# The lines the user recorded, as worded for the generated set (--generated). The user edits these before
# generating; the numbers and short replies use SPOKEN above.
GENERATED_WORDING = {
    "gulf_book": "أبي أحجز موعد عند دكتور العظام يوم الأحد العصر",
    "gulf_cancel": "ابي ألغي موعدي اللي بكره، ما اقدر أجي",
    "gulf_move": "ممكن تأخر موعدي لين الأسبوع الجاي؟",
    "gulf_child": "ولدي عنده حرارة وكحة من يومين، أي دكتور يشوفه؟",
    "gulf_emergency": "أبوي طاح وما يقدر يحرك يده اليمين وكلامه ثقيل",
    "gulf_hours": "شو مواعيدكم يوم الجمعة؟ تفتحون الصبح؟",
    "gulf_parking": "وين أوقف السيارة لما أوصل عندكم؟",
    "gulf_results": "طلعت نتايج التحاليل حقتي ولا بعد؟",
    "gulf_female_doctor": "أبي دكتورة حريم للجلدية، عندكم؟",
    "en_book": "I'd like to book an appointment with Doctor Noura Al-Sulaiti next Tuesday morning.",
    "en_cancel": "I need to cancel my appointment tomorrow, something came up.",
    "en_insurance": "Do you accept Gulf Shield Insurance?",
    "en_child": "My son has an ear infection, which doctor should he see?",
    "en_emergency": "My wife is having chest pain and she can't breathe.",
    "mix_cancel": "لو سمحت أبي أكنسل الـ appointment حقي",
    "mix_doctor": "عندي booking مع Doctor Omar Benali يوم الإثنين",
    "mix_insurance": "تاخذون Doha Care ولا لازم أدفع cash؟",
}
GENERATED = BASE / "generated.json"   # which clips in the recorded set are generated, for the report
GENERATED_SETTINGS = ["E: soniox + finalize", "F: soniox + finalize, description, words"]   # run on --generated


def wording(clip_id: str) -> str:
    """What a generated clip says."""
    return GENERATED_WORDING.get(clip_id) or SPOKEN[clip_id]

# --- phone line ---

def resample(pcm: np.ndarray, rate: int, to: int) -> np.ndarray:
    resampler = av.AudioResampler(format="s16", layout="mono", rate=to)
    frame = av.AudioFrame.from_ndarray(pcm.astype(np.int16).reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = rate
    out = [f.to_ndarray().reshape(-1) for f in resampler.resample(frame) + resampler.resample(None)]
    return np.concatenate(out)


def codec(pcm: np.ndarray, rate: int, name: str, bit_rate: int | None = None) -> np.ndarray:
    """Encode and decode with a real codec, as each hop of the call does."""
    enc = av.CodecContext.create(name, "w")
    enc.sample_rate, enc.layout, enc.format = rate, "mono", "s16"
    if bit_rate:
        enc.bit_rate = bit_rate
    dec = av.CodecContext.create("opus" if name == "libopus" else name, "r")
    dec.sample_rate, dec.layout = rate, "mono"
    to_s16 = av.AudioResampler(format="s16", layout="mono", rate=rate)
    size = enc.frame_size or rate // 50   # 20 ms packets
    pcm = np.concatenate([pcm, np.zeros(-len(pcm) % size, np.int16)])
    out = []
    for i in range(0, len(pcm) + size, size):   # the last round flushes the encoder
        if i < len(pcm):
            frame = av.AudioFrame.from_ndarray(pcm[i:i + size].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate, frame.pts = rate, i
        else:
            frame = None
        for packet in enc.encode(frame):
            for decoded in dec.decode(packet):
                out += [f.to_ndarray().reshape(-1) for f in to_s16.resample(decoded)]
    return np.concatenate(out)


def phone_line(pcm: np.ndarray) -> np.ndarray:
    """A clip at 16 kHz -> what the agent receives from a mobile caller (24 kHz, 300-3,400 Hz phone sound):
    the caller's mobile network (AMR-NB 12.2 kbit/s, 8 kHz), the phone network to Twilio and LiveKit SIP
    (G.711 mu-law, 8 kHz), the LiveKit room track (Opus, 48 kHz), the agent's room input (24 kHz)."""
    x = resample(pcm, RATE, 8000)
    x = codec(x, 8000, "libopencore_amrnb", 12200)
    x = codec(x, 8000, "pcm_mulaw")
    x = codec(resample(x, 8000, 48000), 48000, "libopus", 32000)
    return resample(x, 48000, AGENT_RATE)


def on_the_phone(clip_id: str) -> tuple[np.ndarray, int]:
    """The clip as the agent receives it, with the quiet line before and after, and where the speech ends."""
    audio, end_of_speech = padded(load_pcm(CLIPS / f"{clip_id}.wav"))   # the line is never digitally silent
    return phone_line(audio), end_of_speech * AGENT_RATE // RATE


def save_wav(path: Path, pcm: np.ndarray, rate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype(np.int16).tobytes())


def make_phone_versions():
    for clip_id, *_ in LINES:
        if (CLIPS / f"{clip_id}.wav").exists():
            save_wav(PHONE / f"{clip_id}.wav", on_the_phone(clip_id)[0], AGENT_RATE)
    print(f"Phone versions in {PHONE}")


# --- record ---

PORT = 8765


def record():
    """The recorder runs in the browser (Arabic shows right to left there, not in the Windows terminal).
    This serves the page on this computer only and saves each clip it sends, at 16 kHz."""
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    ids = {ln[0] for ln in LINES}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code: int, body: bytes, kind: str = "text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self.reply(200, (ROOT / "eval" / "stt_recorder.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/lines":
                lines = [{"id": i, "group": GROUPS[g], "text": text, "hint": hint,
                          "done": (CLIPS / f"{i}.wav").exists()} for i, g, text, hint, _ in LINES]
                self.reply(200, json.dumps(lines, ensure_ascii=False).encode("utf-8"), "application/json")
            else:
                self.reply(404, b"not found")

        def do_POST(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path != "/save" or q.get("id") not in ids or not q.get("rate", "").isdigit():
                return self.reply(400, b"bad request")
            pcm = np.frombuffer(self.rfile.read(int(self.headers["Content-Length"])), dtype=np.int16)
            save_wav(CLIPS / f"{q['id']}.wav", resample(pcm, int(q["rate"]), RATE), RATE)
            print(f"  saved {q['id']} ({len(pcm) / int(q['rate']):.1f} s)")
            self.reply(200, b"ok")

        def log_message(self, *args):   # quiet: only saves are printed
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Recorder: {url} (opening the browser). Ctrl+C here when you're done.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"Clips in {CLIPS}")


async def generate():
    """Generated clips in the agent's custom Gemini voice (one request each): the SPOKEN lines not recorded yet, or
    with --generated, every line in SPOKEN and GENERATED_WORDING. Clips already generated with the same wording are
    copied, not requested again."""
    import shutil
    from agent import TTS_VOICE
    from gemini_tts import SAMPLE_RATE, GeminiTTS
    generated = set(json.loads(GENERATED.read_text(encoding="utf-8"))) if GENERATED.exists() else set()
    wanted = list(SPOKEN) + (list(GENERATED_WORDING) if ALL_GENERATED else [])
    missing = [i for i in wanted if not (CLIPS / f"{i}.wav").exists()]
    if ALL_GENERATED:
        CLIPS.mkdir(parents=True, exist_ok=True)
        for clip_id in [i for i in missing if i in generated and i in SPOKEN]:   # same text, same voice
            shutil.copy(BASE / "clips" / f"{clip_id}.wav", CLIPS / f"{clip_id}.wav")
            if (BASE / "phone" / f"{clip_id}.wav").exists():
                PHONE.mkdir(parents=True, exist_ok=True)
                shutil.copy(BASE / "phone" / f"{clip_id}.wav", PHONE / f"{clip_id}.wav")
            # Its transcripts too, so the same clip isn't sent to the STT again (the user's call)
            for provider, path in RESULTS.items():
                base_path = BASE / path.name
                if base_path.exists() and clip_id in (old := json.loads(base_path.read_text(encoding="utf-8"))):
                    results = load_results(provider)
                    results[clip_id] = {s: r for s, r in old[clip_id].items() if s in GENERATED_SETTINGS}
                    path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {clip_id} (copied with its transcripts, already generated)")
        missing = [i for i in missing if not (CLIPS / f"{i}.wav").exists()]
    print(f"Generating {len(missing)} clip(s), one Gemini request each")
    tts = GeminiTTS(voice=TTS_VOICE)
    try:
        for clip_id in missing:
            pcm = np.frombuffer(await tts.full_audio(wording(clip_id)), dtype=np.int16)
            save_wav(CLIPS / f"{clip_id}.wav", resample(pcm, SAMPLE_RATE, RATE), RATE)
            if not ALL_GENERATED:
                generated.add(clip_id)
                GENERATED.write_text(json.dumps(sorted(generated)), encoding="utf-8")
            print(f"  {clip_id} ({len(pcm) / SAMPLE_RATE:.1f} s)")
    finally:
        await tts.aclose()


# --- run ---

async def transcribe(engine, audio: np.ndarray, end_of_speech: int) -> dict:
    """Stream the phone audio at real-time speed, like a live caller; latency = end of speech -> last final.
    After the clip the line stays open (its quiet hiss keeps coming, as on a call) until the final transcript is in,
    or WAIT_AFTER seconds: an STT that waits for silence to end a turn must not lose it because the audio stopped
    (it did with Soniox, 2026-10-25)."""
    from livekit import rtc
    from livekit.agents import stt as lk_stt
    stream = engine.stream()
    texts, state = [], {"last_final": None, "speech_end": None, "pending": False}
    line_hiss = audio[-AGENT_RATE:]   # the last second: quiet line after the speech

    def done() -> bool:   # a final after the speech ended, with nothing still being transcribed
        return bool(state["speech_end"] and state["last_final"] and state["last_final"] > state["speech_end"]
                    and not state["pending"])

    async def push():
        start = time.perf_counter()
        i = 0
        while i < len(audio) or (not done() and i < len(audio) + WAIT_AFTER * AGENT_RATE):
            chunk = audio[i:i + FRAME] if i < len(audio) else line_hiss[(i - len(audio)) % AGENT_RATE:][:FRAME]
            stream.push_frame(rtc.AudioFrame(chunk.tobytes(), AGENT_RATE, 1, len(chunk)))
            if i + FRAME >= end_of_speech and state["speech_end"] is None:
                state["speech_end"] = time.perf_counter()
            i += len(chunk)
            await asyncio.sleep(max(0, start + i / AGENT_RATE - time.perf_counter()))
        stream.end_input()

    async def collect():
        async for ev in stream:
            text = ev.alternatives[0].text.strip() if ev.alternatives else ""
            if ev.type == lk_stt.SpeechEventType.FINAL_TRANSCRIPT and text:
                texts.append(text)
                state["last_final"], state["pending"] = time.perf_counter(), False
            elif ev.type in (lk_stt.SpeechEventType.INTERIM_TRANSCRIPT, lk_stt.SpeechEventType.PREFLIGHT_TRANSCRIPT):
                state["pending"] = bool(text)

    collector = asyncio.create_task(collect())
    error = None
    try:
        await push()
        await asyncio.wait_for(asyncio.shield(collector), timeout=1.0)   # stragglers after the line closes
    except asyncio.TimeoutError:
        collector.cancel()
    except Exception as e:   # a provider error is a result too
        error = f"{type(e).__name__}: {e}"[:200]
        collector.cancel()
    await stream.aclose()
    latency = ((state["last_final"] - state["speech_end"]) * 1000
               if state["last_final"] and state["speech_end"] else None)
    return {"text": " ".join(texts), "latency_ms": latency, "error": error}


async def run_all(only: list[str]):
    from stt_config import make_stt
    settings = only or list(SETTINGS)
    recorded = [ln for ln in LINES if (CLIPS / f"{ln[0]}.wav").exists()]
    print(f"{len(recorded)} of {len(LINES)} lines recorded")
    import aiohttp
    async with aiohttp.ClientSession() as session:
        for setting in settings:
            provider, vocab, *options = SETTINGS[setting]
            engine = make_stt(vocab=vocab, provider=provider, http_session=session, **(options[0] if options else {}))
            await run_setting(engine, setting, recorded,
                              provider)


async def run_setting(engine, setting: str, recorded: list, provider: str):
    results = load_results(provider)
    for clip_id, *_ in recorded:   # one at a time: the accounts allow few sessions
        old = results.get(clip_id, {}).get(setting)
        if old and not old["error"] and "--redo" not in sys.argv:
            continue
        audio, end_of_speech = on_the_phone(clip_id)
        if not (PHONE / f"{clip_id}.wav").exists():   # the same for every setting
            save_wav(PHONE / f"{clip_id}.wav", audio, AGENT_RATE)
        r = await transcribe(engine, audio, end_of_speech)
        results.setdefault(clip_id, {})[setting] = r
        RESULTS[provider].write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  {clip_id:20} {setting[:2]} {r['text']!r} {r['error'] or ''}", flush=True)


# --- report ---

def report():
    """Plain transcripts to listen and compare: no scores (the user judges them, 2026-10-25)."""
    results = {}
    for provider in RESULTS:
        for clip_id, by_setting in load_results(provider).items():
            results.setdefault(clip_id, {}).update(by_setting)
    ran = [s for s in SETTINGS if any(s in by_setting for by_setting in results.values())]
    head = "".join(f"<th>{escape(s)}</th>" for s in ran)
    generated = set(json.loads(GENERATED.read_text(encoding="utf-8"))) if GENERATED.exists() else set()
    if ALL_GENERATED:
        generated = {ln[0] for ln in LINES}

    def source(clip_id):
        return "<b>generated voice</b>" if clip_id in generated else "your voice"

    sections = []
    for group, title in GROUPS.items():
        rows = []
        for clip_id, g, text, hint, _ in LINES:
            if g != group or not (CLIPS / f"{clip_id}.wav").exists():
                continue
            cells = []
            for setting in ran:
                r = results.get(clip_id, {}).get(setting)
                if not r:
                    cells.append("<td>—</td>")
                    continue
                meta = [f"{r['latency_ms']:.0f} ms"] if r.get("latency_ms") is not None else []
                if r.get("error"):
                    meta.append("ERROR " + r["error"])
                cells.append(f"<td><div class='t' dir='auto'>{escape(r['text'] or '(nothing)')}</div>"
                             f"<div class='m'>{escape(' · '.join(meta))}</div></td>")
            said = wording(clip_id) if clip_id in generated else text
            rows.append(f"<tr><td class='id'>{escape(clip_id)}<div class='m'>{source(clip_id)}</div><audio controls "
                        f"preload='none' src='clips/{clip_id}.wav'></audio><div class='m'>over the phone (what "
                        f"Speechmatics hears)</div><audio controls preload='none' src='phone/{clip_id}.wav'></audio>"
                        f"</td><td><div class='t' dir='auto'>{escape(said)}</div></td>{''.join(cells)}</tr>")
        if rows:
            sections.append(f"<h2>{escape(title)}</h2><table><tr><th>Clip</th><th>What was said</th>{head}</tr>"
                            f"{''.join(rows)}</table>")
    title = "Speech-to-text, generated voice" if ALL_GENERATED else "Speech-to-text, your voice"
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --head:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1c1e; --fg:#f2f2f7; --muted:#a1a1a6; --line:#3a3a3c;
  --head:#2c2c2e; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:24px 16px; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:32px; }}
th, td {{ border:1px solid var(--line); padding:8px; vertical-align:top; text-align:start; }}
th {{ background:var(--head); }}
.t {{ font-size:17px; }} .m {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.id {{ white-space:nowrap; font-size:13px; }} audio {{ display:block; width:220px; margin-top:6px; }}
</style></head><body>
<h1>{title}</h1>
<p>Each clip went through a simulated mobile call (AMR-NB, then G.711 at 8 kHz, then LiveKit's Opus) into the
agent's own speech-to-text settings. A and C: plain. B and D: with the clinic word list. E: Soniox finalized by a voice detector when the caller stops. F: E plus a description of the call and Gulf words. A to D ran before a test fix (the line now stays open after each clip until the final text arrives). Time = end of speech to the final
transcript.</p>
{''.join(sections)}
</body></html>"""
    (OUT / "report.html").write_text(html, encoding="utf-8")
    print(f"Report: {OUT / 'report.html'}")


if __name__ == "__main__":
    step = sys.argv[1] if len(sys.argv) > 1 else ""
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if step == "record":
        record()
    elif step == "generate":
        asyncio.run(generate())
    elif step == "phone":
        make_phone_versions()
    elif step == "run":
        asyncio.run(run_all([s for s in SETTINGS if any(a.lower() in s.lower() for a in args)]))
    elif step == "report":
        report()
    else:
        print(__doc__)
