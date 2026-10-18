r"""Build or update the knowledge base index in Qdrant. Run after editing anything in knowledge_base/.

    python agent\index_kb.py

Only new or changed sections are embedded; sections that no longer exist are deleted. Safe to re-run.
"""
from rag import COLLECTION, INDEX_TIMEOUT, VoyageEmbedder, load_chunks, qdrant_client, sync_index


def main():
    chunks = load_chunks()   # stops on an empty file or a duplicate heading
    result = sync_index(qdrant_client(INDEX_TIMEOUT), VoyageEmbedder(INDEX_TIMEOUT), chunks)
    print(f"{COLLECTION}: {result['total']} sections, {result['embedded']} embedded, {result['deleted']} deleted")


if __name__ == "__main__":
    main()
