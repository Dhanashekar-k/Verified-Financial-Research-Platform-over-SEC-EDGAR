"""Fetch the FinanceBench questions and the PDFs they cite (M02 section 3.2).

    uv run python -m pipelines.fetch.fetch_financebench [--dest data/raw/financebench | s3://...]

* The questions file (`financebench_open_source.jsonl`, 150 rows) and each distinct `doc_name`
  it cites are downloaded from the public GitHub repo `patronus-ai/financebench` (CC-BY-NC-4.0).
  This is GitHub, not SEC: a plain `httpx` client with a generic User-Agent. `SEC_USER_AGENT` holds
  contact details and is never sent here.
* Stored under `{dest}`: `financebench_open_source.jsonl`, `pdfs/{doc_name}.pdf` and
  `_manifest.jsonl` (doc_name, url, bytes, sha256, http_status). The JSONL is kept OUT of
  `eval/sets/` on purpose: that path is hook-protected gold. M4 promotes a copy.
* A stored PDF is skipped on rerun; a body that does not start with `%PDF` is a failure (GitHub
  answers rate limits with HTML), and 5xx/connection errors are retried.
* `data/financebench_companies.csv` (company -> CIK) is the owner's file and is never generated
  here; the run prints the company names it must cover.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from pipelines.common.object_store import ObjectStore, open_store
from pipelines.fetch.reference_data import GENERIC_USER_AGENT

log = logging.getLogger(__name__)

_RAW = "https://raw.githubusercontent.com/patronus-ai/financebench/main"
JSONL_URL = f"{_RAW}/data/financebench_open_source.jsonl"
PDF_URL = _RAW + "/pdfs/{doc_name}.pdf"
JSONL_KEY = "financebench_open_source.jsonl"
MANIFEST_KEY = "_manifest.jsonl"
DEFAULT_DEST = "data/raw/financebench"
MAX_ATTEMPTS = 4


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    company: str
    doc_name: str


@dataclass
class Result:
    fetched: int = 0
    skipped: int = 0
    failed: int = 0


def parse_questions(text: str) -> list[Question]:
    questions: list[Question] = []
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            questions.append(Question.model_validate_json(line))
        except ValidationError as exc:
            raise ValueError(f"FinanceBench JSONL line {n} lacks company/doc_name: {exc}") from exc
    return questions


def company_names(questions: Iterable[Question]) -> list[str]:
    return sorted({q.company for q in questions})


def _get(http: httpx.Client, url: str, sleep: Callable[[float], None]) -> tuple[int | None, bytes]:
    """GET with retries on 5xx, 429 and connection errors; returns (status, body)."""
    status: int | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.get(url, headers={"User-Agent": GENERIC_USER_AGENT})
        except httpx.HTTPError as exc:
            log.warning("%s: %s", url, exc)
            status = None
        else:
            status = resp.status_code
            if status < 500 and status != 429:
                return status, resp.content
        if attempt < MAX_ATTEMPTS - 1:
            sleep(min(2.0**attempt, 30.0))
    return status, b""


def fetch_financebench(
    http: httpx.Client,
    store: ObjectStore,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Download the questions and every cited PDF; return counts (also writes the manifest)."""
    result = Result()
    status, body = _get(http, JSONL_URL, sleep)
    if status != 200:
        raise RuntimeError(f"cannot download the FinanceBench questions: HTTP {status}")
    questions = parse_questions(body.decode("utf-8"))
    store.put(JSONL_KEY, body)

    previous = _read_manifest(store)
    rows: dict[str, dict[str, object]] = dict(previous)
    for doc_name in sorted({q.doc_name for q in questions}):
        key = f"pdfs/{doc_name}.pdf"
        url = PDF_URL.format(doc_name=quote(doc_name))
        prev = previous.get(doc_name)
        if prev and prev.get("http_status") == 200 and store.exists(key):
            result.skipped += 1
            continue
        status, content = _get(http, url, sleep)
        if status == 200 and content.startswith(b"%PDF"):
            store.put(key, content)
            rows[doc_name] = {
                "doc_name": doc_name, "url": url, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "http_status": 200,
            }  # fmt: skip
            result.fetched += 1
        else:
            log.warning("%s: not downloaded (HTTP %s)", doc_name, status)
            rows[doc_name] = {
                "doc_name": doc_name, "url": url, "bytes": None, "sha256": None,
                "http_status": status if status != 200 else 0,  # 0 = 200 but not a PDF
            }  # fmt: skip
            result.failed += 1
    store.put(
        MANIFEST_KEY,
        "".join(json.dumps(rows[k], sort_keys=True) + "\n" for k in sorted(rows)).encode(),
    )
    return result


def _read_manifest(store: ObjectStore) -> dict[str, dict[str, object]]:
    try:
        text = store.get(MANIFEST_KEY).decode("utf-8")
    except KeyError:
        return {}
    out: dict[str, dict[str, object]] = {}
    for line in text.splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["doc_name"]] = row
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch FinanceBench questions and PDFs.")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="a directory or s3://bucket/prefix")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    store = open_store(args.dest)
    with httpx.Client(timeout=60.0, follow_redirects=True) as http:
        try:
            result = fetch_financebench(http, store)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 1
    names = company_names(parse_questions(store.get(JSONL_KEY).decode("utf-8")))
    print(f"fetched {result.fetched}, skipped {result.skipped}, failed {result.failed}")
    print(
        f"\n{len(names)} companies need a CIK in data/financebench_companies.csv "
        "(columns: company,cik; you write and verify it):"
    )
    print("\n".join(f"  {n}" for n in names))
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
