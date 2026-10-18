import asyncio
import uuid
from dataclasses import dataclass, field

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import groq, silero

import booking
import identity
from booking import BookingState
from db import ClinicDB, now_qatar
from identity import CallIdentity
from prompts import CLINIC, booking_instructions, reception_instructions
from rag import SEARCH_TIMEOUT, KnowledgeBase, KnowledgeBaseUnavailable

load_dotenv()

# Two network calls per search (Voyage, then Qdrant), each with its own SEARCH_TIMEOUT; this caps the whole search
SEARCH_DEADLINE = 2 * SEARCH_TIMEOUT
KB_DOWN = ("The clinic information can't be looked up right now. Tell the caller you can't check that at the moment, "
           "don't guess, and offer to transfer them to a member of staff.")


# The call's "file": one instance per call, shared by every tool and every agent of the call
@dataclass
class CallData:
    kb: KnowledgeBase | None = None   # shared resources, loaded in prewarm
    db: ClinicDB | None = None
    identity: CallIdentity = field(default_factory=CallIdentity)   # who is verified on this call
    booking: BookingState = field(default_factory=BookingState)    # pending booking action, listed appointments
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)  # owns this call's slot holds


def user_turns(agent: Agent) -> int:
    """How many times the caller has spoken in this conversation. Confirmation tools require this to have
    grown since the read-back, so the model can't confirm in the same turn it read the details out."""
    return sum(1 for item in agent.chat_ctx.items if getattr(item, "role", None) == "user")


# Shared by every agent, so small questions never need a handoff
@function_tool
async def search_knowledge_base(context: RunContext[CallData], query: str) -> str:
    """Search the clinic's documentation (doctors, hours, services, prices, insurance, policies, visits, results).

    Args:
        query: A short, specific query, e.g. "female dermatologist days".
    """
    # search() is blocking network I/O (Voyage, then Qdrant); run it in a thread so audio doesn't stutter
    try:
        results = await asyncio.wait_for(asyncio.to_thread(context.userdata.kb.search, query, 3),
                                         SEARCH_DEADLINE)
    except (KnowledgeBaseUnavailable, asyncio.TimeoutError) as e:
        print(f"[RAG] search failed: {type(e).__name__}")
        return KB_DOWN
    print(f"[RAG] query: {query}")
    for score, chunk in results:
        print(f"[RAG]   {score:.3f}  {chunk['id']}")
    return "\n\n---\n\n".join(chunk["text"] for _, chunk in results)


# Stubs for tools the prompts already name but later stages build (transfer: Stage 6, requests: Stage 4b).
# Without them the model still tries to call these names, the LLM provider rejects the unknown tool,
# and the turn fails with no reply at all (found by the conversation tests during an emergency).
TRANSFER_NOT_YET = ("Transferring to staff isn't connected yet. Apologise that no one can be reached right now and "
                    "keep helping with what you can. In an emergency, tell them to call 999.")
NOT_YET = "This isn't available yet. Apologise, and offer to transfer the caller to a member of staff."


@function_tool
async def transfer_to_human(context: RunContext[CallData], reason: str) -> str:
    """Transfer the call to a member of staff.

    Args:
        reason: Why, in a few words.
    """
    print(f"[STUB] transfer_to_human: {reason}")
    return TRANSFER_NOT_YET


@function_tool
async def log_request(context: RunContext[CallData], request_type: str, details: str) -> str:
    """Record a request for staff (message, callback, refill, report, sick leave, complaint).

    Args:
        request_type: e.g. "callback".
        details: What the caller needs.
    """
    print(f"[STUB] log_request: {request_type}")
    return NOT_YET


SHARED_TOOLS = [search_knowledge_base, transfer_to_human, log_request]

# The prompt says to reply in the caller's language, but everything else the model sees (instructions, its greeting,
# knowledge base results) is English, and in tests it sometimes answered Arabic questions in English.
ARABIC_REPLY = "The caller is speaking Arabic. Write your reply in Arabic."


def arabic_share(text: str) -> float:
    """Share of letters that are Arabic script (names and insurers may be in English inside an Arabic sentence)."""
    letters = [ch for ch in text if ch.isalpha()]
    return sum("؀" <= ch <= "ۿ" for ch in letters) / max(1, len(letters))


def caller_speaks_arabic(chat_ctx) -> bool:
    last = next((item for item in reversed(chat_ctx.items) if getattr(item, "role", None) == "user"), None)
    return last is not None and arabic_share(last.text_content or "") > 0.5


class ClinicAgent(Agent):
    """Base for every agent of the call."""

    def llm_node(self, chat_ctx, tools, model_settings):
        # Runs before every LLM call, including the one after a tool result. The reminder goes last, on a copy,
        # so it applies to this reply only and is never stored in the conversation.
        if caller_speaks_arabic(chat_ctx):
            chat_ctx = chat_ctx.copy()
            chat_ctx.add_message(role="system", content=ARABIC_REPLY)
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


def handoff_ctx(agent: Agent):
    """The conversation for the next agent: what was said, without the previous agent's tool calls and results
    (fewer tokens per request, and the next agent never sees the raw details sent to identity tools)."""
    return agent.chat_ctx.copy(exclude_function_call=True)


class ReceptionAgent(ClinicAgent):
    """Greets callers, answers general questions, and identifies patients.
    Personal work (booking, requests) happens in agents created only after verification."""

    def __init__(self, chat_ctx=None, returning: bool = False):
        super().__init__(instructions=reception_instructions(), tools=SHARED_TOOLS, chat_ctx=chat_ctx)
        self._returning = returning   # coming back from BookingAgent: don't greet again

    async def on_enter(self):
        if self._returning:
            await self.session.generate_reply(
                instructions="Continue the conversation without greeting again: help with what the caller just asked.")
        else:
            await self.session.generate_reply(
                instructions=f"Greet the caller, say they've reached {CLINIC}, and ask how you can help.")

    @function_tool
    async def start_booking(self, context: RunContext[CallData]):
        """Hand over to booking for the identified patient: book, cancel, move, or check appointments."""
        d = context.userdata
        if d.identity.patient_id is None:   # enforced here, not left to the prompt
            return "No patient is identified on this call yet. Identify the patient first, then call start_booking."
        print(f"[HANDOFF] Reception -> Booking (patient_id={d.identity.patient_id})")
        return BookingAgent(d.identity.patient_id, d.identity.patient_first_name, chat_ctx=handoff_ctx(self))

    # Identification rules live in identity.py (enforced in code); these tools only pass the caller's words through.
    # The database is blocking I/O, so each call runs in a thread.

    @function_tool
    async def verify_patient(self, context: RunContext[CallData], file_number: str, date_of_birth: str) -> str:
        """Verify a returning patient by file number and date of birth.

        Args:
            file_number: Digits only.
            date_of_birth: YYYY-MM-DD.
        """
        d = context.userdata
        return await asyncio.to_thread(identity.verify, d.db, d.identity, file_number, date_of_birth)

    @function_tool
    async def find_patient(self, context: RunContext[CallData], date_of_birth: str, phone: str,
                           full_name: str | None) -> str:
        """Find a returning patient who forgot their file number.

        Args:
            date_of_birth: YYYY-MM-DD.
            phone: The phone number on their file, digits only.
            full_name: Null unless a previous find_patient result asked for it.
        """
        d = context.userdata
        return await asyncio.to_thread(identity.find, d.db, d.identity, date_of_birth, phone, full_name)

    @function_tool
    async def register_patient(self, context: RunContext[CallData], full_name: str, date_of_birth: str,
                               phone: str, guardian_phone: str | None) -> str:
        """Step 1 of registering a new patient: checks the details and returns the read-back. Creates nothing.

        Args:
            full_name: As they spelled it.
            date_of_birth: YYYY-MM-DD.
            phone: The patient's mobile, digits only.
            guardian_phone: A child's parent or guardian number, digits only, or null.
        """
        return identity.prepare_registration(context.userdata.identity, full_name, date_of_birth, phone,
                                             guardian_phone, user_turns(self))

    @function_tool
    async def confirm_registration(self, context: RunContext[CallData]) -> str:
        """Step 2: create the file, only after the caller heard the read-back and said yes."""
        d = context.userdata
        return await asyncio.to_thread(identity.confirm_registration, d.db, d.identity, user_turns(self))


class BookingAgent(ClinicAgent):
    """Appointments for ONE verified patient. Created only by ReceptionAgent.start_booking, bound to that
    patient: every tool uses self.patient_id, so the model has no way to act on anyone else."""

    def __init__(self, patient_id: int, first_name: str, chat_ctx=None):
        super().__init__(instructions=booking_instructions(first_name, now_qatar()), tools=SHARED_TOOLS,
                         chat_ctx=chat_ctx)
        self.patient_id = patient_id

    async def on_enter(self):
        await self.session.generate_reply(
            instructions="Continue without greeting again: help with the appointment the caller asked about.")

    async def _run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)   # database work off the audio thread

    @function_tool
    async def list_doctors(self, context: RunContext[CallData], specialty: str | None, gender: str | None,
                           language: str | None) -> str:
        """The clinic's doctors with their exact names.

        Args:
            specialty: e.g. "ENT", or null.
            gender: "female", "male", or null.
            language: e.g. "Urdu", or null.
        """
        return await self._run(booking.doctors_text, context.userdata.db, specialty, gender, language)

    # LiveKit sends tools to Groq in strict mode: every parameter must be present, so optional ones are sent as
    # null. The model sometimes omits them and the call is rejected, so tools keep optional parameters few.

    @function_tool
    async def check_availability(self, context: RunContext[CallData], doctor_name: str, date_from: str,
                                 date_to: str | None, part_of_day: str | None) -> str:
        """Earliest free times with one doctor.

        Args:
            doctor_name: Exact name, e.g. "Dr. Omar Benali".
            date_from: YYYY-MM-DD.
            date_to: YYYY-MM-DD, or null for date_from only.
            part_of_day: "morning", "afternoon", "evening", or null.
        """
        return await self._run(booking.check_availability, context.userdata.db, self.patient_id, date_from,
                               date_to, doctor_name, None, part_of_day)

    @function_tool
    async def check_specialty_availability(self, context: RunContext[CallData], specialty: str, date_from: str,
                                           date_to: str | None, part_of_day: str | None, gender: str | None,
                                           language: str | None) -> str:
        """Earliest free times with any doctor in a specialty.

        Args:
            specialty: e.g. "orthopedics".
            date_from: YYYY-MM-DD.
            date_to: YYYY-MM-DD, or null for date_from only.
            part_of_day: "morning", "afternoon", "evening", or null.
            gender: "female", "male", or null.
            language: A language the doctor speaks, or null.
        """
        return await self._run(booking.check_availability, context.userdata.db, self.patient_id, date_from,
                               date_to, None, specialty, part_of_day, gender, language)

    @function_tool
    async def book_appointment(self, context: RunContext[CallData], doctor_name: str, start: str) -> str:
        """Step 1 of booking: holds the picked slot and returns the read-back. Books nothing.

        Args:
            doctor_name: Exactly as returned by availability.
            start: Exactly as returned, "YYYY-MM-DD HH:MM".
        """
        d = context.userdata
        return await self._run(booking.book, d.db, d.booking, self.patient_id, doctor_name, start, d.call_id,
                               user_turns(self))

    @function_tool
    async def list_my_appointments(self, context: RunContext[CallData]) -> str:
        """This patient's upcoming appointments."""
        d = context.userdata
        return await self._run(booking.list_appointments, d.db, d.booking, self.patient_id)

    @function_tool
    async def cancel_appointment(self, context: RunContext[CallData], ref: int) -> str:
        """Step 1 of cancelling: returns the read-back. Cancels nothing.

        Args:
            ref: From list_my_appointments.
        """
        d = context.userdata
        return await self._run(booking.cancel, d.db, d.booking, self.patient_id, ref, user_turns(self))

    @function_tool
    async def reschedule_appointment(self, context: RunContext[CallData], ref: int, doctor_name: str,
                                     start: str) -> str:
        """Step 1 of moving an appointment: holds the new time and returns the read-back. Changes nothing.

        Args:
            ref: From list_my_appointments.
            doctor_name: Exactly as returned by availability.
            start: Exactly as returned, "YYYY-MM-DD HH:MM".
        """
        d = context.userdata
        return await self._run(booking.reschedule, d.db, d.booking, self.patient_id, ref, doctor_name, start,
                               d.call_id, user_turns(self))

    @function_tool
    async def confirm(self, context: RunContext[CallData]) -> str:
        """Step 2 of the pending booking, cancellation, or move: only after the caller heard the read-back and
        said yes."""
        d = context.userdata
        return await self._run(booking.confirm, d.db, d.booking, self.patient_id, user_turns(self))

    @function_tool
    async def add_to_waitlist(self, context: RunContext[CallData], date_from: str, date_to: str,
                              doctor_name: str | None, specialty: str | None) -> str:
        """Add the patient to the waitlist when no time suits. Read back the details first.

        Args:
            date_from: Earliest date, YYYY-MM-DD.
            date_to: Latest date, YYYY-MM-DD.
            doctor_name: Exact name, or null if specialty is given.
            specialty: Or null if doctor_name is given.
        """
        return await self._run(booking.add_to_waitlist, context.userdata.db, self.patient_id, date_from, date_to,
                               doctor_name, specialty)

    @function_tool
    async def back_to_reception(self, context: RunContext[CallData], reason: str):
        """Hand back to reception: for another patient, or anything that isn't this patient's appointments.

        Args:
            reason: What the caller wants now.
        """
        d = context.userdata
        released = await self._run(lambda: d.db.release_hold(call_id=d.call_id))   # don't leave a slot held
        d.booking.pending = None
        print(f"[HANDOFF] Booking -> Reception ({reason}; released {released} hold(s))")
        return ReceptionAgent(chat_ctx=handoff_ctx(self), returning=True)


def prewarm(proc: agents.JobProcess):
    # Runs once per worker process, before any call
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["kb"] = KnowledgeBase()
    proc.userdata["db"] = ClinicDB()


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    data = CallData(kb=ctx.proc.userdata["kb"], db=ctx.proc.userdata["db"])
    session = AgentSession[CallData](
        userdata=data,
        stt="deepgram/nova-3",
        llm=groq.LLM(model="openai/gpt-oss-120b"),
        tts="cartesia/sonic-2",
        vad=ctx.proc.userdata["vad"],
    )

    # Caller hung up (or the call ended): release any slot this call was holding right away.
    # The 5-minute hold expiry is the backup if this never runs (e.g. the process crashes).
    async def release_holds():
        released = await asyncio.to_thread(data.db.release_hold, call_id=data.call_id)
        print(f"[CALL] ended; released {released} hold(s)")
    ctx.add_shutdown_callback(release_holds)

    await session.start(room=ctx.room, agent=ReceptionAgent())


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
