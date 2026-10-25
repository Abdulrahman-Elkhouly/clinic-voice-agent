"""What the agent does when the caller goes quiet (agreed 2026-10-22).

LiveKit marks the caller "away" after AWAY_AFTER seconds in which neither side speaks, and again after each
check-in. Each "away" moves one step; the caller speaking starts over:
    1st: a gentle check-in ("معي؟ خذ راحتك.") - skipped once if the caller had said they needed a moment, so they
         get about twice as long first
    2nd: a check-in that offers help - the forgot-file-number path if the agent had just asked for the file number
    3rd: a polite goodbye, then the call ends (the shutdown callback releases any held slot)
The lines are pre-recorded in the clinic's voice (prerecord_voice.py), so they play at once without the LLM.
"""
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

log = logging.getLogger("silence")

AWAY_AFTER = 15.0   # seconds of silence before each step (passed to AgentSession as user_away_timeout)

LINES = {
    "checkin": {"ar": "معي؟ خذ راحتك.", "en": "Are you still there? Take your time."},
    "offer_file": {"ar": "إذا ما لقيت رقم الملف، أقدر أدور ملفك بتاريخ الميلاد ورقم التلفون.",
                   "en": "If you can't find your file number, I can find your file with your date of birth and "
                         "phone number."},
    "offer_help": {"ar": "تحتاج مساعدة في شي؟ أنا معك.", "en": "Do you still need help? I'm here."},
    "goodbye": {"ar": "بقفل الخط الحين، اتصل علينا أي وقت.", "en": "I'll end the call now. Please call us any time."},
}
ALL_LINES = [text for line in LINES.values() for text in line.values()]

# The caller said they need a moment (e.g. while finding the file number)
_WAIT = re.compile(r"لحظة|لحظه|دقيقة|دقيقه|ثانية|ثانيه|ثواني|انتظر|اصبر|خلني اشوف|خلني أشوف|wait|moment|hold on|"
                   r"one sec|a sec|let me (?:check|find|look)", re.IGNORECASE)
_FILE_NUMBER = re.compile(r"رقم الملف|رقم ملف|file number", re.IGNORECASE)


class SilenceWatch:
    """`say(text)` speaks a line (pre-recorded if available); `hang_up()` ends the call; `history()` returns the
    conversation (a LiveKit ChatContext). All injected, so the steps can be tested without a call."""

    def __init__(self, say: Callable[[str], Awaitable[None]], hang_up: Callable[[], Awaitable[None]],
                 history: Callable[[], object], language: Callable[[object], str | None]):
        self._say = say
        self._hang_up = hang_up
        self._history = history
        self._language = language
        self.step = 0
        self._patience_used = False
        self._task: asyncio.Task | None = None

    def on_user_state(self, new_state: str) -> None:
        if new_state == "speaking":   # the caller is back: start over
            self.step, self._patience_used = 0, False
            if self._task and not self._task.done():
                self._task.cancel()
        elif new_state == "away" and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(self._next_step())

    def _last(self, role: str) -> str:
        """The last message from `role`, skipping our own silence lines (a check-in isn't what the agent asked)."""
        for item in reversed(getattr(self._history(), "items", [])):
            text = getattr(item, "text_content", None)
            if getattr(item, "role", None) == role and text and text not in ALL_LINES:
                return text
        return ""

    async def _speak(self, key: str) -> None:
        language = self._language(self._history())
        for lang in ([language] if language else ["ar", "en"]):   # caller hasn't spoken yet: both languages
            await self._say(LINES[key][lang])

    async def _next_step(self) -> None:
        if self.step == 0 and not self._patience_used and _WAIT.search(self._last("user")):
            self._patience_used = True   # they asked for a moment: let this silence pass once
            log.info("caller asked for a moment: waiting longer")
            return
        self.step += 1
        if self.step == 1:
            log.info("silent: checking in")
            await self._speak("checkin")
        elif self.step == 2:
            asked_file = bool(_FILE_NUMBER.search(self._last("assistant")))
            log.info("still silent: offering help")
            await self._speak("offer_file" if asked_file else "offer_help")
        else:
            log.info("still silent: ending the call")
            await self._speak("goodbye")
            await self._hang_up()
