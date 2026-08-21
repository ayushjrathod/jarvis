"""Local CPU embeddings for hybrid memory search (Phase J1).

fastembed + bge-small-en-v1.5 int8 ONNX (~34MB, downloads once into
data/models/fastembed) — letta ships sqlite-vec on SQLite in production,
which is the proof this stack suffices (research 2026-07-18). The engine is
a lazy warm singleton like the voice models; embed() is for passages,
embed_query() applies the model's query-side instruction. Vectors are raw
float32 little-endian bytes — exactly what sqlite-vec's vec0 MATCH takes.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from .config import Config
from .db import Database

log = logging.getLogger("dispatcher.embeddings")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DIM = 384  # bge-small; must match the vec0 column in db.py

_engine = None
_lock = threading.Lock()
# Serializes whole backfill passes. Every caller runs embed_missing in a
# thread (asyncio.to_thread), so a threading lock — not an asyncio one — is
# the right shape: an inbox arrival landing mid-refresh used to run a second
# pass over the same missing list, colliding rowids with the first. OR IGNORE
# keeps the collision from raising, but without the lock both passes still
# pay the full embed cost for the same rows.
_backfill_lock = threading.Lock()


def enabled(cfg: Config) -> bool:
    return bool((getattr(cfg, "embeddings", None) or {}).get("enabled"))


def get_engine(cfg: Config):
    global _engine
    with _lock:
        if _engine is None:
            from fastembed import TextEmbedding
            model = (cfg.embeddings or {}).get("model", DEFAULT_MODEL)
            log.info("loading embedding model %s…", model)
            _engine = TextEmbedding(
                model_name=model,
                cache_dir=str(cfg.root / "data" / "models" / "fastembed"))
        return _engine


def _to_bytes(vecs) -> list[bytes]:
    return [np.asarray(v, dtype=np.float32).tobytes() for v in vecs]


def embed_passages(cfg: Config, texts: list[str]) -> list[bytes]:
    return _to_bytes(get_engine(cfg).embed(texts, batch_size=32))


def embed_query(cfg: Config, q: str) -> bytes:
    return _to_bytes(get_engine(cfg).query_embed(q))[0]


def embed_missing(cfg: Config, db: Database, batch: int = 128) -> dict:
    """Backfill vectors for entries/episodes that don't have one yet. Runs in
    a thread (CPU-bound); called at startup, after reindex, and on the
    refresh loop — cheap when nothing is new."""
    stats = {"entries": 0, "episodes": 0, "orphans_swept": 0, "model_reset": False}
    if not (enabled(cfg) and db.vec_ok):
        return stats
    with _backfill_lock:
        return _embed_missing_inner(cfg, db, batch, stats)


def _embed_missing_inner(cfg: Config, db: Database, batch: int, stats: dict) -> dict:
    model = (cfg.embeddings or {}).get("model", DEFAULT_MODEL)
    have = db.embedding_identity()
    if have != (model, DIM):
        if have != (None, None):
            # Same dims, different space: every KNN neighbor computed from
            # here on would be wrong with no error anywhere. Drop and rebuild
            # from source text rather than coexist.
            dropped = db.reset_embeddings()
            log.warning("embedding model changed %s -> %s; dropped %s vectors, "
                        "re-embedding from source text", have, (model, DIM), dropped)
            stats["model_reset"] = True
        db.set_embedding_identity(model, DIM)
    # clear vectors whose entry/episode was deleted since the last pass, so a
    # reindexed-away chunk can't keep matching KNN queries (L3)
    # clear vectors whose entry/episode was deleted since the last pass, so a
    # reindexed-away chunk can't keep matching KNN queries (L3)
    stats["orphans_swept"] = db.sweep_orphan_vectors()
    while True:
        rows = db.entries_missing_embeddings(batch)
        if not rows:
            break
        vecs = embed_passages(cfg, [r["compiled"] for r in rows])
        db.add_entry_embeddings([(r["id"], v) for r, v in zip(rows, vecs)])
        stats["entries"] += len(rows)
    while True:
        rows = db.episodes_missing_embeddings(batch)
        if not rows:
            break
        vecs = embed_passages(cfg, [r["text"] for r in rows])
        db.add_episode_embeddings([(r["id"], v) for r, v in zip(rows, vecs)])
        stats["episodes"] += len(rows)
    if stats["entries"] or stats["episodes"]:
        log.info("embedded %s new entries, %s new episodes",
                 stats["entries"], stats["episodes"])
    return stats
