"""Semantic memory for finance facts, decisions, and corrections (BUILD_PLAN M4).

A small, separate memory module with a clean interface: ``write_memory`` and
``search_memory``. It stores notes (user corrections, obligation decisions,
context the model should recall later) and retrieves the most relevant ones for
a query.

Embedding: this uses a deterministic, stdlib-only bag-of-words term-frequency
vector with cosine similarity. It is intentionally dependency-free (no numpy, no
model, no API). The embedding function is the only thing that would change to
swap in a real sentence-embedding model later; the interface and the retrieval
policy stay the same.

Context-control policy (the part interview Q4 asks about): retrieval applies
three limits in order and reports what each dropped, so the amount of memory
entering context is explicit and defensible:

1. a similarity threshold (``min_score``) - drop weakly-related records,
2. a top-k cap (``k``) - bound how many records can enter,
3. a token budget (``max_tokens``) - bound the total context cost.

Failure mode guarded against: context bloat / irrelevant records crowding out
the query. The threshold removes noise, top-k bounds count, and the token budget
bounds cost even when many records score highly.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any

from .schema import ensure_app_schema


DEFAULT_K = 5
DEFAULT_MIN_SCORE = 0.05
DEFAULT_MAX_TOKENS = 1500

_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
        "was", "were", "be", "this", "that", "it", "with", "as", "at", "by", "from",
        "but", "not", "no", "i", "you", "we", "they", "he", "she", "my", "our",
    }
)


def write_memory(
    conn: sqlite3.Connection,
    *,
    text: str,
    metadata: dict[str, Any] | None = None,
    kind: str = "note",
    source: str | None = None,
    memory_id: str | None = None,
) -> dict[str, Any]:
    """Store a memory record. Idempotent by (kind, source, text) unless an id is given."""

    ensure_app_schema(conn)
    if not text or not text.strip():
        raise ValueError("memory text must be non-empty")
    tf = _term_frequencies(text)
    token_count = sum(tf.values())
    record_id = memory_id or _derive_id(kind, source, text)
    now = _now()
    before = conn.execute("SELECT 1 FROM memory_records WHERE id = ?", (record_id,)).fetchone()
    conn.execute(
        """
        INSERT INTO memory_records (
            id, kind, text, metadata_json, term_frequency_json, token_count,
            source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            kind = excluded.kind,
            text = excluded.text,
            metadata_json = excluded.metadata_json,
            term_frequency_json = excluded.term_frequency_json,
            token_count = excluded.token_count,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        (
            record_id, kind, text,
            json.dumps(metadata, sort_keys=True) if metadata is not None else None,
            json.dumps(dict(tf), sort_keys=True), token_count, source, now, now,
        ),
    )
    return {"id": record_id, "kind": kind, "token_count": token_count, "created": before is None}


def search_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    k: int = DEFAULT_K,
    min_score: float = DEFAULT_MIN_SCORE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    kind: str | None = None,
) -> dict[str, Any]:
    """Return the most relevant memories for a query, under the context policy.

    Records are scored by cosine similarity, then filtered by ``min_score``,
    capped at ``k``, and finally bounded by a ``max_tokens`` budget. The result
    reports how many records each limit dropped.
    """

    ensure_app_schema(conn)
    query_tf = _term_frequencies(query)
    query_norm = _norm(query_tf)

    where = ""
    params: list[Any] = []
    if kind is not None:
        where = "WHERE kind = ?"
        params.append(kind)
    rows = conn.execute(
        f"SELECT id, kind, text, metadata_json, term_frequency_json, token_count, source FROM memory_records {where}",
        params,
    ).fetchall()

    scored: list[dict[str, Any]] = []
    for row in rows:
        tf = Counter(json.loads(row["term_frequency_json"]))
        score = _cosine(query_tf, query_norm, tf)
        scored.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "text": row["text"],
                "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
                "source": row["source"],
                "token_count": row["token_count"],
                "score": round(score, 4),
            }
        )

    considered = len(scored)
    # Deterministic order: score desc, then id for stable ties.
    scored.sort(key=lambda r: (-r["score"], r["id"]))

    above_threshold = [r for r in scored if r["score"] >= min_score]
    dropped_by_threshold = considered - len(above_threshold)

    within_k = above_threshold[:k]
    dropped_by_k = len(above_threshold) - len(within_k)

    selected: list[dict[str, Any]] = []
    used_tokens = 0
    dropped_by_budget = 0
    for record in within_k:
        if used_tokens + record["token_count"] > max_tokens and selected:
            dropped_by_budget += 1
            continue
        selected.append(record)
        used_tokens += record["token_count"]

    return {
        "query": query,
        "policy": {"k": k, "min_score": min_score, "max_tokens": max_tokens},
        "considered": considered,
        "returned": selected,
        "returned_count": len(selected),
        "used_tokens": used_tokens,
        "dropped_by_threshold": dropped_by_threshold,
        "dropped_by_k": dropped_by_k,
        "dropped_by_budget": dropped_by_budget,
    }


def list_memories(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_app_schema(conn)
    where = ""
    params: list[Any] = []
    if kind is not None:
        where = "WHERE kind = ?"
        params.append(kind)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, kind, text, metadata_json, token_count, source, created_at, updated_at
        FROM memory_records {where}
        ORDER BY updated_at DESC, id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "text": r["text"],
            "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else None,
            "source": r["source"],
            "token_count": r["token_count"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def delete_memory(conn: sqlite3.Connection, *, memory_id: str) -> dict[str, Any]:
    ensure_app_schema(conn)
    cur = conn.execute("DELETE FROM memory_records WHERE id = ?", (memory_id,))
    return {"id": memory_id, "deleted": cur.rowcount > 0}


# --- embedding (deterministic, stdlib bag-of-words) ------------------------


def _term_frequencies(text: str) -> Counter:
    tokens = [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 2 and t not in _STOPWORDS]
    return Counter(tokens)


def _norm(tf: Counter) -> float:
    return math.sqrt(sum(v * v for v in tf.values()))


def _cosine(a: Counter, a_norm: float, b: Counter) -> float:
    if a_norm == 0:
        return 0.0
    b_norm = _norm(b)
    if b_norm == 0:
        return 0.0
    # Iterate the smaller vector for the dot product.
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    dot = sum(weight * large.get(token, 0) for token, weight in small.items())
    return dot / (a_norm * b_norm)


def _derive_id(kind: str, source: str | None, text: str) -> str:
    raw = json.dumps({"kind": kind, "source": source, "text": text}, sort_keys=True)
    return f"mem_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _now() -> str:
    return datetime.now().astimezone().isoformat()
