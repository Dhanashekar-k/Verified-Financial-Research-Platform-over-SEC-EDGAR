"""Does halfvec storage itself cost retrieval accuracy? (M03 recall question; decided in M4.)

Runs on the serving box after the index is final. For the SAME fixed queries as the bench it
compares three top-10 lists per query:
  * fp32 exact: the original float32 embeddings from the Parquet export (numpy matrix product);
  * halfvec exact: the stored `halfvec(384)` column, index scans off (what the bench uses as truth);
  * HNSW: the index, at several `ef_search`.
and reports recall of each against each, with bootstrap CIs:
  halfvec exact vs fp32 exact   -> the cost of storing half precision;
  HNSW vs halfvec exact         -> ANN approximation only (apples to apples, what the bench gates);
  HNSW vs fp32 exact            -> what a user of the index sees against full-precision truth.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from app.engine.retrieval.dense import dense_search
from pipelines.common.manifest import MANIFEST_NAME, read_manifest
from serving.bench.retrieval_bench import K, bootstrap_ci, build_queries, exact_top, recall_at_k


def load_fp32(source: str, version: int) -> tuple[list[str], np.ndarray]:
    """All exported chunk ids and their float32 embeddings, in manifest order.

    The matrix is allocated once from the manifest's row count (no concatenation copy): on an
    8 GiB box a second 1.7 GB copy would push Postgres's data out of the page cache.
    """
    fs, root = pafs.FileSystem.from_uri(source)
    base = f"{root}/corpus_version={version}"
    with fs.open_input_stream(f"{base}/{MANIFEST_NAME}") as h:
        manifest = read_manifest(h.read().decode("utf-8"))
    matrix = np.empty((manifest.chunks_rows, manifest.dim), dtype=np.float32)
    ids: list[str] = []
    filled = 0
    for f in manifest.files["chunks"]:
        with fs.open_input_file(f"{base}/{f.path}") as h:
            for batch in pq.ParquetFile(h).iter_batches(
                batch_size=50_000, columns=["chunk_id", "embedding"]
            ):
                ids += batch.column("chunk_id").to_pylist()
                flat = batch.column("embedding").flatten().to_numpy(zero_copy_only=False)
                matrix[filled : filled + batch.num_rows] = np.asarray(
                    flat, dtype=np.float32
                ).reshape(batch.num_rows, manifest.dim)
                filled += batch.num_rows
    if filled != manifest.chunks_rows:
        raise RuntimeError(f"read {filled} rows, manifest says {manifest.chunks_rows}")
    return ids, matrix


def fp32_top(
    ids: list[str], matrix: np.ndarray, queries: np.ndarray, k: int = K
) -> list[list[str]]:
    """Exact cosine top-k by dot product (rows and queries are L2-normalized)."""
    out: list[list[str]] = []
    for q in queries:
        scores = matrix @ q.astype(np.float32)
        top = np.argpartition(-scores, k)[:k]
        order = sorted(top.tolist(), key=lambda i: (-float(scores[i]), ids[i]))
        out.append([ids[i] for i in order])
    return out


def summarize(values: list[float]) -> dict[str, Any]:
    lo, hi = bootstrap_ci(values)
    return {"mean": statistics.fmean(values), "ci95": [lo, hi], "n": len(values)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, Embedder

    embedder = Embedder(Path(args.model_dir), threads=2, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
    ids, matrix = load_fp32(args.source, args.version)
    out: dict[str, Any] = {"chunks": len(ids), "efs": args.ef, "k": K}
    with psycopg.connect(os.environ["EDGARQA_PG_DSN"], autocommit=True) as conn:
        _, texts, mixed = build_queries(conn, args.seed, args.queries)
        sets = {
            "self_span": embedder.embed_queries(texts, instruction=True),
            "mixed_pair": embedder.embed_queries(mixed, instruction=True),
        }
        # fp32 truth first, then free the matrix BEFORE any database scan: the exact scans need
        # Postgres's pages in the OS cache, which the 1.7 GB matrix would otherwise evict.
        truths32 = {name: fp32_top(ids, matrix, vectors) for name, vectors in sets.items()}
        del matrix
        gc.collect()
        for name, vectors in sets.items():
            truth32 = truths32[name]
            half = [[cid for cid, _ in exact_top(conn, v)] for v in vectors]
            block: dict[str, Any] = {
                "halfvec_exact_vs_fp32_exact": summarize(
                    [recall_at_k(h, t) for h, t in zip(half, truth32, strict=True)]
                )
            }
            for ef in args.ef:
                hits = [
                    [h.chunk_id for h in dense_search(conn, v, limit=K, ef_search=ef)]
                    for v in vectors
                ]
                block[f"hnsw_ef{ef}_vs_halfvec_exact"] = summarize(
                    [recall_at_k(a, b) for a, b in zip(hits, half, strict=True)]
                )
                block[f"hnsw_ef{ef}_vs_fp32_exact"] = summarize(
                    [recall_at_k(a, b) for a, b in zip(hits, truth32, strict=True)]
                )
            out[name] = block
        out["index"] = conn.execute(
            "select indexdef from pg_indexes where indexname = 'chunks_v1_hnsw'"
        ).fetchone()[0]  # type: ignore[index]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ef", type=lambda s: [int(x) for x in s.split(",")], default=[100, 200, 800]
    )
    parser.add_argument("--queries", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261122)
    args = parser.parse_args(argv)
    result = run(args)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for name in ("self_span", "mixed_pair"):
        print(name, {k: round(v["mean"], 4) for k, v in result[name].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
