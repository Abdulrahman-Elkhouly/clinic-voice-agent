r"""Talk to the full agent by typing; it answers out loud through your speakers with the clinic's custom voice.
No speech-to-text and no LiveKit server: the LLM, the tools, the knowledge base (Voyage + Qdrant), the clinic
database and the voice (Gemini) are the real ones, so it behaves like a call with typed caller turns.

    python agent\talk.py              opens a chat page in your browser (Arabic shows properly there)
    python agent\talk.py --terminal   the same in the terminal ("exit" ends the call)

The page is served only on this computer (http://127.0.0.1:8765). Uses the seeded database
(python agent\seed_db.py --reset). Every sentence the agent speaks is one Gemini request.
"""
import asyncio
import html
import json
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import web
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import AgentSession
from livekit.agents.voice import io

from agent import LLM_MODEL, TTS_VOICE, CallData, ReceptionAgent, make_llm
from db import ClinicDB
from gemini_tts import GREETING_TEXT, SAMPLE_RATE, GeminiTTS
from rag import KnowledgeBase

load_dotenv()
PORT = 8765
TIMING_LOG = Path(__file__).resolve().parent.parent / "data" / "logs" / "talk_timings.log"


class Speakers(io.AudioOutput):
    """Plays the agent's audio on the default output device and tells the session when each reply has played."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__(label="Speakers", capabilities=io.AudioOutputCapabilities(pause=False),
                         sample_rate=SAMPLE_RATE)
        self._loop = loop
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._pushed = 0.0
        self._started = False
        self.drained = asyncio.Event()
        self.drained.set()
        self._stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=self._play)
        self._stream.start()
        self.first_sound_at: float | None = None

    def _play(self, outdata, frames, _time, _status):   # audio thread
        with self._lock:
            n = min(len(self._buf), frames * 2)
            outdata[:] = 0
            if n:
                outdata[:n // 2, 0] = np.frombuffer(bytes(self._buf[:n]), dtype=np.int16)
                del self._buf[:n]
                if not self._started:
                    self._started = True
                    self.first_sound_at = time.perf_counter()
                    self._loop.call_soon_threadsafe(lambda: self.on_playback_started(created_at=time.time()))
            if not self._buf:
                self._loop.call_soon_threadsafe(self.drained.set)

    async def capture_frame(self, frame: rtc.AudioFrame) -> None:
        await super().capture_frame(frame)
        with self._lock:
            self._buf += bytes(frame.data)
            self._pushed += frame.duration
        self.drained.clear()

    def flush(self) -> None:
        super().flush()
        asyncio.ensure_future(self._finish())

    def clear_buffer(self) -> None:
        with self._lock:
            self._buf.clear()
        self.drained.set()

    async def _finish(self) -> None:
        await self.drained.wait()
        await asyncio.sleep(0.15)   # let the device play out its last block
        pushed, self._pushed, self._started = self._pushed, 0.0, False
        self.on_playback_finished(playback_position=pushed, interrupted=False)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()


class Collect(logging.Handler):
    """Keeps the timing lines of the current turn (never the text: those loggers don't log it)."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"[{record.name}] {record.getMessage()}")


class Call:
    """One call: start it, then send typed caller turns; each turn returns what was said, tools, and timings."""

    async def start(self) -> None:
        self.data = CallData(kb=await asyncio.to_thread(KnowledgeBase), db=ClinicDB())
        self.voice = GeminiTTS(voice=TTS_VOICE)
        self.speakers = Speakers(asyncio.get_running_loop())
        self.collect = Collect()
        for name in ("gemini_tts", "timing"):
            logging.getLogger(name).setLevel(logging.INFO)
            logging.getLogger(name).addHandler(self.collect)
        self.session = AgentSession[CallData](userdata=self.data, llm=make_llm(), tts=self.voice)
        self.session.output.audio = self.speakers
        self.lock = asyncio.Lock()
        self.turns = 0
        TIMING_LOG.parent.mkdir(parents=True, exist_ok=True)
        await self.session.start(ReceptionAgent())   # plays the greeting

    async def turn(self, text: str) -> dict:
        async with self.lock:
            await self.speakers.drained.wait()
            self.collect.lines = []
            self.speakers.first_sound_at = None
            sent = time.perf_counter()
            result = await self.session.run(user_input=text)
            replies, tools = [], []
            for ev in result.events:
                if ev.type == "message" and ev.item.role == "assistant" and ev.item.text_content:
                    replies.append(ev.item.text_content)
                elif ev.type == "function_call":
                    tools.append(ev.item.name)
            first = self.speakers.first_sound_at
            timings = list(self.collect.lines)
            if first:
                timings.append(f"first sound {first - sent:.1f} s after you sent the message")
            self._log_turn(tools, timings)
            return {"replies": replies, "tools": tools, "timings": timings}

    def _log_turn(self, tools: list[str], timings: list[str]) -> None:
        """Timings and tool names only (never what was said: it can hold patient details), so they can be
        reviewed after the call. data/ is gitignored."""
        self.turns += 1
        with open(TIMING_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} call {self.data.call_id[:8]} turn {self.turns} "
                    f"| model {LLM_MODEL} | tools: {', '.join(tools) or '-'}\n")
            f.writelines(f"    {line}\n" for line in timings)

    async def end(self) -> str:
        released = await asyncio.to_thread(self.data.db.release_hold, call_id=self.data.call_id)
        await self.session.aclose()
        await self.voice.aclose()
        self.speakers.close()
        return f"Call ended; released {released} hold(s)."


PAGE = """<!doctype html><html lang="ar"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Talk to the agent</title>
<style>
:root { --bg:#f6f6f7; --panel:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e3e3e7; --you:#dbeafe; --agent:#fff;
        --accent:#0a7c66; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --panel:#1f1f22; --fg:#f2f2f5; --muted:#a1a1a8;
        --line:#333338; --you:#1e3a5f; --agent:#26262a; --accent:#36c2a3; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.55 system-ui, "Segoe UI", Tahoma, sans-serif; }
header { padding:14px 16px; border-bottom:1px solid var(--line); background:var(--panel); display:flex;
         align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
header h1 { font-size:17px; margin:0; } header small { color:var(--muted); }
#log { max-width:820px; margin:0 auto; padding:16px; display:flex; flex-direction:column; gap:10px;
       padding-bottom:110px; }
.msg { padding:10px 14px; border-radius:14px; max-width:85%; white-space:pre-wrap; border:1px solid var(--line); }
.you { background:var(--you); align-self:flex-end; } .agent { background:var(--agent); align-self:flex-start; }
.meta { color:var(--muted); font-size:13px; align-self:flex-start; font-family:ui-monospace, Consolas, monospace;
        white-space:pre-wrap; }
form { position:fixed; bottom:0; left:0; right:0; background:var(--panel); border-top:1px solid var(--line);
       padding:12px 16px; }
form div { max-width:820px; margin:0 auto; display:flex; gap:8px; }
input { flex:1; font:inherit; padding:10px 12px; border-radius:10px; border:1px solid var(--line);
        background:var(--bg); color:var(--fg); min-width:0; }
button { font:inherit; padding:10px 16px; border-radius:10px; border:0; background:var(--accent); color:#fff;
         cursor:pointer; } button.end { background:transparent; color:var(--muted); border:1px solid var(--line); }
button:disabled, input:disabled { opacity:.5; cursor:default; }
</style></head><body>
<header><div><h1>Talk to the agent</h1><small>__MODEL__ · voice __VOICE__ · typed turns, spoken replies</small></div>
<button class="end" id="end">Hang up</button></header>
<div id="log"><div class="msg agent" dir="auto">__GREETING__</div></div>
<form id="f"><div><input id="t" dir="auto" autocomplete="off" placeholder="اكتب رسالتك / type your message"
autofocus><button id="send">Send</button></div></form>
<script>
const log = document.getElementById('log'), t = document.getElementById('t'), send = document.getElementById('send');
function add(cls, text) { const d = document.createElement('div'); d.className = cls; d.dir = 'auto';
  d.textContent = text; log.appendChild(d); d.scrollIntoView({block: 'end'}); return d; }
function busy(b) { t.disabled = send.disabled = b; if (!b) t.focus(); }
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault(); const text = t.value.trim(); if (!text) return;
  add('msg you', text); t.value = ''; busy(true); const wait = add('meta', '…');
  try {
    const r = await (await fetch('/say', {method: 'POST', body: JSON.stringify({text})})).json();
    wait.remove();
    if (r.error) { add('meta', 'error: ' + r.error); }
    else {
      r.replies.forEach(x => add('msg agent', x));
      const meta = (r.tools.length ? 'tools: ' + r.tools.join(', ') + '\\n' : '') + r.timings.join('\\n');
      if (meta) add('meta', meta);
    }
  } catch (err) { wait.textContent = 'error: ' + err; }
  busy(false);
};
document.getElementById('end').onclick = async () => {
  busy(true); document.getElementById('end').disabled = true;
  const r = await (await fetch('/end', {method: 'POST'})).json(); add('meta', r.message);
};
</script></body></html>"""


async def run_web(call: Call) -> None:
    done = asyncio.Event()
    page = (PAGE.replace("__MODEL__", html.escape(LLM_MODEL)).replace("__VOICE__", html.escape(TTS_VOICE))
            .replace("__GREETING__", html.escape(GREETING_TEXT)))

    async def index(_request):
        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def say(request):
        try:
            text = (json.loads(await request.text()).get("text") or "").strip()
            if not text:
                return web.json_response({"error": "empty message"})
            return web.json_response(await call.turn(text))
        except Exception as e:   # shown on the page; the call goes on
            logging.exception("turn failed")
            return web.json_response({"error": f"{type(e).__name__}: {e}"})

    async def end(_request):
        message = await call.end()
        done.set()
        return web.json_response({"message": message})

    app = web.Application()
    app.add_routes([web.get("/", index), web.post("/say", say), web.post("/end", end)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()   # this computer only
    url = f"http://127.0.0.1:{PORT}"
    print(f"Chat page: {url}  (opening it in your browser; press Hang up there to end the call)")
    webbrowser.open(url)
    try:
        await done.wait()
    finally:
        await runner.cleanup()


async def run_terminal(call: Call) -> None:
    print("Call started. Type what the caller says ('exit' to hang up).\n")
    try:
        while True:
            await call.speakers.drained.wait()
            text = (await asyncio.to_thread(input, "You: ")).strip()
            if text.lower() in ("exit", "quit"):
                break
            if not text:   # an empty line (or text the terminal didn't pass on) used to hang up: ignore it
                print("  (nothing received; type a message, or 'exit' to hang up)")
                continue
            r = await call.turn(text)
            for reply in r["replies"]:
                print(f"Agent: {reply}")
            for name in r["tools"]:
                print(f"  (tool: {name})")
            for line in r["timings"]:
                print(f"  {line}")
            print()
    finally:
        print(await call.end())


async def main():
    logging.basicConfig(level=logging.WARNING, format="  [%(name)s] %(message)s")
    print("Loading the knowledge base and the clinic database...")
    call = Call()
    await call.start()
    await (run_terminal(call) if "--terminal" in sys.argv else run_web(call))


if __name__ == "__main__":
    asyncio.run(main())
