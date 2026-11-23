"""Draft narrative eval questions from 10-K Items 1A / 7 / 7A (M04 3.4), for Shekar to curate.

Writes ``eval/sets/drafts/narrative_drafts.jsonl`` ONLY (never the gold ``narrative.jsonl``):
Shekar keeps the best 50, rewrites unnatural questions, fixes the points and sets
``verified_by``. Also writes ``eval/sets/drafts/unanswerable_template.jsonl`` (M04 3.5: five
categories x 10 empty slots that Shekar fills).

Sampling (deterministic, seed in the hash): served 10-K / 10-K/A text chunks of sections
1A, 7 and 7A, fiscal year >= 2020, at least ``MIN_TOKENS`` tokens, not tables. Strata are
(SIC division x section); each stratum gets an equal share, filled in md5(seed, chunk_id) order
with at most one chunk per company per stratum and fiscal years spread round-robin. Each sampled
chunk is sent with the next chunk of the same section, so the drafter can make about one in ten
questions need two chunks.

Drafting: Sonnet 5.5 (mapped onto the client's ``composer`` role for this run), structured
output ``NarrativeDraft``, through the LLM client in batch mode (50% off),
with cassettes in ``eval/cassettes/narrative_drafts/`` so a rerun costs $0. A cost estimate is
printed first and the run refuses to start above ``--max-usd``.

    EDGARQA_SERVING_DSN=... uv run python -m eval.gen.draft_narrative --n 120 --max-usd 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.llm.client import BatchPending, LLMClient
from app.llm.prices import cost_usd
from app.llm.types import LLMMode, Usage

SEED = 13
MIN_TOKENS = 150
MIN_FY = 2020
SECTIONS = ("1A", "7", "7A")
DRAFT_MODEL = "claude-sonnet-5-5"
OUT = Path("eval/sets/drafts/narrative_drafts.jsonl")
UNANSWERABLE_OUT = Path("eval/sets/drafts/unanswerable_template.jsonl")
CASSETTES = Path("eval/cassettes/narrative_drafts")
EST_OUTPUT_TOKENS = 350
MAX_TOKENS = 1024  # part of the cassette key; changing it re-bills every draft

SYSTEM = """You write evaluation questions for a question-answering system over SEC 10-K filings.
You are given one or two consecutive passages from one company's 10-K (Item 1A risk factors,
Item 7 MD&A, or Item 7A market risk), with the company name and fiscal year.

Write ONE question a financial analyst might ask that the passages answer in words (not a
single number lookup). The question must name the company and the fiscal year, must be
answerable from the passages alone, and must not quote the passages verbatim.
Then list 2 to 4 gold_points: short phrases (under 15 words each) that a correct answer has to
mention, each stated in the passages.
gold_chunk_ids: the chunk_id of every passage needed to cover the gold points. Use the second
passage only when the question genuinely needs it (set uses_second_chunk accordingly).
If the passages are boilerplate or too thin for a good question, still return your best
attempt and set usable to false with a one-line note."""


class NarrativeDraft(BaseModel):
    question: str
    gold_points: list[str]
    gold_chunk_ids: list[str]
    uses_second_chunk: bool
    usable: bool
    note: str


@dataclass(frozen=True)
class Candidate:
    chunk_id: str
    cik: int
    fiscal_year: int
    section: str
    sic: int | None
    company: str
    accession: str
    chunk_index: int


def sic_division(sic: int | None) -> str:
    """SEC SIC divisions (by code range)."""
    if sic is None:
        return "unknown"
    for upper, name in (
        (999, "agriculture"),
        (1499, "mining"),
        (1799, "construction"),
        (3999, "manufacturing"),
        (4999, "transport_utilities"),
        (5199, "wholesale"),
        (5999, "retail"),
        (6799, "finance"),
        (8999, "services"),
    ):
        if sic <= upper:
            return name
    return "public_admin"


def _rank(seed: int, chunk_id: str) -> str:
    return hashlib.md5(f"{seed}:{chunk_id}".encode()).hexdigest()  # noqa: S324 (ordering only)


def stratified_sample(cands: list[Candidate], n: int, seed: int = SEED) -> list[Candidate]:
    """Equal share per (SIC division x section) stratum; one chunk per company per stratum;
    fiscal years round-robin inside a stratum; leftover slots go to the largest strata."""
    strata: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for c in cands:
        strata[(sic_division(c.sic), c.section)].append(c)
    keys = sorted(strata)
    if not keys:
        return []

    def ordered(group: list[Candidate]) -> list[Candidate]:
        by_year: dict[int, list[Candidate]] = defaultdict(list)
        for c in sorted(group, key=lambda c: _rank(seed, c.chunk_id)):
            by_year[c.fiscal_year].append(c)
        out: list[Candidate] = []
        seen: set[int] = set()
        years = sorted(by_year)
        while any(by_year[y] for y in years):
            for y in years:
                while by_year[y]:
                    c = by_year[y].pop(0)
                    if c.cik not in seen:
                        seen.add(c.cik)
                        out.append(c)
                        break
        return out

    queues = {k: ordered(strata[k]) for k in keys}
    picked: list[Candidate] = []
    share = max(1, n // len(keys))
    for k in keys:
        picked += queues[k][:share]
        queues[k] = queues[k][share:]
    for k in sorted(keys, key=lambda k: -len(queues[k])):
        while len(picked) < n and queues[k]:
            picked.append(queues[k].pop(0))
    return sorted(picked[:n], key=lambda c: _rank(seed, c.chunk_id))


def user_message(c: Candidate, passages: list[tuple[str, str]]) -> str:
    blocks = "\n\n".join(f"chunk_id={cid}\n{text}" for cid, text in passages)
    return (
        f"Company: {c.company} (CIK {c.cik}); fiscal year {c.fiscal_year}; "
        f"10-K Item {c.section}.\n\n{blocks}"
    )


def estimate_usd(
    messages: list[str], *, batch: bool, output_tokens: int = EST_OUTPUT_TOKENS
) -> Decimal:
    """Rough pre-run estimate: ~4 characters per token for input, ``output_tokens`` out.
    With ``output_tokens=MAX_TOKENS`` it is the worst case the --max-usd cap is checked
    against (every draft hitting max_tokens); the typical case uses EST_OUTPUT_TOKENS."""
    total = Decimal(0)
    for m in messages:
        tokens_in = (len(SYSTEM) + len(m)) // 4 + 400  # + schema / framing overhead
        total += cost_usd(DRAFT_MODEL, tokens_in, 0, 0, output_tokens, batch=batch)
    return total


UNANSWERABLE_CATEGORIES = {
    "future_guidance_not_in_filings": "Forward guidance or forecasts filings don't contain "
    "(e.g. revenue guidance for a future fiscal year).",
    "not_disclosed": "Information companies don't disclose in 10-K/10-Q text or XBRL "
    "(e.g. headcount of one office, a named customer's contract value).",
    "out_of_scope": "Outside SEC filings: stock price, buy/sell advice, private companies.",
    "false_premise": "The question presumes something false "
    "(e.g. 'Why did Apple's revenue double in FY2023?').",
    "wrong_entity_period": "A company/period that can't exist in the corpus "
    "(e.g. a fiscal year before the company filed with the SEC).",
}


def unanswerable_template() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cat, guide in UNANSWERABLE_CATEGORIES.items():
        for i in range(1, 11):
            rows.append(
                {
                    "id": f"un-{len(rows) + 1:03d}",
                    "question": "",
                    "reason": cat,
                    "expected": "refuse",
                    "_guide": guide if i == 1 else "",
                }
            )
    return rows


DRAFTS_DIR = Path("eval/sets/drafts")


def check_out_path(out: Path, *, overwrite: bool) -> None:
    """Drafts go under eval/sets/drafts/ only, and an existing drafts file is replaced only
    with --overwrite and never once any row carries verified_by (Shekar has curated it)."""
    if not out.resolve().is_relative_to(DRAFTS_DIR.resolve()):
        raise SystemExit(f"--out must be under {DRAFTS_DIR}/ (got {out})")
    if not out.exists():
        return
    curated = any(
        json.loads(line).get("verified_by") for line in out.read_text().splitlines() if line
    )
    if curated:
        raise SystemExit(f"{out} has verified rows; refusing to overwrite curated drafts")
    if not overwrite:
        raise SystemExit(f"{out} exists; pass --overwrite to replace it")


def write_template_if_absent(path: Path) -> None:
    if path.exists():
        print(f"kept existing {path} (not overwritten)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in unanswerable_template()))
    print(f"wrote {path} (50 empty slots, 5 categories)")


def _fetch_candidates(conn: Any) -> list[Candidate]:
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.cik, c.fiscal_year, c.section, co.sic, co.name, c.accession,
               c.chunk_index
        FROM chunks c JOIN companies co ON co.cik = c.cik
        WHERE c.form IN ('10-K', '10-K/A') AND c.doc_source = 'edgar_html'
          AND c.section = ANY(%s) AND c.fiscal_year >= %s
          AND c.n_tokens >= %s AND NOT coalesce(c.is_table, false)
        """,
        [list(SECTIONS), MIN_FY, MIN_TOKENS],
    ).fetchall()
    return [Candidate(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]


def _fetch_passages(conn: Any, picked: list[Candidate]) -> dict[str, list[tuple[str, str]]]:
    """Each sampled chunk plus the next chunk of the same filing section (if served)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for c in picked:
        rows = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE accession = %s AND section = %s "
            "AND chunk_index IN (%s, %s) ORDER BY chunk_index",
            [c.accession, c.section, c.chunk_index, c.chunk_index + 1],
        ).fetchall()
        out[c.chunk_id] = [(r[0], r[1]) for r in rows]
    return out


def draft_all(
    client: LLMClient, picked: list[Candidate], passages: dict[str, list[tuple[str, str]]]
) -> tuple[dict[str, tuple[NarrativeDraft, Usage]], dict[str, str]]:
    """Two-pass batch: queue every request, run one batch, then collect from cassettes."""
    results: dict[str, tuple[NarrativeDraft, Usage]] = {}
    errors: dict[str, str] = {}
    key_to_chunk: dict[str, str] = {}
    for attempt in range(2):
        for c in picked:
            if c.chunk_id in results:
                continue
            msg = [{"role": "user", "content": user_message(c, passages[c.chunk_id])}]
            try:
                results[c.chunk_id] = client.structured(
                    "composer", SYSTEM, msg, NarrativeDraft, max_tokens=MAX_TOKENS
                )
                errors.pop(c.chunk_id, None)
            except BatchPending as pend:
                key_to_chunk[pend.key] = c.chunk_id
                if attempt == 1:  # failed inside the batch (reason recorded below), re-queued
                    errors.setdefault(c.chunk_id, "no result after batch")
                continue
            except Exception as err:  # noqa: BLE001 (recorded per draft, never aborts the run)
                errors[c.chunk_id] = f"{type(err).__name__}: {err}"
        if client.mode is LLMMode.batch and client.pending and attempt == 0:
            report = client.run_pending_batch(poll_interval_s=20)
            print(f"batch {report.batch_id}: {report.succeeded}/{report.submitted} succeeded")
            for key, why in report.failed.items():  # keyed by chunk_id, like every other error
                errors[key_to_chunk.get(key, key)] = why
        else:
            break
    return results, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("EDGARQA_SERVING_DSN"))
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--seed", type=int, default=SEED)
    # no stub mode: placeholder drafts must never land in the drafts file labelled as Sonnet output
    ap.add_argument("--mode", default="batch", choices=["batch", "record", "replay"])
    ap.add_argument("--max-usd", type=Decimal, default=Decimal("2"))
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument(
        "--overwrite", action="store_true", help="replace an existing drafts file (never curated)"
    )
    args = ap.parse_args(argv)
    check_out_path(args.out, overwrite=args.overwrite)

    write_template_if_absent(UNANSWERABLE_OUT)

    if not args.dsn:
        ap.error("set EDGARQA_SERVING_DSN or pass --dsn")
    import psycopg

    # read-only at the session level (conn.read_only is a no-op under autocommit)
    with psycopg.connect(
        args.dsn, autocommit=True, options="-c default_transaction_read_only=on"
    ) as conn:
        cands = _fetch_candidates(conn)
        picked = stratified_sample(cands, args.n, args.seed)
        passages = _fetch_passages(conn, picked)
    strata: dict[tuple[str, str], int] = defaultdict(int)
    for c in picked:
        strata[(sic_division(c.sic), c.section)] += 1
    print(f"{len(cands)} candidate chunks; sampled {len(picked)} over {len(strata)} strata:")
    for k in sorted(strata):
        print(f"  {k[0]:<20} Item {k[1]:<3} {strata[k]}")

    msgs = [user_message(c, passages[c.chunk_id]) for c in picked]
    est = estimate_usd(msgs, batch=args.mode == "batch")
    worst = estimate_usd(msgs, batch=args.mode == "batch", output_tokens=MAX_TOKENS)
    print(
        f"estimated cost ({args.mode}): ${est:.4f} typical, ${worst:.4f} worst case "
        f"(every draft at max_tokens); cap ${args.max_usd} (cassette hits are free)"
    )
    if args.estimate_only:
        return 0
    if worst > args.max_usd:
        raise SystemExit(f"worst case ${worst:.4f} exceeds --max-usd {args.max_usd}; not running")

    client = LLMClient(
        mode=LLMMode(args.mode), cassette_dir=CASSETTES, registry={"composer": DRAFT_MODEL}
    )
    results, errors = draft_all(client, picked, passages)

    spent = sum((u.cost_usd for _, u in results.values()), Decimal(0)) + client.failed_usage_usd
    rows: list[dict[str, Any]] = []
    for i, c in enumerate(picked, start=1):
        if c.chunk_id not in results:
            continue
        d, usage = results[c.chunk_id]
        given = {cid for cid, _ in passages[c.chunk_id]}
        rows.append(
            {
                "draft_id": f"nar-d{i:03d}",
                "question": d.question,
                "cik": c.cik,
                "company": c.company,
                "fiscal_year": c.fiscal_year,
                "section": c.section,
                "sector": sic_division(c.sic),
                "gold_chunk_ids": d.gold_chunk_ids,
                "gold_points": d.gold_points,
                "uses_second_chunk": d.uses_second_chunk,
                "usable": d.usable,
                "note": d.note,
                "invalid_chunk_ids": sorted(set(d.gold_chunk_ids) - given),
                "drafted_by": DRAFT_MODEL,
                "verified_by": None,
                "verified_at": None,
                "source_chunks": [{"chunk_id": cid, "text": t} for cid, t in passages[c.chunk_id]],
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    summary = {
        "drafts": len(rows),
        "errors": errors,
        "usable": sum(r["usable"] for r in rows),
        "two_chunk": sum(len(r["gold_chunk_ids"]) > 1 for r in rows),
        "invalid_chunk_id_rows": sum(bool(r["invalid_chunk_ids"]) for r in rows),
        "cost_usd": str(spent),
        "failed_usage_usd": str(client.failed_usage_usd),
        "estimate_usd": str(est),
        "mode": args.mode,
        "model": DRAFT_MODEL,
        "seed": args.seed,
        "candidates": len(cands),
        "strata": {f"{k[0]}|{k[1]}": v for k, v in sorted(strata.items())},
    }
    summary_path = args.out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "strata"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
