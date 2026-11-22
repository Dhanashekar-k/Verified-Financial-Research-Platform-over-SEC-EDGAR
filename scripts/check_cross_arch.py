"""x86-vs-ARM embedding check for M3 (ADR-036): do the Glue vectors match a local re-embedding?

    python -m scripts.check_cross_arch --glue-arch x86_64 --glue-run jr_... \
        [--table corpus_smoke.chunk_embeddings] [--n 200]

Reads the Glue smoke vectors and the chunk texts with read-only Athena SELECTs, embeds the same
texts locally with the pinned ONNX model (this machine's architecture), and compares row by row.
`--glue-arch` is what the Glue driver logged (`driver platform ...`); it is never assumed.
Exit 0 when every chunk agrees to cosine >= 0.9999; exit 1 otherwise: STOP, no full run.
The result is merged into `docs/milestones/M03-embed-metrics.json` under `cross_arch_agreement`.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from pipelines.common.embedding import ARTIFACT_ONNX_SHA256, Embedder
from pipelines.common.vector_agreement import (
    MIN_COSINE,
    parse_athena_array,
    stratified_pick,
    summarize,
)

DEFAULT_MODEL_DIR = Path("data/models/bge-small-en-v1.5")
METRICS = Path("docs/milestones/M03-embed-metrics.json")
CANDIDATES = 800  # pseudo-random chunks fetched; the stratified pick takes `n` of them


def athena_rows(sql: str, workgroup: str = "edgarqa", database: str = "curated") -> list[list[str]]:
    """Run a read-only SELECT through Athena and return the rows (header dropped) as strings."""
    import boto3

    athena = boto3.client("athena")
    qid = athena.start_query_execution(
        QueryString=sql, WorkGroup=workgroup, QueryExecutionContext={"Database": database}
    )["QueryExecutionId"]
    while True:
        state = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]["Status"]
        if state["State"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(2)
    if state["State"] != "SUCCEEDED":
        raise RuntimeError(f"Athena query {qid} {state['State']}: {state.get('StateChangeReason')}")
    rows: list[list[str]] = []
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        rows += [[c.get("VarCharValue", "") for c in r["Data"]] for r in page["ResultSet"]["Rows"]]
    return rows[1:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--table", default="corpus_smoke.chunk_embeddings")
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--glue-arch", required=True, help="architecture the Glue driver logged")
    parser.add_argument("--glue-run", required=True, help="Glue job run id of the smoke run")
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--out", type=Path, default=METRICS)
    args = parser.parse_args(argv)

    rows = athena_rows(
        "SELECT e.chunk_id, CAST(c.n_tokens AS varchar), c.text, '[' || array_join(e.embedding, ',') || ']' "
        f"FROM {args.table} e JOIN curated.chunks c ON c.chunk_id = e.chunk_id "
        f"ORDER BY xxhash64(to_utf8(e.chunk_id)) LIMIT {CANDIDATES}"
    )
    picked = stratified_pick([int(r[1]) for r in rows], args.n)
    chosen = [rows[i] for i in picked]
    texts = [r[2] for r in chosen]
    glue = np.stack([parse_athena_array(r[3]) for r in chosen])

    one = Embedder(args.model_dir, threads=1, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
    four = Embedder(args.model_dir, threads=4, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)
    local = one.embed_passages(texts)
    local_threads = four.embed_passages(texts)

    vs_glue = summarize(local, glue)
    control = summarize(local, local_threads)
    import fastembed
    import onnxruntime

    record: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "table": args.table,
        "glue_run": args.glue_run,
        "glue_arch": args.glue_arch,
        "local_arch": platform.machine(),
        "local_onnxruntime": onnxruntime.__version__,
        "local_fastembed": fastembed.__version__,
        "onnx_sha256": ARTIFACT_ONNX_SHA256,
        "gate_min_cosine": MIN_COSINE,
        "chunk_ids": [r[0] for r in chosen],
        "local_vs_glue": vs_glue.as_dict(),
        "control_local_threads_1_vs_4": control.as_dict(),
    }
    existing: dict[str, Any] = {}
    if args.out.is_file():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
    existing["cross_arch_agreement"] = record
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(existing, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    print(
        f"{vs_glue.n} chunks, {args.glue_arch} (Glue) vs {platform.machine()} (local): "
        f"min cosine {vs_glue.min_cosine:.6f}, mean {vs_glue.mean_cosine:.6f}, "
        f"below {MIN_COSINE}: {vs_glue.n_below_gate}; thread control min {control.min_cosine:.6f}"
    )
    if not vs_glue.passed:
        print("STOP: the vectors differ across architectures; do not start the full run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
