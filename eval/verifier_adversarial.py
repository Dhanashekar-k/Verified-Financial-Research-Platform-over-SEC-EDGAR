"""The adversarial claims: does the deterministic verifier drop what it should? (ADR-049)

    uv run python -m eval.verifier_adversarial --list-bases           # claims a row can start from
    uv run python -m eval.verifier_adversarial --show num-0003 c1     # one claim with evidence
    uv run python -m eval.verifier_adversarial --check                # validate rows (no verifier)
    uv run python -m eval.verifier_adversarial                        # score (needs the cores)

``eval/sets/adversarial_claims.jsonl`` is written by hand (05 section 6): each row changes one
real claim of the Gate 0 dev trace. No LLM and no judge: $0. Claim level only (not the answer's
sufficiency). An expected-drop row is **caught** only when the unchanged claim is kept and the
changed one dropped; a drop of a claim that was already dropped proves nothing (**vacuous**).
A miss is a result, not a failure: the command exits 0 whatever the score.

A changed provenance may cite only passages and facts the trace saved. Anything else would be
dropped as "not found" offline, whatever the row meant to test (to test an absent quote, change
the quote and keep the passage).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.engine.answer import Claim, ExprProvenance, Provenance
from app.engine.verify.models import VerifierResult, VerifyConfig
from app.engine.verify.pipeline import (
    DEFAULT_VERIFIER,
    Verifier,
    VerifierNotImplemented,
    require_implemented,
    verify_claim,
)
from eval.stats import wilson_ci
from eval.verifier_inputs import (
    TRACE_PATH,
    Reference,
    context_for,
    evidence_for,
    load_reference,
    load_trace,
    row_by_id,
)
from eval.verifier_trace import Trace

SET_PATH = Path("eval/sets/adversarial_claims.jsonl")
OUT_PATH = Path("eval/results/verifier/adversarial.json")
GATE0_PATH = Path("eval/results/verifier/gate0_dev_pre.json")

DROP_CATEGORIES = (
    "wrong_year",
    "wrong_company",
    "scale_million_billion",
    "sign_flip",
    "absent_quote",
    "changed_number",
    "formula_not_built",
)
# A bad claim the deterministic verifier is expected to miss: the documented boundary.
BOUNDARY_CATEGORIES = ("added_cause", "invalid_inference")
CONTROL_CATEGORY = "correct"
Category = Literal[
    "wrong_year",
    "wrong_company",
    "scale_million_billion",
    "sign_flip",
    "absent_quote",
    "changed_number",
    "formula_not_built",
    "added_cause",
    "invalid_inference",
    "correct",
]


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    claim_id: str


class Change(BaseModel):
    """Claim fields to replace; only the ones written in the row are changed."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    value: str | None = None  # a decimal written as a string, never a float
    display: str | None = None
    unit: str | None = None
    provenance: Provenance | None = None  # replaced whole


class AdversarialRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    base: Base
    change: Change = Field(default_factory=Change)
    expected: Literal["drop", "keep"]
    note: str
    authored_by: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> AdversarialRow:
        want = "drop" if self.category in DROP_CATEGORIES else "keep"
        if self.expected != want:
            raise ValueError(f"category {self.category!r} takes expected={want!r}")
        if self.category in DROP_CATEGORIES and not self.change.model_fields_set:
            raise ValueError(f"category {self.category!r} needs a change")
        return self


def load_rows(path: Path) -> list[AdversarialRow]:
    rows: list[AdversarialRow] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(AdversarialRow.model_validate_json(line))
        except ValidationError as err:
            raise ValueError(f"{path}:{number}: {err}") from err
    ids = [r.id for r in rows]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"{path}: repeated ids {repeated}")
    return rows


def base_claim(trace: Trace, base: Base) -> Claim:
    answer = row_by_id(trace, base.question_id).answer
    for claim in answer.claims if answer else []:
        if claim.id == base.claim_id:
            return claim
    raise KeyError(f"{base.question_id} has no claim {base.claim_id!r}")


def changed_claim(claim: Claim, change: Change) -> Claim:
    update = change.model_dump(mode="json", include=change.model_fields_set)
    return Claim.model_validate(claim.model_dump(mode="json") | update)


def problems(rows: Sequence[AdversarialRow], trace: Trace) -> list[str]:
    """Rows that cannot be scored: no such base, an invalid claim, a change that changes nothing."""
    out: list[str] = []
    for row in rows:
        try:
            claim = base_claim(trace, row.base)
            new = changed_claim(claim, row.change)
        except (KeyError, ValidationError) as err:
            out.append(f"{row.id}: {err}")
            continue
        if row.change.model_fields_set and new == claim:
            out.append(f"{row.id}: the change leaves the claim as it was")
        p = new.provenance
        if isinstance(p, ExprProvenance):
            unknown = sorted(set(p.fact_ids) - trace.facts.keys())
            if unknown:
                out.append(f"{row.id}: facts not saved in the trace: {unknown}")
        elif p.chunk_id not in trace.chunks:
            out.append(f"{row.id}: passage not saved in the trace: {p.chunk_id}")
    return out


def unshown_citations(rows: Sequence[AdversarialRow], trace: Trace) -> list[str]:
    """Rows moved to a saved passage their question's composer never saw. Legitimate (the engine
    would drop it too), but the drop is then for "not shown", not for company or quote."""
    out: list[str] = []
    for row in rows:
        p = changed_claim(base_claim(trace, row.base), row.change).provenance
        if row.change.provenance is None or isinstance(p, ExprProvenance):
            continue
        if p.chunk_id not in row_by_id(trace, row.base.question_id).shown_chunk_ids:
            out.append(f"{row.id}: {p.chunk_id} was not shown for this question")
    return out


def _failed(result: VerifierResult) -> str | None:
    if result.action != "drop":
        return None
    return (result.checks.failed() or ["policy"])[0]


def _outcome(expected: str, changed: bool, base_kept: bool, new_kept: bool) -> str:
    if changed and not base_kept:
        return "vacuous"  # the claim was dropped before the change: the row tests nothing
    if expected == "keep":
        return "kept" if new_kept else "dropped"
    return "missed" if new_kept else "caught"


def score(
    rows: Sequence[AdversarialRow],
    trace: Trace,
    reference: Reference,
    verifier: Verifier = DEFAULT_VERIFIER,
) -> dict[str, Any]:
    cfg = VerifyConfig()
    scored: list[dict[str, Any]] = []
    for row in rows:
        trace_row = row_by_id(trace, row.base.question_id)
        claim = base_claim(trace, row.base)
        evidence, ctx = evidence_for(trace, trace_row), context_for(trace_row, reference)
        before = verify_claim(claim, evidence, ctx, cfg, verifier)
        after = verify_claim(changed_claim(claim, row.change), evidence, ctx, cfg, verifier)
        scored.append(
            {
                "id": row.id,
                "category": row.category,
                "expected": row.expected,
                "outcome": _outcome(
                    row.expected,
                    bool(row.change.model_fields_set),
                    before.action != "drop",
                    after.action != "drop",
                ),
                "base": f"{row.base.question_id}/{row.base.claim_id}",
                "failed_check": _failed(after),
                "reason": after.reason,
                "note": row.note,
            }
        )

    def count(subset: list[dict[str, Any]], outcome: str) -> int:
        return sum(s["outcome"] == outcome for s in subset)

    by_category: dict[str, Any] = {}
    for category in dict.fromkeys(s["category"] for s in scored):
        subset = [s for s in scored if s["category"] == category]
        expected = subset[0]["expected"]
        names = ("caught", "missed") if expected == "drop" else ("kept", "dropped")
        names += ("vacuous",)
        by_category[category] = {
            "expected": expected,
            "rows": len(subset),
            **{name: count(subset, name) for name in names},
        }

    errors = [s for s in scored if s["category"] in DROP_CATEGORIES]
    boundary = [s for s in scored if s["category"] in BOUNDARY_CATEGORIES]
    controls = [s for s in scored if s["category"] == CONTROL_CATEGORY]
    caught = count(errors, "caught")
    return {
        "n_rows": len(scored),
        "level": "claim (the answer's sufficiency policy is not exercised)",
        "errors_to_catch": {
            "n": len(errors),
            "caught": caught,
            "missed": count(errors, "missed"),
            "vacuous": count(errors, "vacuous"),
            "recall": caught / len(errors) if errors else None,
            "recall_note": "caught / all expected-drop rows; vacuous rows count as not caught",
            "ci95_wilson": list(wilson_ci(caught, len(errors))) if errors else None,
        },
        "boundary_known_misses": {
            "note": "bad claims the deterministic verifier is expected to PASS; 'kept' is the "
            "documented limitation, not a success. A 'dropped' row failed a deterministic check "
            "(see its failed_check): that is not detection of the semantic error",
            "n": len(boundary),
            "kept": count(boundary, "kept"),
            "dropped": count(boundary, "dropped"),
            "vacuous": count(boundary, "vacuous"),
        },
        "controls": {
            "n": len(controls),
            "kept": count(controls, "kept"),
            "dropped": count(controls, "dropped"),
            "vacuous": count(controls, "vacuous"),
        },
        "by_category": by_category,
        "rows": scored,
    }


def show(trace: Trace, question_id: str, claim_id: str) -> str:
    """One base claim with everything the verifier will see for it, to write a row against."""
    row = row_by_id(trace, question_id)
    claim = base_claim(trace, Base(question_id=question_id, claim_id=claim_id))
    lines = [
        f"question: {row.question}",
        f"claim: {claim.model_dump_json(indent=2)}",
    ]
    p = claim.provenance
    if isinstance(p, ExprProvenance):
        lines.append("compiled results:")
        lines += [r.model_dump_json() for r in row.results]
        lines.append("cited facts:")
        lines += [trace.facts[i].model_dump_json() for i in p.fact_ids if i in trace.facts]
    else:
        chunk = trace.chunks.get(p.chunk_id)
        lines.append(f"passage (cik {chunk.cik if chunk else '?'}):")
        lines.append(chunk.text if chunk else "(not found)")
    return "\n".join(lines)


def list_bases(trace: Trace, labels: dict[tuple[str, str], str]) -> str:
    """Claims the support judge marked supported: sensible starting points."""
    out = []
    for row in trace.rows:
        for claim in row.answer.claims if row.answer else []:
            if labels.get((row.id, claim.id)) == "supported":
                out.append(f"{row.id} {claim.id} {claim.kind:16} {claim.text[:120]}")
    return "\n".join(out)


def _labels(path: Path) -> dict[tuple[str, str], str]:
    verdicts = json.loads(path.read_text())["verdicts"]
    return {(v["question_id"], v["claim_id"]): v["label"] for v in verdicts}


def main(argv: list[str] | None = None) -> int:
    from eval.run import _git_sha

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--set", dest="set_path", type=Path, default=SET_PATH)
    ap.add_argument("--trace", type=Path, default=TRACE_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--check", action="store_true", help="validate the rows; run no check")
    ap.add_argument("--list-bases", action="store_true")
    ap.add_argument("--show", nargs=2, metavar=("QUESTION_ID", "CLAIM_ID"))
    args = ap.parse_args(argv)
    trace = load_trace(args.trace)
    if args.list_bases:
        print(list_bases(trace, _labels(GATE0_PATH)))
        return 0
    if args.show:
        print(show(trace, *args.show))
        return 0
    if not args.set_path.exists():
        print(f"{args.set_path} does not exist yet: it is written by hand (05 section 6)")
        return 2
    rows = load_rows(args.set_path)
    found = problems(rows, trace)
    if found:
        print("\n".join(found))
        return 2
    labels = _labels(GATE0_PATH)
    for row in rows:
        label = labels.get((row.base.question_id, row.base.claim_id))
        if row.category not in BOUNDARY_CATEGORIES and label != "supported":
            print(f"warning: {row.id}: the judge marked its base claim {label!r}, not supported")
    for warning in unshown_citations(rows, trace):
        print(f"warning: {warning}")
    if args.check:
        print(f"{len(rows)} rows are valid")
        return 0
    try:
        require_implemented()
    except VerifierNotImplemented as err:
        print(err)
        return 2
    report = {
        "kind": "verifier_adversarial",
        "git_sha": _git_sha(),
        "set_file": str(args.set_path),
        "set_sha256": hashlib.sha256(args.set_path.read_bytes()).hexdigest(),
        "trace_file": str(args.trace),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "llm_calls": 0,
        **score(rows, trace, load_reference()),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{'category':24} {'expected':8} result")
    for name, row_counts in report["by_category"].items():
        if row_counts["expected"] == "drop":
            result = f"caught {row_counts['caught']}/{row_counts['rows']}"
            if row_counts["vacuous"]:
                result += f" ({row_counts['vacuous']} vacuous)"
        else:
            result = f"kept {row_counts['kept']}/{row_counts['rows']}"
        print(f"{name:24} {row_counts['expected']:8} {result}")
    errors = report["errors_to_catch"]
    print(
        f"errors caught: {errors['caught']}/{errors['n']} "
        f"({errors['vacuous']} vacuous, counted as not caught) -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
