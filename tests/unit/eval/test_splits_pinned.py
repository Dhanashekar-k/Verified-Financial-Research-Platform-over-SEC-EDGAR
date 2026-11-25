"""The committed dev/test split is exactly what seed 13 produces from the committed gold sets.

`eval/sets/splits.json` is not in `eval/sets/CHECKSUMS` (that file covers `*.jsonl` and is written
only by Shekar's `make gold-checksums`). A silent re-split would move questions between dev and
test, so this test regenerates the split from the gold sets and compares. It also fails when a gold
row is added or removed without re-splitting.
"""

from __future__ import annotations

import json
from pathlib import Path

from eval.splits import SEED, make_splits

ROOT = Path(__file__).resolve().parents[3]
SETS = ROOT / "eval" / "sets"


def test_committed_splits_match_seed_regeneration() -> None:
    committed = json.loads((SETS / "splits.json").read_text(encoding="utf-8"))
    assert committed["seed"] == SEED == 13
    assert make_splits(SETS, seed=committed["seed"]) == committed
