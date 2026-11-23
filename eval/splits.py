"""Stratified dev/test splits per eval set (M04 3.6).

Dev is about 30% and test about 70% of each set, stratified so every stratum appears in dev
with the same proportion (rounded, at least one item in dev for strata of two or more). The
seed is fixed and recorded. Iterate on dev only; report test (and all) at milestones.

Writes to ``eval/sets/drafts/splits.json`` by default; Shekar promotes it to ``eval/sets/``
with the gold files (``eval/sets/splits.json`` is hook-protected only as part of the gold set's
checksum pass, so it is promoted, never written in place).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from eval.schemas import SET_FILES, load_set

DEV_FRACTION = 0.3
SEED = 13


def _stratum(name: str) -> Callable[[Any], str]:
    """The field each set is stratified on."""
    return {
        "numeric": lambda r: r.qtype,
        "financebench": lambda r: r.question_type,
        "narrative": lambda r: "two_chunk" if len(r.gold_chunk_ids) > 1 else "one_chunk",
        "unanswerable": lambda r: r.reason,
    }[name]


def split_ids(
    rows: list[BaseModel],
    stratum: Callable[[Any], str],
    *,
    dev_fraction: float = DEV_FRACTION,
    seed: int = SEED,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[stratum(row)].append(row.id)  # type: ignore[attr-defined]
    dev: list[str] = []
    test: list[str] = []
    for key in sorted(groups):
        # Order by a seeded hash of the id, not a shared RNG: adding a row to one stratum moves
        # at most the boundary item of that stratum, never items of other strata.
        ids = sorted(groups[key], key=lambda i: hashlib.sha256(f"{seed}:{i}".encode()).hexdigest())
        n_dev = round(len(ids) * dev_fraction)
        if len(ids) >= 2:
            n_dev = min(max(n_dev, 1), len(ids) - 1)
        dev += ids[:n_dev]
        test += ids[n_dev:]
    return {"dev": sorted(dev), "test": sorted(test)}


def make_splits(
    sets_dir: Path, *, dev_fraction: float = DEV_FRACTION, seed: int = SEED
) -> dict[str, Any]:
    out: dict[str, Any] = {"seed": seed, "dev_fraction": dev_fraction, "sets": {}, "missing": []}
    for name in SET_FILES:
        if not (sets_dir / SET_FILES[name]).exists():
            out["missing"].append(name)
            continue
        rows = load_set(name, sets_dir)
        out["sets"][name] = split_ids(rows, _stratum(name), dev_fraction=dev_fraction, seed=seed)
    return out


def ids_for(split: str, set_name: str, splits: dict[str, Any] | None) -> set[str] | None:
    """The ids of one split of one set; None means no filter (split 'all')."""
    if split == "all":
        return None
    if splits is None or set_name not in splits["sets"]:
        raise FileNotFoundError(f"no {split} split for set {set_name!r}; run eval.splits first")
    return set(splits["sets"][set_name][split])


def check_coverage(set_name: str, gold_ids: set[str], splits: dict[str, Any]) -> None:
    """Fail when the gold set and its split disagree (a row added or removed after splitting)."""
    s = splits["sets"][set_name]
    split_ids = set(s["dev"]) | set(s["test"])
    if split_ids != gold_ids:
        raise ValueError(
            f"{set_name}: splits.json is stale: {len(gold_ids - split_ids)} gold ids not split, "
            f"{len(split_ids - gold_ids)} split ids not in gold; re-run eval.splits"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sets-dir", type=Path, default=Path("eval/sets"))
    ap.add_argument("--out", type=Path, default=Path("eval/sets/drafts/splits.json"))
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args(argv)
    splits = make_splits(args.sets_dir, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(splits, indent=2, sort_keys=True) + "\n")
    for name, s in splits["sets"].items():
        print(f"{name}: dev {len(s['dev'])}, test {len(s['test'])}")
    if splits["missing"]:
        print("missing sets (not split):", ", ".join(splits["missing"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
