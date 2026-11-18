"""Check that citation URLs resolve (M02 section 3.5): GET each document URL, expect HTTP 200.

    uv run python -m eval.check_source_urls [--metrics docs/milestones/M02-parse-metrics.json]
        [--rate 2] [--out docs/milestones/M02-url-check.json]

Takes the 50 random chunk `source_url`s sampled by `eval/parse_report.py`, drops the `#fragment`
(a server never sees it: anchors and text fragments cannot be validated here, so the owner
spot-checks 5 in a browser) and requests each distinct document once, through `SecClient` at
`--rate` requests per second (CLAUDE.md rule 9). The result counts documents, not chunks: several
sampled chunks can cite the same filing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

from dotenv import load_dotenv

from pipelines.fetch.sec_client import SecClient, SecClientConfigError, SecRequestError, TokenBucket

DEFAULT_METRICS = Path("docs/milestones/M02-parse-metrics.json")
DEFAULT_OUT = Path("docs/milestones/M02-url-check.json")


def check_urls(client: SecClient, sampled: list[str]) -> dict[str, Any]:
    documents = sorted({urldefrag(u).url for u in sampled})
    results: list[dict[str, Any]] = []
    for url in documents:
        try:
            body = client.get(url)
            results.append({"url": url, "status": 200, "bytes": len(body)})
        except SecRequestError as exc:
            results.append({"url": url, "status": exc.status_code, "error": str(exc)})
    ok = sum(r["status"] == 200 for r in results)
    return {
        "sampled_chunk_urls": len(sampled),
        "distinct_documents": len(documents),
        "ok": ok,
        "failed": [r for r in results if r["status"] != 200],
        "results": results,
    }


def main(argv: list[str] | None = None, *, client: SecClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that source URLs return 200.")
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS)
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not args.metrics.is_file():
        print(f"URL check: NOT MEASURED ({args.metrics} does not exist; run eval.parse_report)")
        return 0
    sampled = json.loads(args.metrics.read_text())["html"]["url_sample"]
    load_dotenv()
    try:
        sec = client or SecClient(bucket=TokenBucket(rate=args.rate))
    except SecClientConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        result = check_urls(sec, sampled)
    finally:
        if client is None:
            sec.close()
    result["rate_per_second"] = args.rate
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"{result['ok']}/{result['distinct_documents']} documents returned 200 "
        f"({result['sampled_chunk_urls']} sampled chunk URLs)"
    )
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
