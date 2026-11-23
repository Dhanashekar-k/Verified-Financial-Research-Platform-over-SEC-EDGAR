"""Pool A/B for the embedder questions M3 handed to M4 (approved plan, step 8). CODE ONLY so far.

    uv run python -m eval.retrieval_ab --fp32-model-dir data/models/bge-small-en-v1.5-fp32

Four arms = {quantized incumbent ``Qdrant/bge-small-en-v1.5-onnx-Q``,
fp32 ``BAAI/bge-small-en-v1.5``} x {query instruction on, off}. Passages never get the
instruction. The pool is every chunk of the 84 FinanceBench documents (plus optional extra
chunk ids) in the LOCAL warehouse's current Iceberg snapshot, minus the chunks serving excludes
(``n_tokens <= 5``, ADR-035); search is exact cosine with numpy, so HNSW is out of the picture
and only the model and the query text vary. Questions are the FinanceBench **dev** split by
default (model selection on dev only, .claude/rules/eval.md).

Metrics per arm and question: evidence-page hit@5/10/20 (any-hit: at least one retrieved chunk
lies on a gold page of the gold document) and reciprocal rank @10.

Matched contrasts, each a paired bootstrap (challenger minus reference):
- model effect: fp32+instr vs quantized+instr, and fp32-noinstr vs quantized-noinstr;
- instruction effect: quantized-noinstr vs quantized+instr (a query-side flag, no re-embed).
The RE-EMBED trigger (approved plan) fires only on a model-effect contrast whose hit@10 CI
excludes 0 in fp32's favour; the instruction effect is reported separately, since acting on it
flips a flag rather than paying for a re-embed. Three contrasts, no multiplicity correction.
Caveat written into every result: within-pool ranking, not full-corpus retrieval.

The incumbent arm is ADR-033's served default (status: proposed): quantized model WITH the
instruction (``embed_queries(instruction=True)``).

Embedding is injectable (``Embedder`` protocol); tests use a tiny fake. Real models load only from
local directories (never downloaded here): the incumbent through ``pipelines.common.embedding``
(sha-pinned), the fp32 model through ``OnnxBgeEmbedder`` (onnxruntime + tokenizers, CLS pooling,
L2-normalised, as BGE specifies). ``loader_parity`` embeds a sample with ``OnnxBgeEmbedder`` on the
quantized file and compares it with the production embedder, so a loader difference can't pose as a
model difference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from pipelines.common.embedding_artifact import (
    ARTIFACT_HF_REPO,
    ARTIFACT_ONNX_FILE,
    ARTIFACT_ONNX_SHA256,
    QUERY_INSTRUCTION,
)

CAVEAT = "within-pool ranking, not full-corpus retrieval"
KS = (5, 10, 20)
MRR_K = 10
SEED = 13
FP32_MODEL = "BAAI/bge-small-en-v1.5"
QUANTIZED_MODEL = ARTIFACT_HF_REPO
INCUMBENT_ARM = "quantized+instr"
MIN_LOADER_PARITY = 0.999
DEGENERATE_MAX_TOKENS = 5  # serving drops chunks with n_tokens <= 5 (ADR-035)
MODEL_CONTRASTS: tuple[tuple[str, str], ...] = (
    ("fp32+instr", "quantized+instr"),
    ("fp32-noinstr", "quantized-noinstr"),
)
INSTRUCTION_CONTRAST: tuple[str, str] = ("quantized-noinstr", "quantized+instr")
TRIGGER_METRIC = "hit_at_10"
MULTIPLICITY_NOTE = (
    "three contrasts (two model, one instruction) tested at 95% each, no multiplicity correction"
)


class Embedder(Protocol):
    name: str

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_queries(self, questions: Sequence[str], *, instruction: bool) -> np.ndarray: ...


@dataclass(frozen=True)
class Arm:
    name: str
    model: str  # "quantized" | "fp32"
    instruction: bool


ARMS: tuple[Arm, ...] = (
    Arm("quantized+instr", "quantized", True),
    Arm("quantized-noinstr", "quantized", False),
    Arm("fp32+instr", "fp32", True),
    Arm("fp32-noinstr", "fp32", False),
)


@dataclass(frozen=True)
class PoolChunk:
    chunk_id: str
    doc_name: str
    page: int | None
    text: str


@dataclass(frozen=True)
class Query:
    id: str
    question: str
    doc_name: str
    gold_pages: frozenset[int]


# --- scoring ----------------------------------------------------------------------------------


def exact_top_k(queries: np.ndarray, passages: np.ndarray, k: int) -> np.ndarray:
    """Indices of the k highest-cosine passages (unit vectors), best first; ties by index."""
    scores = queries @ passages.T
    k = min(k, passages.shape[0])
    # lexsort by (index, -score): stable and deterministic under ties
    order = np.lexsort(
        (np.broadcast_to(np.arange(passages.shape[0]), scores.shape), -scores), axis=1
    )
    return order[:, :k]


def page_hits(retrieved: Sequence[PoolChunk], query: Query) -> list[bool]:
    return [c.doc_name == query.doc_name and c.page in query.gold_pages for c in retrieved]


def per_query_metrics(hits: Sequence[bool]) -> dict[str, float]:
    out = {f"hit_at_{k}": float(any(hits[:k])) for k in KS}
    first = next((i for i, h in enumerate(hits[:MRR_K]) if h), None)
    out[f"rr_at_{MRR_K}"] = 0.0 if first is None else 1.0 / (first + 1)
    return out


def run_arms(
    pool: Sequence[PoolChunk],
    queries: Sequence[Query],
    embedders: dict[str, Embedder],
    arms: Sequence[Arm] = ARMS,
) -> dict[str, list[dict[str, float]]]:
    """Per arm: one metrics dict per query (in query order)."""
    max_k = max(KS)
    results: dict[str, list[dict[str, float]]] = {}
    passage_cache: dict[str, np.ndarray] = {}
    for arm in arms:
        embedder = embedders[arm.model]
        if arm.model not in passage_cache:
            passage_cache[arm.model] = embedder.embed_passages([c.text for c in pool])
        q = embedder.embed_queries([x.question for x in queries], instruction=arm.instruction)
        top = exact_top_k(q, passage_cache[arm.model], max_k)
        results[arm.name] = [
            per_query_metrics(page_hits([pool[i] for i in row], query))
            for row, query in zip(top, queries, strict=True)
        ]
    return results


def _contrast(
    challenger: list[dict[str, float]],
    reference: list[dict[str, float]],
    metric_names: Sequence[str],
    *,
    n_resamples: int,
    seed: int,
) -> dict[str, Any]:
    from eval.stats import paired_bootstrap

    out: dict[str, Any] = {}
    for m in metric_names:
        res = paired_bootstrap(
            [r[m] for r in challenger],
            [r[m] for r in reference],
            n_resamples=n_resamples,
            seed=seed,
        )
        lo, hi = res.ci95
        out[m] = {
            "mean_diff": res.mean_diff,
            "ci95": [lo, hi],
            "p_two_sided": res.p_two_sided,
            "challenger_better": lo > 0,
        }
    return out


def summarize(
    per_arm: dict[str, list[dict[str, float]]],
    *,
    n_resamples: int = 10_000,
    seed: int = SEED,
) -> dict[str, Any]:
    from eval.stats import bootstrap_ci

    metric_names = list(next(iter(per_arm.values()))[0].keys())
    arms_out: dict[str, Any] = {}
    for arm, rows in per_arm.items():
        arms_out[arm] = {"n": len(rows)}
        for m in metric_names:
            values = [r[m] for r in rows]
            arms_out[arm][m] = float(np.mean(values))
            arms_out[arm][f"{m}_ci95"] = list(
                bootstrap_ci(values, n_resamples=n_resamples, seed=seed)
            )

    model_effect: dict[str, Any] = {}
    fired: list[str] = []
    for challenger, reference in MODEL_CONTRASTS:
        if challenger in per_arm and reference in per_arm:
            key = f"{challenger} vs {reference}"
            model_effect[key] = _contrast(
                per_arm[challenger],
                per_arm[reference],
                metric_names,
                n_resamples=n_resamples,
                seed=seed,
            )
            if model_effect[key][TRIGGER_METRIC]["challenger_better"]:
                fired.append(key)
    instruction_effect: dict[str, Any] = {}
    challenger, reference = INSTRUCTION_CONTRAST
    if challenger in per_arm and reference in per_arm:
        key = f"{challenger} vs {reference}"
        instruction_effect[key] = _contrast(
            per_arm[challenger],
            per_arm[reference],
            metric_names,
            n_resamples=n_resamples,
            seed=seed,
        )
    instr_better = any(v[TRIGGER_METRIC]["challenger_better"] for v in instruction_effect.values())
    return {
        "arms": arms_out,
        "model_effect": model_effect,
        "instruction_effect": instruction_effect,
        "trigger": {
            "rule": "re-embed (fp32) only if a matched model contrast's paired-bootstrap CI on "
            f"{TRIGGER_METRIC} excludes 0 in fp32's favour",
            "fired": bool(fired),
            "contrasts": fired,
            "instruction_off_better": instr_better,
            "instruction_note": "acting on the instruction effect flips a query-side flag; "
            "passages are unaffected, nothing is re-embedded",
            "multiplicity": MULTIPLICITY_NOTE,
            "caveat": CAVEAT,
        },
    }


# --- real embedders (local files only) ---------------------------------------------------------


class ProductionQuantized:
    """The served incumbent via ``pipelines.common.embedding`` (sha-pinned quantized ONNX)."""

    name = QUANTIZED_MODEL

    def __init__(self, model_dir: Path) -> None:
        from pipelines.common.embedding import get_embedder

        self._e = get_embedder(model_dir, expected_onnx_sha256=ARTIFACT_ONNX_SHA256)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._e.embed_passages(texts, batch_size=32)

    def embed_queries(self, questions: Sequence[str], *, instruction: bool) -> np.ndarray:
        return self._e.embed_queries(questions, batch_size=32, instruction=instruction)


class OnnxBgeEmbedder:
    """BGE with onnxruntime + tokenizers: CLS pooling, L2 norm, max 512 tokens."""

    def __init__(self, model_dir: Path, onnx_file: str, name: str) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.name = name
        self.onnx_path = Path(model_dir) / onnx_file
        self._tok = Tokenizer.from_file(str(Path(model_dir) / "tokenizer.json"))
        self._tok.enable_truncation(max_length=512)
        self._tok.enable_padding()
        self._sess = ort.InferenceSession(str(self.onnx_path), providers=["CPUExecutionProvider"])
        self._inputs = {i.name for i in self._sess.get_inputs()}

    def _run(self, texts: Sequence[str], batch_size: int = 32) -> np.ndarray:
        out: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            enc = self._tok.encode_batch(list(texts[start : start + batch_size]))
            feed = {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
            }
            hidden = self._sess.run(None, {k: v for k, v in feed.items() if k in self._inputs})[0]
            out.append(hidden[:, 0, :])
        vectors = np.concatenate(out).astype(np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._run(texts)

    def embed_queries(self, questions: Sequence[str], *, instruction: bool) -> np.ndarray:
        texts = [QUERY_INSTRUCTION + q for q in questions] if instruction else list(questions)
        return self._run(texts)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def loader_parity(quantized_dir: Path, production: Embedder, sample: Sequence[str]) -> float:
    """Min cosine between OnnxBgeEmbedder and the production embedder on the quantized file."""
    alt = OnnxBgeEmbedder(quantized_dir, ARTIFACT_ONNX_FILE, QUANTIZED_MODEL)
    a = alt.embed_passages(sample)
    b = production.embed_passages(sample)
    return float(np.min(np.sum(a * b, axis=1)))


# --- data -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolInfo:
    snapshot_id: int | None
    metadata_file: str
    excluded_degenerate: int


def load_pool(
    warehouse_chunks: Path, extra_chunk_ids: Sequence[str] = ()
) -> tuple[list[PoolChunk], PoolInfo]:
    """FinanceBench chunks (+ extras) from the current snapshot, minus degenerate chunks."""
    import duckdb

    from eval.gen.iceberg_files import snapshot_files

    snap = snapshot_files(warehouse_chunks)
    if not snap.files:
        raise FileNotFoundError(f"no data files in the current snapshot of {warehouse_chunks}")
    rel = duckdb.sql(
        "SELECT chunk_id, page, text, n_tokens FROM read_parquet(?) "
        "WHERE doc_source = 'financebench_pdf' OR list_contains(?, chunk_id) ORDER BY chunk_id",
        params=[snap.files, list(extra_chunk_ids)],
    )
    if rel is None:
        raise RuntimeError("duckdb returned no relation")
    rows = rel.fetchall()
    ids = [str(r[0]) for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate chunk ids in the pool")
    pool: list[PoolChunk] = []
    excluded = 0
    for chunk_id, page, text, n_tokens in rows:
        if n_tokens is not None and int(n_tokens) <= DEGENERATE_MAX_TOKENS:
            excluded += 1
            continue
        doc = str(chunk_id).removeprefix("fb:").split("#", 1)[0]
        pool.append(PoolChunk(str(chunk_id), doc, None if page is None else int(page), str(text)))
    return pool, PoolInfo(snap.snapshot_id, snap.metadata_file, excluded)


def load_queries(
    financebench_jsonl: Path, *, split: str = "dev", splits_path: Path | None = None
) -> list[Query]:
    """FinanceBench questions of one split ('dev' by default, or 'all')."""
    from eval.schemas import FinanceBenchRow
    from eval.splits import ids_for

    if not financebench_jsonl.exists():
        raise FileNotFoundError(
            f"{financebench_jsonl} not found: Shekar promotes eval/sets/drafts/financebench.jsonl "
            "to eval/sets/ first (or pass --financebench)"
        )
    splits = None
    if split != "all":
        if splits_path is None or not splits_path.exists():
            raise FileNotFoundError(
                f"split {split!r} needs {splits_path}: run `make splits` and promote "
                "eval/sets/drafts/splits.json first, or pass --split all"
            )
        splits = json.loads(splits_path.read_text())
    keep = ids_for(split, "financebench", splits)
    out = []
    for line in financebench_jsonl.read_text().splitlines():
        if line.strip():
            row = FinanceBenchRow.model_validate_json(line)
            if keep is not None and row.id not in keep:
                continue
            pages = frozenset(e.page for e in row.evidence)
            out.append(Query(row.id, row.question, row.doc_name, pages))
    return out


def build_config(
    embedders: dict[str, Embedder],
    pool: Sequence[PoolChunk],
    queries: Sequence[Query],
    **extra: Any,
) -> dict[str, Any]:
    models = {k: e.name for k, e in embedders.items()}
    if models.get("fp32") == models.get("quantized"):
        raise ValueError("fp32 arm resolves to the same model as the quantized arm")
    return {
        "models": models,
        "arms": [a.__dict__ for a in ARMS],
        "incumbent": INCUMBENT_ARM,
        "incumbent_note": "quantized + instruction follows ADR-033 (status: proposed)",
        "metrics": "hit_at_k = any retrieved chunk on a gold evidence page; rr_at_10",
        "multiplicity": MULTIPLICITY_NOTE,
        "instruction": QUERY_INSTRUCTION,
        "pool_size": len(pool),
        "pool_documents": len({c.doc_name for c in pool}),
        "queries": len(queries),
        "search": "exact cosine (numpy), no HNSW",
        "seed": SEED,
        "caveat": CAVEAT,
        **extra,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pool A/B: quantized vs fp32 bge x query instruction")
    p.add_argument(
        "--quantized-model-dir", type=Path, default=Path("data/models/bge-small-en-v1.5")
    )
    p.add_argument("--fp32-model-dir", type=Path, required=True)
    p.add_argument("--fp32-onnx-file", default="onnx/model.onnx")
    p.add_argument("--financebench", type=Path, default=Path("eval/sets/financebench.jsonl"))
    p.add_argument("--split", choices=["dev", "all"], default="dev")
    p.add_argument("--splits", type=Path, default=Path("eval/sets/splits.json"))
    p.add_argument("--warehouse-chunks", type=Path, default=Path("warehouse/curated/chunks"))
    p.add_argument("--extra-chunk-ids", type=Path, help="file with one chunk id per line")
    p.add_argument("--out-dir", type=Path, default=Path("eval/results"))
    args = p.parse_args(argv)

    try:
        queries = load_queries(args.financebench, split=args.split, splits_path=args.splits)
    except FileNotFoundError as err:
        raise SystemExit(str(err)) from err
    extra = args.extra_chunk_ids.read_text().split() if args.extra_chunk_ids else []
    pool, info = load_pool(args.warehouse_chunks, extra)
    quantized = ProductionQuantized(args.quantized_model_dir)
    parity = loader_parity(args.quantized_model_dir, quantized, [c.text for c in pool[:64]])
    print(f"loader parity min cosine: {parity:.6f} (minimum {MIN_LOADER_PARITY})")
    if parity < MIN_LOADER_PARITY:
        raise SystemExit(
            f"loader parity {parity:.6f} < {MIN_LOADER_PARITY}: OnnxBgeEmbedder does not "
            "reproduce the production embedder, so a model difference would be confounded"
        )
    fp32 = OnnxBgeEmbedder(args.fp32_model_dir, args.fp32_onnx_file, FP32_MODEL)
    embedders: dict[str, Embedder] = {"quantized": quantized, "fp32": fp32}
    config = build_config(
        embedders,
        pool,
        queries,
        split=args.split,
        iceberg_snapshot_id=info.snapshot_id,
        iceberg_metadata=info.metadata_file,
        excluded_degenerate_chunks=info.excluded_degenerate,
        model_sha256={
            "quantized": ARTIFACT_ONNX_SHA256,
            "fp32": sha256_file(fp32.onnx_path),
        },
        loader_parity_min_cosine=parity,
    )
    per_arm = run_arms(pool, queries, embedders)
    result = {
        "run_id": datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ") + "_retrieval-ab",
        "config": config,
        **summarize(per_arm),
        "per_question": {
            arm: [{"id": q.id, **m} for q, m in zip(queries, rows, strict=True)]
            for arm, rows in per_arm.items()
        },
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{result['run_id']}.json"
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
