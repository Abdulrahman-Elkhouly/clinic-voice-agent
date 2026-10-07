"""Evaluate the agent's RAG and safety behaviour against eval/test_set.md, without voice.

    python eval\eval_rag.py          # retrieval only (offline)
    python eval\eval_rag.py --llm    # retrieval + Groq answers + checks
    python eval\eval_rag.py --chat   # type your own questions, get retrieval + answers
"""
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from rag import KnowledgeBase  # noqa: E402

K = 3
SECTIONS = ("answerable", "not_in_kb", "safety")

# Same tool the live agent has, in OpenAI function-calling format.
# The model decides whether to search, exactly as on a call.
SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_knowledge_base",
        "description": "Search the clinic's documentation: about the clinic, opening hours, doctors and "
                       "specialties, services and prices, insurance and payment, appointment policies, "
                       "visit preparation, and after the visit.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "A short, specific search query."}},
            "required": ["query"],
        },
    },
}


def agent_prompt() -> str:
    """The live agent's real instructions, so the eval can never drift from agent.py."""
    from agent import InfoAgent
    return InfoAgent().instructions


def load_test_set() -> list[dict]:
    text = (ROOT / "eval" / "test_set.md").read_text(encoding="utf-8")
    parts = re.split(r"^## (?:Questions outside the knowledge base|Safety questions)\s*$", text, flags=re.M)
    tests = []
    for section, kind in zip(parts, SECTIONS):
        for block in re.split(r"\n(?=\d+\. )", section):
            q = re.search(r"\*\*Question:\*\*\s*(.+?)\s*$", block, re.M)
            if not q:
                continue
            expected = re.search(r"\*\*(?:Correct answer|Expected response):\*\*\s*(.+?)\s*$", block, re.M)
            source = re.search(r"\*\*Source:\*\*\s*`(.+?)`", block)
            check = re.search(r"\*\*Check:\*\*\s*(.+?)\s*$", block, re.M)
            tests.append({
                "question": q.group(1),
                "expected": expected.group(1) if expected else "",
                "source": source.group(1) if source else None,
                "check": check.group(1) if check else "",
                "kind": kind,
            })
    return tests


def run_checks(check: str, reply: str, searched: bool) -> list[str]:
    """Check line format: includes "a|b"; excludes "c|d"; no search. Returns the failures."""
    failures = []
    for rule in filter(None, (r.strip() for r in check.split(";"))):
        if rule == "no search":
            if searched:
                failures.append("searched the knowledge base")
            continue
        m = re.fullmatch(r'(includes|excludes) "(.+)"', rule)
        if not m:
            failures.append(f"bad check rule: {rule}")
            continue
        found = any(w.lower() in reply.lower() for w in m.group(2).split("|"))
        if m.group(1) == "includes" and not found:
            failures.append(f'missing "{m.group(2)}"')
        if m.group(1) == "excludes" and found:
            failures.append(f'contains "{m.group(2)}"')
    return failures


def ask_agent(client, kb: KnowledgeBase, prompt: str, question: str) -> tuple[str, list[str]]:
    """One caller turn with the real prompt and tool. Returns the reply and the searches made."""
    messages = [{"role": "system", "content": prompt}, {"role": "user", "content": question}]
    queries = []
    for _ in range(4):   # a few tool rounds at most
        msg = client.chat.completions.create(
            model="openai/gpt-oss-120b", messages=messages, tools=[SEARCH_TOOL],
        ).choices[0].message
        if not msg.tool_calls:
            return (msg.content or "").strip(), queries
        messages.append(msg)
        for call in msg.tool_calls:
            query = json.loads(call.function.arguments).get("query", "")
            queries.append(query)
            results = kb.search(query, K)
            messages.append({
                "role": "tool", "tool_call_id": call.id,
                "content": "\n\n---\n\n".join(c["text"] for _, c in results),
            })
    return "(no reply: too many tool calls)", queries


def make_client():
    from dotenv import load_dotenv
    from openai import OpenAI  # Groq's API is OpenAI-compatible

    load_dotenv(ROOT / ".env")
    return OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")


def chat(kb: KnowledgeBase, client, prompt: str):
    print("\nAsk the clinic anything (empty line to quit).")
    while True:
        question = input("\nYou: ").strip()
        if not question:
            break
        reply, queries = ask_agent(client, kb, prompt, question)
        for q in queries:
            print(f"   [RAG] searched: {q}")
        print(f"Agent: {reply}")


def main():
    use_llm = "--llm" in sys.argv or "--chat" in sys.argv
    client = make_client() if use_llm else None
    prompt = agent_prompt() if use_llm else None

    kb = KnowledgeBase()
    if "--chat" in sys.argv:
        chat(kb, client, prompt)
        return

    tests = load_test_set()
    hit1 = hitk = answerable = 0
    passed = {kind: 0 for kind in SECTIONS}
    checked = {kind: 0 for kind in SECTIONS}

    for i, t in enumerate(tests, 1):
        print(f"\nQ{i} [{t['kind']}]: {t['question']}")

        if t["kind"] != "safety":   # retrieval is meaningless for safety questions
            results = kb.search(t["question"], K)
            sources = [c["source"] for _, c in results]
            for rank, (score, chunk) in enumerate(results, 1):
                mark = " <-- expected" if chunk["source"] == t["source"] else ""
                print(f"   {rank}. {score:.3f}  {chunk['source']}{mark}")
            if t["kind"] == "answerable":
                answerable += 1
                hit1 += sources[0] == t["source"]
                hitk += t["source"] in sources
            else:
                print("   (not in KB: top score should be low)")

        if client:
            reply, queries = ask_agent(client, kb, prompt, t["question"])
            print(f"   SEARCHED: {queries or 'nothing'}")
            print(f"   EXPECTED: {t['expected']}")
            print(f"   AGENT:    {reply}")
            if t["check"]:
                checked[t["kind"]] += 1
                failures = run_checks(t["check"], reply, bool(queries))
                if failures:
                    print(f"   FAIL: {', '.join(failures)}")
                else:
                    passed[t["kind"]] += 1
                    print("   PASS")

    print(f"\nRetrieval: hit@1 = {hit1}/{answerable}, hit@{K} = {hitk}/{answerable}")
    if client:
        for kind in SECTIONS:
            if checked[kind]:
                print(f"Checks [{kind}]: {passed[kind]}/{checked[kind]} passed")
        print("Answerable questions have no automatic check: compare AGENT with EXPECTED by eye.")


if __name__ == "__main__":
    main()
