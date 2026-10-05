from dataclasses import dataclass
from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentSession, RunContext, function_tool
from livekit.plugins import groq, silero
import asyncio
from dataclasses import dataclass
from rag import KnowledgeBase

load_dotenv()

# 1. The call's "file": one instance per call, shared by every tool
@dataclass
class CallData:
    name: str | None = None
    phone: str | None = None
    kb: KnowledgeBase | None = None   # shared resource, loaded in prewarm


SPEECH_RULES = """Your replies are spoken aloud, so:
- Keep answers to one to three short sentences.
- Never use lists, markdown, emojis, or URLs."""


class IntakeAgent(Agent):
    """Phase 1: the receptionist. Its only job is collecting details."""

    def __init__(self):
        super().__init__(instructions=f"""You are the receptionist for a company's support line.
Your ONLY job is to collect the caller's name and phone number.
Save the name with record_name and the number with record_phone, then read the number back digit by digit.
When both are saved and confirmed, call transfer_to_support.
If the caller asks anything else, tell them you'll connect them to support right after you have their details.
{SPEECH_RULES}""")

    async def on_enter(self):
        await self.session.generate_reply(instructions="Greet the caller and ask for their name.")

    @function_tool
    async def record_name(self, context: RunContext[CallData], name: str) -> str:
        """Save the caller's name. Only call this AFTER the caller has actually spoken their name.
        Never guess, and never use placeholders like "user" or "caller".

        Args:
            name: The caller's name exactly as they said it.
        """
        if name.strip().lower() in {"", "user", "caller", "customer", "unknown"}:
            print(f"[TOOL] record_name REJECTED -> {name}")
            return "You don't know the caller's name yet. Ask them for it."
        context.userdata.name = name
        print(f"[TOOL] record_name -> {name}")
        return f"Name saved as {name}."


    @function_tool
    async def record_phone(self, context: RunContext[CallData], phone: str) -> str:
        """Save the caller's phone number. Call this when the caller says their phone number.

        Args:
            phone: The phone number as spoken, e.g. "1234 5678".
        """
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) != 8:
            print(f"[TOOL] record_phone REJECTED -> {phone}")
            return f"'{phone}' is not a valid 8-digit phone number. Ask the caller to repeat it slowly."
        context.userdata.phone = digits
        print(f"[TOOL] record_phone -> {digits}")
        return f"Phone saved as {digits}. Read it back digit by digit to confirm."


    @function_tool
    async def transfer_to_support(self, context: RunContext[CallData]):
        """Transfer the caller to the support agent. Call this only once the name and
        phone number are both saved and the caller has confirmed the number."""
        data = context.userdata
        if not data.name or not data.phone:
            print("[TOOL] transfer REJECTED: missing details")
            return "You can't transfer yet. You still need the caller's name and phone number."
        print(f"[HANDOFF] Intake -> Support ({data.name}, {data.phone})")
        # Returning an Agent is what triggers the handoff
        return SupportAgent(chat_ctx=self.chat_ctx)


class SupportAgent(Agent):
    """Phase 2: answers questions using the knowledge base."""

    def __init__(self, chat_ctx=None):
        super().__init__(
            instructions=f"""You are a friendly support agent for Nova Mobile, a mobile network in Qatar.
The caller has already given their details to the receptionist, so never ask for them again.

For ANY question about Nova Mobile (plans, prices, roaming, billing, SIM, eSIM, stores, policies),
call search_knowledge_base first, then answer ONLY from what it returns.
If the answer isn't in the results, say you don't have that information and offer to connect them with a human.
Never guess prices, numbers, or policies.
{SPEECH_RULES}""",
            chat_ctx=chat_ctx,
        )

    async def on_enter(self):
        name = self.session.userdata.name
        await self.session.generate_reply(
            instructions=f"Greet {name} by name and ask how you can help. "
                         "If they already mentioned a problem earlier, refer to it."
        )

    @function_tool
    async def search_knowledge_base(self, context: RunContext[CallData], query: str) -> str:
        """Search Nova Mobile's documentation: prepaid and postpaid plans, prices, roaming,
        billing and payments, SIM and eSIM, and general FAQs.

        Args:
            query: A short, specific search query, e.g. "prepaid plan 10 GB price".
        """
        # search() is CPU work; run it in a thread so audio doesn't stutter
        results = await asyncio.to_thread(context.userdata.kb.search, query, 3)
        print(f"[RAG] query: {query}")
        for score, chunk in results:
            print(f"[RAG]   {score:.3f}  {chunk['source']}")
        return "\n\n---\n\n".join(chunk["text"] for _, chunk in results)


def prewarm(proc: agents.JobProcess):
    # Runs once per worker process, before any call
    proc.userdata["vad"] = silero.VAD.load()
    proc.userdata["kb"] = KnowledgeBase()


async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    session = AgentSession[CallData](
        userdata=CallData(kb=ctx.proc.userdata["kb"]),
        stt="deepgram/nova-3",
        llm=groq.LLM(model="openai/gpt-oss-120b"),
        tts="cartesia/sonic-2",
        vad=ctx.proc.userdata["vad"],
    )


    await session.start(room=ctx.room, agent=IntakeAgent())


if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))



