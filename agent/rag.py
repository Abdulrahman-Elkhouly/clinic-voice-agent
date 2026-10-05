from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

KB_DIR = Path(__file__).resolve().parent.parent / "knowledge_base"


def load_chunks() -> list[dict]:
    """Split every .md file into one chunk per '##' section."""
    chunks = []
    for path in sorted(KB_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        doc_title = text.splitlines()[0].lstrip("# ").strip()
        parts = text.split("\n## ")
        sections = parts[1:] or [text]          # files without ## become one chunk
        for section in sections:
            # Prefix the doc title so every chunk says what it's about on its own
            chunks.append({"source": path.name, "text": f"{doc_title}\n## {section.strip()}"})
    return chunks


class KnowledgeBase:
    def __init__(self):
        # Small local embedding model (~130 MB, downloaded on first run)
        self.model = TextEmbedding("BAAI/bge-small-en-v1.5")
        self.chunks = load_chunks()
        vectors = np.array(list(self.model.embed([c["text"] for c in self.chunks])))
        self.vectors = self._normalize(vectors)
        print(f"[RAG] indexed {len(self.chunks)} chunks, vector size {self.vectors.shape[1]}")

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        # Unit-length vectors make the dot product equal to cosine similarity
        return v / np.linalg.norm(v, axis=-1, keepdims=True)

    def search(self, query: str, k: int = 3) -> list[tuple[float, dict]]:
        q = self._normalize(np.array(list(self.model.query_embed(query)))[0])
        scores = self.vectors @ q                      # one similarity score per chunk
        top = np.argsort(scores)[::-1][:k]             # indices of the k highest scores
        return [(float(scores[i]), self.chunks[i]) for i in top]


if __name__ == "__main__":
    kb = KnowledgeBase()
    while True:
        query = input("\nQuestion (empty to quit): ").strip()
        if not query:
            break
        for score, chunk in kb.search(query):
            first_line = chunk["text"].splitlines()[1]
            print(f"  {score:.3f}  {chunk['source']:28} {first_line}")
