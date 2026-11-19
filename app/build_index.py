"""Precompute chunk embeddings: python -m app.build_index  (run at Docker build time; also run it locally after
changing data/ or the chunking)."""
import os

os.environ["USE_EMBEDDINGS"] = "0"  # don't load/compute embeddings on import; build_index() does it once, explicitly

from app.retrieval import build_index  # noqa: E402

if __name__ == "__main__":
    build_index()
