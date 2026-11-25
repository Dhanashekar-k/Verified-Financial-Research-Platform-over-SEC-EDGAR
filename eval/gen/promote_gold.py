"""Promote a REVIEWED draft set to gold (Shekar runs this; it writes ``eval/sets/<set>.jsonl``).

    make promote-gold SET=narrative VERIFIED_BY=<your name>     # then: make gold-checksums
    make promote-gold SET=unanswerable

Running it is the statement "I reviewed these rows". The narrative draft carries review-only
fields (``_source_chunks``, the drafter's note, ...) and ``verified_by``/``verified_at`` of null;
this strips the ``_`` fields, stamps who verified and when, validates every row against the
set's schema (``eval/schemas.py``, ``extra="forbid"``), checks the row count and refuses to
overwrite an existing gold file. The unanswerable draft is validated and copied as is (the
schema has no reviewer field, so ``--verified-by`` is not recorded for it; your running this is
the review).
Claude does not run this: it writes gold (CLAUDE.md rule 2).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from eval.schemas import ROW_MODELS

EXPECTED_N = {"narrative": 50, "unanswerable": 50}
DRAFTS = {
    "narrative": Path("eval/sets/drafts/narrative_selected.jsonl"),
    "unanswerable": Path("eval/sets/drafts/unanswerable.jsonl"),
}


def prepare(
    name: str, drafts: Path, verified_by: str | None, verified_on: date
) -> list[dict[str, Any]]:
    """Validated gold rows for ``name`` from its reviewed draft file."""
    if name not in DRAFTS:
        raise ValueError(f"unknown set {name!r}; choose one of {sorted(DRAFTS)}")
    rows = [json.loads(x) for x in drafts.read_text().splitlines() if x.strip()]
    if len(rows) != EXPECTED_N[name]:
        raise ValueError(f"{drafts}: {len(rows)} rows, expected {EXPECTED_N[name]}")
    model = ROW_MODELS[name]
    out: list[dict[str, Any]] = []
    for row in rows:
        row = {k: v for k, v in row.items() if not k.startswith("_")}
        if name == "narrative":
            if not verified_by or not verified_by.strip():
                raise ValueError("narrative rows need --verified-by (who reviewed them)")
            row["verified_by"] = verified_by.strip()
            row["verified_at"] = verified_on.isoformat()
        out.append(json.loads(model.model_validate(row).model_dump_json()))
    ids = [r["id"] for r in out]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate ids")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("set", choices=sorted(DRAFTS))
    ap.add_argument("--verified-by", default=None)
    ap.add_argument("--drafts", type=Path, default=None)
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    args = ap.parse_args(argv)
    target = args.sets_dir / f"{args.set}.jsonl"
    if target.exists():
        print(f"refusing: {target} already exists (gold is never overwritten)", file=sys.stderr)
        return 2
    try:
        rows = prepare(args.set, args.drafts or DRAFTS[args.set], args.verified_by, date.today())
    except (ValueError, OSError) as err:
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    tmp = target.with_suffix(".jsonl.tmp")  # never leave a truncated gold file behind
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    tmp.replace(target)
    print(f"wrote {target} ({len(rows)} rows). Next: make gold-checksums && make check-gold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
