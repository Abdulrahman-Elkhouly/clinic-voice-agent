"""Evaluate the RAG pipeline against eval/test_set.md, without voice.

    python eval\eval_rag.py          # retrieval only (offline)
    python eval\eval_rag.py --llm    # retrieval + Groq answers
    python eval\eval_rag.py --chat   # type your own questions, get retrieval + answers
"""
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from rag import KnowledgeBase  # noqa: E402

K = 3

# Mirrors SupportAgent's grounding rules; keep the two in sync
SYSTEM_PROMPT = """You are a friendly support agent for Nova Mobile, a mobile network in Qatar.
Answer ONLY from the retrieved information below.
If the answer isn't in it, say you don't have that information and offer to connect them with a human.
Never guess prices, numbers, or policies.
Keep answers to one to three short sentences. Never use lists, markdown, emojis, or URLs.

Retrieved information:
{context}"""


def load_test_set() -> list[dict]:
    text = (ROOT / "eval" / "test_set.md").read_text(encoding="utf-8")
    answerable, unanswerable = text.split("## Questions outside the knowledge base")
    tests = []
    for section, in_kb in ((answerable, True), (unanswerable, False)):
        for block in re.split(r"\n(?=\d+\. )", section):
            q = re.search(r"\*\*Question:\*\*\s*(.+?)\s*$", block, re.M)
            if not q:
                continue
            expected = re.search(r"\*\*(?:Correct answer|Expected response):\*\*\s*(.+?)\s*$", block, re.M)
            source = re.search(r"\*\*Source:\*\*\s*`(.+?)`", block)
            tests.append({
                "question": q.group(1),
                "expected": expected.group(1) if expected else "",
                "source": source.group(1) if source else None,
                "in_kb": in_kb,
            })
    return tests


def ask_llm(client, question: str, chunks: list[dict]) -> str:
    context = "\n\n---\n\n".join(c["text"] for c in chunks)
    resp = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
            {"role": "user", "content": question},
        ],
    )
    return resp.choices[0].message.content.strip()


def make_client():
    from dotenv import load_dotenv
    from openai import OpenAI  # Groq's API is OpenAI-compatible

    load_dotenv(ROOT / ".env")
    return OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url="https://api.groq.com/openai/v1")


def chat(kb: KnowledgeBase, client):
    print("\nAsk Nova Mobile support anything (empty line to quit).")
    while True:
        question = input("\nYou: ").strip()
        if not question:
            break
        results = kb.search(question, K)
        for score, chunk in results:
            print(f"   [RAG] {score:.3f}  {chunk['source']}")
        print(f"Agent: {ask_llm(client, question, [c for _, c in results])}")


def main():
    use_llm = "--llm" in sys.argv or "--chat" in sys.argv
    client = make_client() if use_llm else None

    kb = KnowledgeBase()
    if "--chat" in sys.argv:
        chat(kb, client)
        return

    tests = load_test_set()
    hit1 = hitk = answerable = 0

    for i, t in enumerate(tests, 1):
        results = kb.search(t["question"], K)
        sources = [c["source"] for _, c in results]
        print(f"\nQ{i}: {t['question']}")
        for rank, (score, chunk) in enumerate(results, 1):
            mark = " <-- expected" if chunk["source"] == t["source"] else ""
            print(f"   {rank}. {score:.3f}  {chunk['source']}{mark}")

        if t["in_kb"]:
            answerable += 1
            hit1 += sources[0] == t["source"]
            hitk += t["source"] in sources
        else:
            print("   (not in KB: top score should be low)")

        if client:
            print(f"   EXPECTED: {t['expected']}")
            print(f"   AGENT:    {ask_llm(client, t['question'], [c for _, c in results])}")

    print(f"\nRetrieval: hit@1 = {hit1}/{answerable}, hit@{K} = {hitk}/{answerable}")


if __name__ == "__main__":
    main()
