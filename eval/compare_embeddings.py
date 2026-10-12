r"""Compare embedding models on our own test set: accuracy (English + Arabic) and CPU latency.

    python eval\compare_embeddings.py                 # all models
    python eval\compare_embeddings.py e5-small bge-m3 # only some (by short name)
    python eval\compare_embeddings.py gemini-3072 gemini-768   # no huggingface.co needed

Retrieval only (no LLM). Uses the real chunker from rag.py and the real test set.
"""
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "eval"))

from rag import load_chunks          # noqa: E402
from eval_rag import check_sources, load_test_set, score_retrieval   # noqa: E402

K = 3

# short name -> (library, model id, query prefix, passage prefix)
# e5 models were trained with these prefixes and score worse without them.
# "fastembed" models download from Qdrant's storage; "st" (sentence-transformers) ones need huggingface.co.
MODELS = {
    "e5-small": ("st", "intfloat/multilingual-e5-small", "query: ", "passage: "),
    "e5-base": ("st", "intfloat/multilingual-e5-base", "query: ", "passage: "),
    "e5-large": ("st", "intfloat/multilingual-e5-large", "query: ", "passage: "),
    "bge-m3": ("st", "BAAI/bge-m3", "", ""),
    # Cloud API: uses a task type instead of prefixes; the number is the vector size requested
    "gemini-3072": ("gemini", "gemini-embedding-001", 3072, None),
    "gemini-768": ("gemini", "gemini-embedding-001", 768, None),
}

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _unit(v) -> np.ndarray:
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def load_encoder(name: str):
    """Returns encode(texts, is_query) -> unit-length vectors, whatever the library."""
    library, model_id, a, b = MODELS[name]

    if library == "gemini":
        import httpx
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        client = httpx.Client(headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=30)
        client.get(f"{GEMINI_URL}/{model_id}")   # open the connection now, so "load" includes the TLS setup

        def encode(texts, is_query):
            task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
            body = {"requests": [{"model": f"models/{model_id}", "content": {"parts": [{"text": t}]},
                                  "taskType": task, "outputDimensionality": a} for t in texts]}
            r = client.post(f"{GEMINI_URL}/{model_id}:batchEmbedContents", json=body)
            r.raise_for_status()
            # Vectors smaller than 3072 come back unnormalized, so always normalize
            return _unit([e["values"] for e in r.json()["embeddings"]])
        return encode

    q_prefix, p_prefix = a, b
    if library == "fastembed":
        from fastembed import TextEmbedding
        model = TextEmbedding(model_id)
        return lambda texts, is_query: _unit(list(model.embed(
            [(q_prefix if is_query else p_prefix) + t for t in texts])))

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(model_id, device="cpu")
    return lambda texts, is_query: model.encode(
        [(q_prefix if is_query else p_prefix) + t for t in texts], normalize_embeddings=True)


def evaluate(name: str, chunks: list[dict], tests: list[dict]) -> dict:
    t0 = time.perf_counter()
    encode = load_encoder(name)
    load_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    doc_vecs = encode([c["text"] for c in chunks], is_query=False)
    index_s = time.perf_counter() - t0

    encode(["warm up"], is_query=True)   # first call is slow; don't count it

    hit1, hitk, scored = {}, {}, {}
    top_scores = {"in_kb": [], "not_in_kb": []}
    latencies = []
    for t in tests:
        if t["kind"] == "safety":
            continue
        t0 = time.perf_counter()
        q = encode([t["question"]], is_query=True)[0]
        latencies.append((time.perf_counter() - t0) * 1000)

        scores = doc_vecs @ q
        top = np.argsort(scores)[::-1][:K]
        top_ids = [chunks[i]["id"] for i in top]

        if t["sources"]:
            kind = t["kind"]
            h1, hk = score_retrieval(t["sources"], top_ids)
            scored[kind] = scored.get(kind, 0) + 1
            hit1[kind] = hit1.get(kind, 0) + h1
            hitk[kind] = hitk.get(kind, 0) + hk
            if not h1:
                print(f"   miss@1: {t['question']}\n      wanted {t['sources']}\n      got    {top_ids}")
            top_scores["in_kb"].append(float(scores[top[0]]))
        elif t["kind"] == "not_in_kb" or (t["kind"] == "arabic" and "no search" not in t["check"]):
            # Arabic questions with no source are not-in-KB, except the emergency (no search at all)
            top_scores["not_in_kb"].append(float(scores[top[0]]))

    return {
        "name": name, "dim": doc_vecs.shape[1], "load_s": load_s, "index_s": index_s,
        "ms_median": statistics.median(latencies), "ms_max": max(latencies),
        "hit1": hit1, "hitk": hitk, "scored": scored,
        # How far apart "has an answer" and "has no answer" are; bigger = easier to tell apart
        "gap": statistics.mean(top_scores["in_kb"]) - statistics.mean(top_scores["not_in_kb"]),
        "min_in_kb": min(top_scores["in_kb"]), "max_not_in_kb": max(top_scores["not_in_kb"]),
    }


def main():
    names = sys.argv[1:] or list(MODELS)
    chunks = load_chunks()
    tests = load_test_set()
    check_sources(tests, chunks)
    print(f"{len(chunks)} chunks, {len(tests)} test questions\n")

    rows = []
    for name in names:
        print(f"--- {name} ({MODELS[name][1]}, {MODELS[name][0]})")
        r = evaluate(name, chunks, tests)
        rows.append(r)
        for kind in r["scored"]:
            print(f"   {kind:10} hit@1 {r['hit1'][kind]}/{r['scored'][kind]}   hit@{K} {r['hitk'][kind]}/{r['scored'][kind]}")
        print(f"   query {r['ms_median']:.0f} ms median ({r['ms_max']:.0f} max), "
              f"index {r['index_s']:.1f} s, load {r['load_s']:.1f} s, dim {r['dim']}")
        print(f"   score gap {r['gap']:.3f} (lowest answerable {r['min_in_kb']:.3f}, "
              f"highest not-in-KB {r['max_not_in_kb']:.3f})\n")

    print(f"{'model':14}{'EN @1':>8}{'AR @1':>8}{'AR @3':>8}{'ms/query':>10}{'gap':>8}{'dim':>6}")
    for r in rows:
        en = f"{r['hit1'].get('answerable', 0)}/{r['scored'].get('answerable', 0)}"
        ar1 = f"{r['hit1'].get('arabic', 0)}/{r['scored'].get('arabic', 0)}"
        ar3 = f"{r['hitk'].get('arabic', 0)}/{r['scored'].get('arabic', 0)}"
        print(f"{r['name']:14}{en:>8}{ar1:>8}{ar3:>8}{r['ms_median']:>10.0f}{r['gap']:>8.3f}{r['dim']:>6}")


if __name__ == "__main__":
    main()
