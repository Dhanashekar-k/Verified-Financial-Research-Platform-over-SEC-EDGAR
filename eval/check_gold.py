"""Verify the gold evaluation files against `eval/sets/CHECKSUMS` (CLAUDE.md rule 2).

    uv run python -m eval.check_gold            # check (CI: `make check-gold`)
    uv run python -m eval.check_gold --write    # Shekar only: `make gold-checksums`

Gold files are `eval/sets/*.jsonl` (not `eval/sets/drafts/`) and `eval/judges/calibration.jsonl`.
CHECKSUMS holds `<sha256>  <path relative to the repo root>` lines sorted by path. The Edit/Write
hook cannot see shell edits; this check can.

With no gold files at all there is nothing to verify and the check skips (exit 0). As soon as one
exists, a missing CHECKSUMS file, an edited, deleted or unlisted gold file is a failure (exit 1).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

CHECKSUMS = "eval/sets/CHECKSUMS"
CALIBRATION = "eval/judges/calibration.jsonl"


def find_gold_files(root: Path) -> list[str]:
    """Repo-relative paths of every gold file that exists, sorted."""
    found = {p.relative_to(root).as_posix() for p in (root / "eval" / "sets").glob("*.jsonl")}
    if (root / CALIBRATION).is_file():
        found.add(CALIBRATION)
    return sorted(found)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_checksums(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64:
            raise ValueError(f"malformed CHECKSUMS line: {line!r}")
        entries[name] = digest
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify gold files against CHECKSUMS.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true", help="rewrite CHECKSUMS (Shekar only)")
    args = parser.parse_args(argv)
    root: Path = args.root

    gold = find_gold_files(root)
    checksums = root / CHECKSUMS
    if not gold and not checksums.is_file():
        print("check-gold: skipped, no gold files exist yet")
        return 0

    if args.write:
        if not gold:
            print(
                "check-gold: refusing --write with no gold files; CHECKSUMS lists some",
                file=sys.stderr,
            )
            return 1
        lines = [f"{_digest(root / name)}  {name}" for name in gold]
        (root / CHECKSUMS).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"check-gold: wrote {len(lines)} checksums to {CHECKSUMS}")
        return 0

    if not checksums.is_file():
        print(
            f"check-gold: {len(gold)} gold file(s) exist but {CHECKSUMS} is missing. "
            "Run `make gold-checksums` and commit the result.",
            file=sys.stderr,
        )
        return 1
    try:
        expected = _read_checksums(checksums)
    except ValueError as exc:
        print(f"check-gold: {exc}", file=sys.stderr)
        return 1

    problems: list[str] = []
    for name in sorted(set(expected) | set(gold)):
        if name not in expected:
            problems.append(f"{name}: gold file is not listed in CHECKSUMS")
        elif not (root / name).is_file():
            problems.append(f"{name}: listed in CHECKSUMS but missing")
        elif _digest(root / name) != expected[name]:
            problems.append(f"{name}: content differs from CHECKSUMS")
    if problems:
        print("check-gold: FAILED", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"check-gold: {len(gold)} gold file(s) match CHECKSUMS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
