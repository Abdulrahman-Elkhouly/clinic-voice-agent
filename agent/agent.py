import asyncio
import uuid
from dataclasses import dataclass, field

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import groq, silero

import identity
from db import ClinicDB
from identity import CallIdentity
from prompts import CLINIC, reception_instructions
from rag import KnowledgeBase

load_dotenv()


# The call's "file": one instance per call, shared by every tool and every agent of the call
@dataclass
class CallData:
    kb: KnowledgeBase | None = None   # shared resources, loaded in prewarm
    db: ClinicDB | None = None
    identity: CallIdentity = field(default_factory=CallIdentity)   # who is verified on this call
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


# Shared by every agent (Reception now; Booking and Requests in Stage 4), so small questions never need a handoff
@function_tool
async def search_knowledge_base(context: RunContext[CallData], query: str) -> str:
    """Search the clinic's documentation: about the clinic (location, parking, contact), opening hours,
    doctors and specialties, services and prices, insurance and payment, appointment policies,
    visit preparation, and after the visit (results, refills, reports, sick leave, complaints).

    Args:
        query: A short, specific search query, e.g. "female dermatologist days".
    """
    # search() is CPU work; run it in a thread so audio doesn't stutter
    results = await asyncio.to_thread(context.userdata.kb.search, query, 3)
    print(f"[RAG] query: {query}")
    for score, chunk in results:
        print(f"[RAG]   {score:.3f}  {chunk['id']}")
    return "\n\n---\n\n".join(chunk["text"] for _, chunk in results)


# Stubs for tools the prompts already name but later stages build (transfer: Stage 6, requests: Stage 4b).
# Without them the model still tries to call these names, the LLM provider rejects the unknown tool,
# and the turn fails with no reply at all (found by the conversation tests during an emergency).
TRANSFER_NOT_YET = ("Transferring to staff isn't connected yet. Apologise that no one can be reached right now and "
                    "keep helping with what you can. Don't give a number to call instead. "
                    "In an emergency, tell them to call 999.")
NOT_YET = "This isn't available yet. Apologise, and offer to transfer the caller to a member of staff."


@function_tool
async def transfer_to_human(context: RunContext[CallData], reason: str) -> str:
    """Transfer the call to a member of staff.

    Args:
        reason: Why the caller needs a person, in a few words.
    """
    print(f"[STUB] transfer_to_human: {reason}")
    return TRANSFER_NOT_YET


@function_tool
async def log_request(context: RunContext[CallData], request_type: str, details: str) -> str:
    """Record a request for staff: a message, callback, refill, report, sick leave note, or complaint.

    Args:
        request_type: The kind of request, e.g. "callback".
        details: What the caller needs.
    """
    print(f"[STUB] log_request: {request_type}")
    return NOT_YET


SHARED_TOOLS = [search_knowledge_base, transfer_to_human, log_request]


class ReceptionAgent(Agent):
    """Greets callers, answers general questions, and identifies patients.
    Personal work (booking, requests) happens in agents created only after verification (Stage 4)."""

    def __init__(self, chat_ctx=None):
        super().__init__(instructions=reception_instructions(), tools=SHARED_TOOLS, chat_ctx=chat_ctx)

    async def on_enter(self):
        await self.session.generate_reply(
            instructions=f"Greet the caller, say they've reached {CLINIC}, and ask how you can help."
        )

    # Identification rules live in identity.py (enforced in code); these tools only pass the caller's words through.
    # The database is blocking I/O, so each call runs in a thread.

    @function_tool
    async def verify_patient(self, context: RunContext[CallData], file_number: str, date_of_birth: str) -> str:
        """Verify a returning patient who knows their file number, using their file number and date of birth.

        Args:
            file_number: The file number as digits, e.g. "938100".
            date_of_birth: The patient's date of birth as YYYY-MM-DD.
        """
        d = context.userdata
        return await asyncio.to_thread(identity.verify, d.db, d.identity, file_number, date_of_birth)

    @function_tool
    async def find_patient(self, context: RunContext[CallData], date_of_birth: str, phone: str,
                           full_name: str | None = None) -> str:
        """Find a returning patient who forgot their file number, by date of birth and the phone number on
        their file. Leave full_name empty unless a previous find_patient result asked for it.

        Args:
            date_of_birth: The patient's date of birth as YYYY-MM-DD.
            phone: The phone number on the patient's file, as digits.
            full_name: The patient's full name, only when the tool asked for it.
        """
        d = context.userdata
        return await asyncio.to_thread(identity.find, d.db, d.identity, date_of_birth, phone, full_name)

    def _user_turns(self) -> int:
        """How many times the caller has spoken in this conversation (for the confirmation gate)."""
        return sum(1 for item in self.chat_ctx.items if getattr(item, "role", None) == "user")

    @function_tool
    async def register_patient(self, context: RunContext[CallData], full_name: str, date_of_birth: str,
                               phone: str, guardian_phone: str | None = None) -> str:
        """Step 1 of registering a new patient: checks the details and tells you what to read back.
        It does not create the file; confirm_registration does, after the caller confirms.

        Args:
            full_name: The patient's full name as they spelled it.
            date_of_birth: The patient's date of birth as YYYY-MM-DD.
            phone: The patient's mobile number, as digits.
            guardian_phone: For a child, the parent's or guardian's number, as digits.
        """
        return identity.prepare_registration(context.userdata.identity, full_name, date_of_birth, phone,
                                             guardian_phone, self._user_turns())

    @function_tool
    async def confirm_registration(self, context: RunContext[CallData]) -> str:
        """Step 2: create the new patient's file. Call only after the caller has heard the details read back
        and said they are correct."""
        d = context.userdata
        return await asyncio.to_thread(identity.confirm_registration, d.db, d.identity, self._user_turns())


def prewarm(proc: agents.JobProcess):
    # Runs once per worker process, before any call
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["kb"] = KnowledgeBase()
    proc.userdata["db"] = ClinicDB()


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    session = AgentSession[CallData](
        userdata=CallData(kb=ctx.proc.userdata["kb"], db=ctx.proc.userdata["db"], call_id=ctx.room.name),
        stt="deepgram/nova-3",
        llm=groq.LLM(model="openai/gpt-oss-120b"),
        tts="cartesia/sonic-2",
        vad=ctx.proc.userdata["vad"],
    )

    await session.start(room=ctx.room, agent=ReceptionAgent())


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
