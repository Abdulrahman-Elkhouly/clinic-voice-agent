"""Which LLM the clinic uses, in one place: the conversation (through LiveKit) and the name judge (a plain call).

LLM_MODEL is "provider:model": "openai:gpt-6-luna" (default since 2026-10-21) or "groq:openai/gpt-oss-120b".
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

LLM_MODEL = os.environ.get("LLM_MODEL", "openai:gpt-6-luna")
PROVIDER, MODEL = LLM_MODEL.split(":", 1)
API_KEYS = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY"}
if PROVIDER not in API_KEYS:
    raise ValueError(f"Unknown LLM provider in LLM_MODEL: {LLM_MODEL}")

# gpt-6-luna reasons by default, and OpenAI rejects reasoning together with function tools on chat completions
# ("set reasoning_effort to 'none'"). No reasoning also means no hidden thinking time before the first word.
EXTRA = {"reasoning_effort": "none"} if PROVIDER == "openai" else {}


def make_llm():
    """The conversation LLM for an AgentSession."""
    if PROVIDER == "openai":
        from livekit.plugins import openai
        return openai.LLM(model=MODEL, **EXTRA)
    from livekit.plugins import groq
    return groq.LLM(model=MODEL)


def chat_client(timeout: float):
    """A plain OpenAI-compatible client for one-shot calls (the name judge)."""
    from openai import OpenAI
    base_url = "https://api.groq.com/openai/v1" if PROVIDER == "groq" else None
    return OpenAI(api_key=os.environ[API_KEYS[PROVIDER]], base_url=base_url, timeout=timeout)
