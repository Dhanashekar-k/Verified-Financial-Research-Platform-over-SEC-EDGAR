"""Retrieval benchmark for the serving store (M03 section 3.4); runs ON the serving box.

    python -m serving.bench.retrieval_bench --model-dir /opt/edgarqa/model --out bench.json \
        --instance-type m7i.large

Measures, over the live `chunks` / `facts` views:
  * HNSW recall@10 against exact search over the same vectors, at several `ef_search`, with the
    latency of each setting. Two query sets of 200: spans of chunks (easy: each query's nearest
    neighbour is its own chunk) and two spans joined (harder); the headline is the lower recall;
  * numeric lookup latency (200 lookups of one metric, one company, one period);
  * hybrid search latency (lexical + dense + RRF, no rerank), embedding the question on the box
    (100 queries), with the stage breakdown;
  * the BGE query-instruction A/B (ADR-033). The queries are spans of the chunks themselves, so a
    chunk ranks itself high without any instruction: the numbers are REPORTED, not used to pick
    the default;
  * a filtered dense search, to show iterative scans return the rows a filter allows;
  * CPU steal over the whole run, so a throttled (flex or burstable) instance would show.
Everything printed and written comes from these measurements; nothing is estimated.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import psycopg

from app.engine.retrieval.dense import dense_search, vector_literal
from app.engine.retrieval.fuse import rrf
from app.engine.retrieval.lexical import lexical_search

K = 10
DEFAULT_EF = (40, 100, 200)


# ----------------------------------------------------------------------------- pure helpers


def recall_at_k(approx: Sequence[str], exact: Sequence[str], k: int = K) -> float:
    """Share of the exact top-k that the approximate top-k also returned."""
    truth = list(exact)[:k]
    if not truth:
        return 1.0
    return len(set(approx[:k]) & set(truth)) / len(truth)


def percentiles(values_ms: Sequence[float]) -> dict[str, float]:
    arr = np.asarray(values_ms, dtype=np.float64)
    return {
        "n": int(arr.size),
        "p50_ms": float(np.percentile(arr, 50)),
        "p95_ms": float(np.percentile(arr, 95)),
        "mean_ms": float(arr.mean()),
        "max_ms": float(arr.max()),
    }


def pseudo_query(text: str, rng: random.Random, words: int = 12) -> str | None:
    """A span of `words` consecutive words from the middle of a chunk; None if it is too short."""
    tokens = text.split()
    if len(tokens) < words * 3:
        return None
    start = rng.randint(
        len(tokens) // 4, max(len(tokens) // 4, len(tokens) - words - len(tokens) // 4)
    )
    return " ".join(tokens[start : start + words])


def lexical_question(query: str, max_terms: int = 6) -> str:
    """A broad (OR) full-text query from a pseudo-query: its longest distinct words."""
    seen: list[str] = []
    for word in re.findall(r"[A-Za-z]{4,}", query):
        if word.lower() not in {w.lower() for w in seen}:
            seen.append(word)
    longest = sorted(seen, key=len, reverse=True)[:max_terms]
    return " or ".join(longest) if longest else query


def cpu_steal_fraction(before: str, after: str) -> float:
    """Fraction of CPU time stolen by the hypervisor between two `/proc/stat` first lines."""

    def parse(line: str) -> list[int]:
        return [int(x) for x in line.split()[1:]]

    b, a = parse(before), parse(after)
    total = sum(a) - sum(b)
    steal = (a[7] - b[7]) if len(a) > 7 else 0
    return steal / total if total > 0 else 0.0


def cpu_steal_of_busy_time(before: str, after: str) -> float:
    """Stolen CPU time as a share of the non-idle time (steal / (total - idle - iowait)).

    The whole-run fraction is diluted by idle time (model loads, waits); this one is not.
    """

    def parse(line: str) -> list[int]:
        return [int(x) for x in line.split()[1:]]

    b, a = parse(before), parse(after)
    total = sum(a) - sum(b)
    idle = (a[3] - b[3]) + ((a[4] - b[4]) if len(a) > 4 else 0)
    steal = (a[7] - b[7]) if len(a) > 7 else 0
    busy = total - idle
    return steal / busy if busy > 0 else 0.0


def pick_default_ef(curve: dict[int, dict[str, float]], target: float = 0.95) -> int | None:
    """The smallest ef_search whose mean recall reaches `target`; None if none does."""
    for ef in sorted(curve):
        if curve[ef]["recall_at_10"] >= target:
            return ef
    return None


# ----------------------------------------------------------------------------- DB measurements


def _timed_ms(fn: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    result = fn()
    return result, (time.perf_counter() - started) * 1000.0


def exact_top_ids(conn: psycopg.Connection[Any], vector: Sequence[float], k: int = K) -> list[str]:
    """Exact cosine top-k over the same halfvec column (index scans off, so a sequential scan)."""
    lit = vector_literal(vector)
    with conn.transaction():
        conn.execute("SET LOCAL enable_indexscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        rows = conn.execute(
            "SELECT chunk_id FROM chunks ORDER BY embedding <=> %s::halfvec, chunk_id LIMIT %s",
            (lit, k),
        ).fetchall()
    return [r[0] for r in rows]


def sample_chunks(conn: psycopg.Connection[Any], n: int, seed: int) -> list[tuple[str, str]]:
    """`n` pseudo-random (chunk_id, text) pairs with enough words for a pseudo-query.

    One sequential scan with a deterministic hash filter (about 2% of the chunks), then a seeded
    sample of the prose chunks among them: no per-row OFFSET, no unbounded loop. Chunks under 60
    words (tables, headings) are skipped, so the queries lean toward prose.
    """
    rows = conn.execute(
        "SELECT chunk_id, text FROM chunks WHERE mod(abs(hashtext(chunk_id || %s)), 50) = 0",
        (str(seed),),
    ).fetchall()
    prose = [(cid, text) for cid, text in rows if len(text.split()) >= 60]
    if len(prose) < n:
        raise RuntimeError(f"only {len(prose)} prose chunks in the sample, need {n}")
    return random.Random(seed).sample(prose, n)


def filtered_plan_uses_hnsw(
    conn: psycopg.Connection[Any], vector: Sequence[float], cik: int, ef: int
) -> bool:
    """Whether the planner answers the filtered dense query with the HNSW index."""
    with conn.transaction():
        conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef)}")
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        plan = conn.execute(
            "EXPLAIN (FORMAT JSON) SELECT chunk_id FROM chunks WHERE cik = %s "
            "ORDER BY embedding <=> %s::halfvec LIMIT %s",
            (cik, vector_literal(vector), K),
        ).fetchone()[0]  # type: ignore[index]
    return "hnsw" in json.dumps(plan)


def run(args: argparse.Namespace) -> dict[str, Any]:
    from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, Embedder

    dsn = os.environ["EDGARQA_PG_DSN"]
    steal_before = Path("/proc/stat").read_text().splitlines()[0]
    embedder = Embedder(Path(args.model_dir), threads=2, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
    rng = random.Random(args.seed)
    result: dict[str, Any] = {"instance_type": args.instance_type, "seed": args.seed}
    with psycopg.connect(dsn, autocommit=True) as conn:
        result["server"] = {
            "version": conn.execute("SHOW server_version").fetchone()[0],  # type: ignore[index]
            "pgvector": conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()[0],  # type: ignore[index]
            "chunks": conn.execute("SELECT count(*) FROM chunks").fetchone()[0],  # type: ignore[index]
            "facts": conn.execute("SELECT count(*) FROM facts").fetchone()[0],  # type: ignore[index]
            "live_version": conn.execute("SELECT live_version FROM corpus_state").fetchone()[0],  # type: ignore[index]
        }
        # ---- chunk-derived queries
        sample = sample_chunks(conn, args.queries * 2, args.seed)
        queries: list[tuple[str, str]] = []
        for chunk_id, text in sample:
            q = pseudo_query(text, rng)
            if q:
                queries.append((chunk_id, q))
            if len(queries) == args.queries:
                break
        texts = [q for _, q in queries]
        # A harder second set: two unrelated spans joined, which lands between clusters.
        mixed = [f"{texts[i]} {texts[(i + 1) % len(texts)]}" for i in range(len(texts))]
        vec_prefix = embedder.embed_queries(texts, instruction=True)
        vec_plain = embedder.embed_queries(texts, instruction=False)
        vec_mixed = embedder.embed_queries(mixed, instruction=True)

        # ---- exact top-10 (the ground truth of the recall measurement)
        started = time.perf_counter()
        exact_prefix = [exact_top_ids(conn, v) for v in vec_prefix]
        result["exact_scan_seconds_per_query"] = (time.perf_counter() - started) / len(queries)
        exact_plain = [exact_top_ids(conn, v) for v in vec_plain]
        exact_mixed = [exact_top_ids(conn, v) for v in vec_mixed]

        # ---- HNSW recall and latency per ef_search. Recall is reported for both query sets; the
        # headline `recall_at_10` is the LOWER of the two (self-span queries are the easy case:
        # each one's nearest neighbour is its own source chunk, inside a dense region).
        query_sets = {
            "self_span": (vec_prefix, exact_prefix),
            "mixed_pair": (vec_mixed, exact_mixed),
        }
        curve: dict[int, dict[str, float]] = {}
        for ef in args.ef:
            for v in vec_prefix[:20]:  # warm-up, not measured
                dense_search(conn, v, limit=K, ef_search=ef)
            per_set: dict[str, float] = {}
            lat = []
            worst = 1.0
            for name, (vectors, truths) in query_sets.items():
                recalls = []
                for v, truth in zip(vectors, truths, strict=True):
                    hits, ms = _timed_ms(
                        lambda v=v, ef=ef: dense_search(conn, v, limit=K, ef_search=ef)
                    )
                    recalls.append(recall_at_k([h.chunk_id for h in hits], truth))
                    lat.append(ms)
                per_set[f"recall_{name}"] = statistics.fmean(recalls)
                worst = min(worst, min(recalls))
            curve[ef] = {
                "recall_at_10": min(per_set.values()),
                **per_set,
                "min_single_query_recall": worst,
                **percentiles(lat),
            }
        result["hnsw_curve"] = {str(ef): m for ef, m in curve.items()}
        result["latency_note"] = (
            "latencies are one `dense_search`/query call as the application issues it: round trip "
            "from the box's Python through the Docker port, BEGIN, two SET LOCAL, the query, COMMIT"
        )
        result["default_ef_search_for_recall_0.95"] = pick_default_ef(curve)

        # ---- numeric lookup
        facts = conn.execute(
            "SELECT cik, tag, fiscal_year, fiscal_period, uom FROM facts WHERE is_original "
            "ORDER BY fact_id LIMIT 20000"
        ).fetchall()
        picks = rng.sample(facts, min(args.lookups, len(facts)))
        for p in picks[:20]:
            conn.execute(
                "SELECT value, accession FROM facts WHERE cik=%s AND tag=%s AND fiscal_year=%s "
                "AND fiscal_period=%s AND uom=%s AND is_original",
                p,
            ).fetchall()
        lat = []
        for p in picks:
            _, ms = _timed_ms(
                lambda p=p: conn.execute(
                    "SELECT value, accession FROM facts WHERE cik=%s AND tag=%s AND fiscal_year=%s "
                    "AND fiscal_period=%s AND uom=%s AND is_original",
                    p,
                ).fetchall()
            )
            lat.append(ms)
        result["numeric_lookup"] = percentiles(lat)

        # ---- hybrid (lexical + dense + RRF), question embedded on the box
        hybrid: dict[str, Any] = {}
        for text in texts[:20]:  # warm-up (embedder session, GIN and heap caches); not measured
            lexical_search(conn, lexical_question(text), limit=50)
            dense_search(conn, embedder.embed_queries([text])[0], limit=50, ef_search=args.ef[0])
        for ef in args.ef:
            total, t_embed, t_lex, t_dense, t_fuse = [], [], [], [], []
            for text in texts[: args.hybrid]:
                started = time.perf_counter()
                qv, ms_e = _timed_ms(lambda text=text: embedder.embed_queries([text])[0])
                lex, ms_l = _timed_ms(
                    lambda text=text: lexical_search(conn, lexical_question(text), limit=50)
                )
                den, ms_d = _timed_ms(
                    lambda qv=qv, ef=ef: dense_search(conn, qv, limit=50, ef_search=ef)
                )
                lex_ids, den_ids = [h.chunk_id for h in lex], [h.chunk_id for h in den]
                _, ms_f = _timed_ms(lambda a=lex_ids, b=den_ids: rrf([a, b]))
                total.append((time.perf_counter() - started) * 1000.0)
                t_embed.append(ms_e)
                t_lex.append(ms_l)
                t_dense.append(ms_d)
                t_fuse.append(ms_f)
            hybrid[str(ef)] = {
                "total": percentiles(total),
                "embed": percentiles(t_embed),
                "lexical": percentiles(t_lex),
                "dense": percentiles(t_dense),
                "fuse": percentiles(t_fuse),
            }
        result["hybrid"] = hybrid

        # ---- BGE query-instruction A/B (reported only: the queries come from the chunks)
        def source_rank(ids: list[str], source: str) -> int | None:
            return ids.index(source) + 1 if source in ids else None

        ab: dict[str, Any] = {}
        for name, exact in (
            ("with_instruction", exact_prefix),
            ("without_instruction", exact_plain),
        ):
            ranks = [source_rank(e, src) for e, (src, _) in zip(exact, queries, strict=True)]
            hits = [r for r in ranks if r is not None]
            ab[name] = {
                "source_chunk_in_top10": len(hits) / len(ranks),
                "mrr_at_10": sum(1.0 / r for r in hits) / len(ranks),
            }
        ab["top10_overlap_between_variants"] = statistics.fmean(
            len(set(a) & set(b)) / K for a, b in zip(exact_prefix, exact_plain, strict=True)
        )
        ab["caveat"] = (
            "queries are spans of the chunks themselves; this favours no instruction (ADR-033)"
        )
        result["query_instruction_ab"] = ab

        # ---- filtered dense search: rows returned, and which plan the planner chose
        ciks = [
            r[0]
            for r in conn.execute(
                "SELECT cik FROM chunks GROUP BY cik HAVING count(*) >= 50 ORDER BY cik LIMIT 300"
            ).fetchall()
        ]
        chosen = rng.sample(ciks, min(50, len(ciks)))
        returned = []
        for cik, v in zip(chosen, vec_prefix, strict=False):
            returned.append(len(dense_search(conn, v, limit=K, ef_search=args.ef[0], cik=cik)))
        hnsw_plans = sum(
            1
            for cik, v in zip(chosen[:10], vec_prefix, strict=False)
            if filtered_plan_uses_hnsw(conn, v, cik, args.ef[0])
        )
        result["filtered_dense"] = {
            "n": len(returned),
            "min_rows_returned": min(returned),
            "limit": K,
            "plans_using_the_hnsw_index": f"{hnsw_plans} of {min(10, len(chosen))}",
            "note": "when the planner prefers the (cik, fiscal_year) btree the HNSW iterative scan "
            "is not exercised for that query",
        }
    steal_after = Path("/proc/stat").read_text().splitlines()[0]
    result["cpu_steal_fraction_whole_run"] = cpu_steal_fraction(steal_before, steal_after)
    result["cpu_steal_fraction_of_busy_time"] = cpu_steal_of_busy_time(steal_before, steal_after)
    result["queries"] = len(queries)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--instance-type", default="unknown")
    parser.add_argument(
        "--ef", type=lambda s: [int(x) for x in s.split(",")], default=list(DEFAULT_EF)
    )
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--lookups", type=int, default=200)
    parser.add_argument("--hybrid", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args(argv)
    result = run(args)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: result[k]
                for k in ("hnsw_curve", "numeric_lookup", "default_ef_search_for_recall_0.95")
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
