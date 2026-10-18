"""The clinic knowledge base: markdown sections, embedded with Voyage, stored and searched in Qdrant.

Indexing is done only by index_kb.py. The agent's KnowledgeBase only checks the index and searches it, so call
processes (one per call) never index at the same moment.
"""
import hashlib
import os
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "knowledge_base"
load_dotenv(ROOT / ".env")

EMBED_MODEL = "voyage-4"       # chosen 2026-10-18 (see clinic_plan.md, Stage 1b)
EMBED_DIM = 1024
COLLECTION = f"kb_{EMBED_MODEL}_{EMBED_DIM}"   # one collection per model: vectors from different models never mix
VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
SEARCH_TIMEOUT = 2.0           # seconds, per call to Voyage or Qdrant during a call
INDEX_TIMEOUT = 60.0
BATCH = 32                     # texts per Voyage request when indexing


class KnowledgeBaseUnavailable(Exception):
    """Voyage or Qdrant failed or was too slow. The caller hears that the information can't be looked up."""


def load_chunks() -> list[dict]:
    """Split every .md file into one chunk per '##' section."""
    chunks = []
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError(f"{path.name} is empty")
        doc_title = text.splitlines()[0].lstrip("# ").strip()
        parts = text.split("\n## ")
        sections = parts[1:] or [text]          # files without ## become one chunk
        for section in sections:
            heading = section.splitlines()[0].strip() if parts[1:] else doc_title
            # Prefix the doc title so every chunk says what it's about on its own
            chunks.append({
                "source": path.name,
                "id": f"{path.name} > {heading}",   # what the test set's Source lines refer to
                "text": f"{doc_title}\n## {section.strip()}",
            })
    ids = [c["id"] for c in chunks]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:   # ids come from "file > heading", so a repeated heading would overwrite a section
        raise ValueError(f"Duplicate headings: {duplicates}")
    return chunks


def point_id(chunk_id: str) -> str:
    """A fixed Qdrant id per section, so re-indexing overwrites instead of duplicating."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class VoyageEmbedder:
    def __init__(self, timeout: float):
        self.client = httpx.Client(headers={"Authorization": f"Bearer {os.environ['VOYAGE_API_KEY']}"},
                                   timeout=timeout)

    def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        """input_type is "document" when indexing and "query" for a caller's question."""
        vectors = []
        for i in range(0, len(texts), BATCH):
            r = self.client.post(VOYAGE_URL, json={"model": EMBED_MODEL, "input": texts[i:i + BATCH],
                                                   "input_type": input_type, "output_dimension": EMBED_DIM})
            r.raise_for_status()
            vectors += [e["embedding"] for e in sorted(r.json()["data"], key=lambda e: e["index"])]
        return vectors


def qdrant_client(timeout: float) -> QdrantClient:
    return QdrantClient(url=os.environ["QDRANT_URL"].strip(), api_key=os.environ["QDRANT_API_KEY"].strip(),
                        timeout=int(max(1, timeout)))


def stored_hashes(client: QdrantClient) -> dict[str, str]:
    """Point id -> text hash for every section in the collection."""
    hashes, offset = {}, None
    while True:
        points, offset = client.scroll(COLLECTION, limit=256, offset=offset, with_payload=["hash"],
                                       with_vectors=False)
        hashes.update({str(p.id): p.payload["hash"] for p in points})
        if offset is None:
            return hashes


def sync_index(client: QdrantClient, embedder, chunks: list[dict]) -> dict:
    """Make the collection match the chunks: embed only new or changed sections, delete removed ones.
    Safe to re-run after a crash (fixed ids)."""
    if not client.collection_exists(COLLECTION):
        client.create_collection(COLLECTION, vectors_config=models.VectorParams(size=EMBED_DIM,
                                                                                distance=models.Distance.COSINE))
    stored = stored_hashes(client)
    changed = [c for c in chunks if stored.get(point_id(c["id"])) != text_hash(c["text"])]
    if changed:
        vectors = embedder.embed([c["text"] for c in changed], "document")
        client.upsert(COLLECTION, points=[
            models.PointStruct(id=point_id(c["id"]), vector=v,
                               payload={"chunk_id": c["id"], "source": c["source"], "text": c["text"],
                                        "hash": text_hash(c["text"])})
            for c, v in zip(changed, vectors)])
    removed = sorted(set(stored) - {point_id(c["id"]) for c in chunks})
    if removed:
        client.delete(COLLECTION, points_selector=models.PointIdsList(points=removed))
    return {"embedded": len(changed), "deleted": len(removed), "total": len(chunks)}


def index_problems(client: QdrantClient, chunks: list[dict]) -> int:
    """How many sections are new, changed, or removed since the last index_kb.py run."""
    stored = stored_hashes(client)
    local = {point_id(c["id"]): text_hash(c["text"]) for c in chunks}
    return sum(stored.get(pid) != h for pid, h in local.items()) + len(set(stored) - set(local))


class KnowledgeBase:
    def __init__(self, client: QdrantClient | None = None, embedder=None):
        self.client = client or qdrant_client(SEARCH_TIMEOUT)
        self.embedder = embedder or VoyageEmbedder(SEARCH_TIMEOUT)
        try:
            exists = self.client.collection_exists(COLLECTION)
        except (ResponseHandlingException, UnexpectedResponse, httpx.HTTPError) as e:
            raise RuntimeError(f"Can't reach Qdrant ({e}). If the free cluster was suspended, reactivate it in "
                               "the Qdrant Cloud dashboard.") from e
        if not exists:
            raise RuntimeError(f"Qdrant has no '{COLLECTION}' collection. Run: python agent/index_kb.py")
        self.chunks = load_chunks()   # the local sections, for the startup check and the eval scripts
        stale = index_problems(self.client, self.chunks)
        if stale:
            print(f"[RAG] WARNING: {stale} knowledge base section(s) changed since indexing. "
                  "Run: python agent/index_kb.py")
        print(f"[RAG] using Qdrant collection {COLLECTION}")

    def search(self, query: str, k: int = 3) -> list[tuple[float, dict]]:
        """Top k sections as (score, {"id", "source", "text"}). Raises KnowledgeBaseUnavailable on any failure."""
        try:
            vector = self.embedder.embed([query], "query")[0]
            points = self.client.query_points(COLLECTION, query=vector, limit=k, with_payload=True).points
        except (ResponseHandlingException, UnexpectedResponse, httpx.HTTPError) as e:
            raise KnowledgeBaseUnavailable(str(e)) from e
        return [(p.score, {"id": p.payload["chunk_id"], "source": p.payload["source"], "text": p.payload["text"]})
                for p in points]


if __name__ == "__main__":
    kb = KnowledgeBase()
    while True:
        query = input("\nQuestion (empty to quit): ").strip()
        if not query:
            break
        for score, chunk in kb.search(query):
            first_line = chunk["text"].splitlines()[1]
            print(f"  {score:.3f}  {chunk['source']:28} {first_line}")
