"""Tests for what the agent does when the caller goes quiet (agent/silence.py). Simulated "away" events, no call.

    python -m pytest tests -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from livekit.agents import llm  # noqa: E402

import agent  # noqa: E402
from silence import LINES, SilenceWatch  # noqa: E402


class FakeCall:
    def __init__(self, *turns: tuple[str, str]):
        self.ctx = llm.ChatContext()
        for role, text in turns:
            self.ctx.add_message(role=role, content=text)
        self.said: list[str] = []
        self.hung_up = False
        self.watch = SilenceWatch(self.say, self.hang_up, lambda: self.ctx, agent.caller_language)

    async def say(self, text: str) -> None:
        self.said.append(text)
        self.ctx.add_message(role="assistant", content=text)   # as session.say adds it to the conversation

    async def hang_up(self) -> None:
        self.hung_up = True

    async def silence(self, times: int = 1) -> None:
        for _ in range(times):
            self.watch.on_user_state("away")
            await self.watch._task


@pytest.mark.asyncio
async def test_check_in_then_offer_help_then_goodbye_in_the_callers_language():
    call = FakeCall(("assistant", "هلا"), ("user", "أبي أحجز موعد"), ("assistant", "عندك ملف عندنا؟"))
    await call.silence(3)
    assert call.said == [LINES["checkin"]["ar"], LINES["offer_help"]["ar"], LINES["goodbye"]["ar"]]
    assert call.hung_up


@pytest.mark.asyncio
async def test_after_asking_for_the_file_number_it_offers_the_forgot_path():
    call = FakeCall(("user", "I have a file"), ("assistant", "Great, what's your file number and full name?"))
    await call.silence(2)   # the check-in comes in between, but it still knows what was asked before it
    assert call.said == [LINES["checkin"]["en"], LINES["offer_file"]["en"]]
    assert not call.hung_up


@pytest.mark.asyncio
async def test_a_caller_who_asked_for_a_moment_gets_longer_first():
    call = FakeCall(("assistant", "شو رقم ملفك؟"), ("user", "لحظة أدور عليه"))
    await call.silence()
    assert call.said == []                                  # the first silence just passes
    await call.silence()
    assert call.said == [LINES["checkin"]["ar"]]


@pytest.mark.asyncio
async def test_speaking_starts_over():
    call = FakeCall(("user", "Where do I park?"))
    await call.silence(2)
    call.watch.on_user_state("speaking")
    await call.silence()
    assert call.said == [LINES["checkin"]["en"], LINES["offer_help"]["en"], LINES["checkin"]["en"]]
    assert not call.hung_up


@pytest.mark.asyncio
async def test_before_the_caller_has_spoken_both_languages():
    call = FakeCall(("assistant", "مركز ازغوى الطبي، شلون اقدر اساعدك؟ Welcome to Izghawa Medical Center"))
    await call.silence()
    assert call.said == [LINES["checkin"]["ar"], LINES["checkin"]["en"]]
