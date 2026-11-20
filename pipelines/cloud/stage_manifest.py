"""Point a local fetch manifest at S3 objects, for filings already fetched on a laptop.

    uv run python -m pipelines.cloud.stage_manifest \
        --manifest data/raw/filings/_manifest.jsonl --dest s3://<lake>/raw/filings \
        --out dist/filings_manifest.jsonl

The fetcher records each object's `s3_uri` (a `file://` URI when it ran locally). After the
objects are copied with `aws s3 sync`, the same manifest must name the S3 objects, or
`load_filings_html` would load URIs the Glue job cannot read. Everything else in a row stays: the
bytes copied are identical, so `bytes` and `sha256` still describe them. The key layout is the
fetcher's own, `{cik}/{accession}.htm.gz`, and a row with no object (a failed fetch) is untouched.
This only rewrites text; it never touches AWS.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse


def object_key(s3_uri: str) -> str:
    """`{cik}/{accession}.htm.gz` from a `file://` or `s3://` URI of a fetched object."""
    path = unquote(urlparse(s3_uri).path)
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or not parts[-1].endswith(".htm.gz"):
        raise ValueError(f"not a fetched-filing object URI: {s3_uri!r}")
    return "/".join(parts[-2:])


def rewrite_lines(lines: Iterable[str], dest: str) -> list[str]:
    """The manifest lines with every `s3_uri` replaced by `{dest}/{cik}/{accession}.htm.gz`."""
    if not dest.startswith("s3://"):
        raise ValueError(f"--dest must be an s3:// location, got {dest!r}")
    out = []
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("s3_uri"):
            row["s3_uri"] = f"{dest.rstrip('/')}/{object_key(row['s3_uri'])}"
        out.append(json.dumps(row, separators=(",", ":")))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite a local fetch manifest for S3.")
    parser.add_argument("--manifest", type=Path, default=Path("data/raw/filings/_manifest.jsonl"))
    parser.add_argument("--dest", required=True, help="s3://<lake>/raw/filings")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    lines = rewrite_lines(args.manifest.read_text(encoding="utf-8").splitlines(), args.dest)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {len(lines)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
