"""Tests for the knowledge base index (agent/rag.py): chunking checks, incremental sync, startup checks, failures.
No network: Qdrant runs in memory and a fake embedder stands in for Voyage.

    python -m pytest tests -v
"""
import asyncio
import hashlib
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import rag  # noqa: E402
from rag import KnowledgeBase, KnowledgeBaseUnavailable, load_chunks, sync_index  # noqa: E402


class FakeEmbedder:
    """Bag of words hashed into EMBED_DIM numbers: texts sharing words get similar vectors."""

    def __init__(self):
        self.embedded = 0

    def embed(self, texts, input_type):
        self.embedded += len(texts)
        vectors = []
        for t in texts:
            v = np.zeros(rag.EMBED_DIM)
            for word in t.lower().split():
                v[int(hashlib.md5(word.encode()).hexdigest(), 16) % rag.EMBED_DIM] += 1
            vectors.append((v / (np.linalg.norm(v) or 1)).tolist())
        return vectors


class BrokenEmbedder:
    def embed(self, texts, input_type):
        raise httpx.ConnectError("Voyage unreachable")


@pytest.fixture
def kb_dir(tmp_path, monkeypatch):
    (tmp_path / "hours.md").write_text("# Opening hours\n\n## Weekdays\nSat to Thu 8:00 to 22:00.\n\n"
                                       "## Friday\nFriday 16:00 to 22:00.\n", encoding="utf-8")
    (tmp_path / "parking.md").write_text("# Parking\n\nFree parking behind the building.\n", encoding="utf-8")
    monkeypatch.setattr(rag, "KB_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def client():
    return QdrantClient(":memory:")


# --- Chunking checks ---

def test_real_knowledge_base_has_unique_sections():
    chunks = load_chunks()
    assert len(chunks) == len({c["id"] for c in chunks}) > 0


def test_duplicate_heading_stops_indexing(kb_dir):
    (kb_dir / "hours.md").write_text("# Hours\n\n## Friday\nA\n\n## Friday\nB\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate headings"):
        load_chunks()


def test_empty_file_stops_indexing(kb_dir):
    (kb_dir / "empty.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty.md is empty"):
        load_chunks()


# --- Incremental sync ---

def test_first_sync_embeds_everything_and_a_rerun_embeds_nothing(kb_dir, client):
    emb = FakeEmbedder()
    assert sync_index(client, emb, load_chunks()) == {"embedded": 3, "deleted": 0, "total": 3}
    assert sync_index(client, emb, load_chunks()) == {"embedded": 0, "deleted": 0, "total": 3}
    assert emb.embedded == 3


def test_edited_section_is_re_embedded_alone(kb_dir, client):
    sync_index(client, FakeEmbedder(), load_chunks())
    (kb_dir / "parking.md").write_text("# Parking\n\nParking now costs 5 QAR.\n", encoding="utf-8")
    assert sync_index(client, FakeEmbedder(), load_chunks())["embedded"] == 1
    [(_, chunk)] = KnowledgeBase(client, FakeEmbedder()).search("parking cost", k=1)
    assert "5 QAR" in chunk["text"]   # the old text is gone, not just shadowed


def test_removed_or_renamed_section_leaves_nothing_stale(kb_dir, client):
    sync_index(client, FakeEmbedder(), load_chunks())
    (kb_dir / "hours.md").write_text("# Opening hours\n\n## Saturday to Thursday\nSat to Thu 8:00 to 22:00.\n",
                                     encoding="utf-8")
    assert sync_index(client, FakeEmbedder(), load_chunks()) == {"embedded": 1, "deleted": 2, "total": 2}
    ids = {c["id"] for _, c in KnowledgeBase(client, FakeEmbedder()).search("friday weekdays hours", k=10)}
    assert ids == {"hours.md > Saturday to Thursday", "parking.md > Parking"}


# --- Startup checks ---

def test_agent_refuses_to_start_without_an_index(kb_dir, client):
    with pytest.raises(RuntimeError, match="index_kb.py"):
        KnowledgeBase(client, FakeEmbedder())


def test_agent_warns_when_the_index_is_stale(kb_dir, client, capsys):
    sync_index(client, FakeEmbedder(), load_chunks())
    (kb_dir / "parking.md").write_text("# Parking\n\nChanged.\n", encoding="utf-8")
    KnowledgeBase(client, FakeEmbedder())
    assert "1 knowledge base section(s) changed" in capsys.readouterr().out


# --- Search ---

def test_search_returns_scored_sections(kb_dir, client):
    sync_index(client, FakeEmbedder(), load_chunks())
    [(score, chunk)] = KnowledgeBase(client, FakeEmbedder()).search("friday 16:00", k=1)
    assert chunk["id"] == "hours.md > Friday" and chunk["source"] == "hours.md" and 0 < score <= 1


def test_voyage_failure_becomes_unavailable(kb_dir, client):
    sync_index(client, FakeEmbedder(), load_chunks())
    with pytest.raises(KnowledgeBaseUnavailable):
        KnowledgeBase(client, BrokenEmbedder()).search("friday")


@pytest.mark.parametrize("text, arabic", [
    ("وين أوقف سيارتي لما أجيكم؟", True),
    ("عندكم تأمين QLM؟", True),            # an insurer's English name inside an Arabic question
    ("Where do I park?", False),
    ("Is Dr. Benali available? شكرا", False),
])
def test_caller_language_is_detected_from_the_last_message(text, arabic):
    from livekit.agents import llm
    import agent   # noqa: E402
    ctx = llm.ChatContext()
    ctx.add_message(role="user", content="Hello")
    ctx.add_message(role="assistant", content="Welcome to Izghawa Medical Center.")
    ctx.add_message(role="user", content=text)
    assert agent.caller_speaks_arabic(ctx) == arabic


def test_agent_tool_offers_staff_when_search_fails(kb_dir, client):
    import agent   # noqa: E402  (imports LiveKit; kept local so the other tests stay light)
    sync_index(client, FakeEmbedder(), load_chunks())
    context = SimpleNamespace(userdata=SimpleNamespace(kb=KnowledgeBase(client, BrokenEmbedder())))
    reply = asyncio.run(agent.search_knowledge_base(context, "friday hours"))
    assert reply == agent.KB_DOWN
