"""Fetch filing HTML into an object store (M02 section 3.2; contract section 2.1).

    uv run python -m pipelines.fetch.fetch_filings --targets data/targets/filings.csv
        [--dest data/raw/filings | s3://<lake>/raw/filings] [--cik N ...] [--limit N] [--workers 4]

For every row of `filings.csv` it downloads the primary document through `SecClient`, gzips it
(deterministically: mtime 0) and stores it at `{cik}/{accession}.htm.gz`. One `raw.filings_html`
record per target is kept in `_manifest.jsonl` at the top of the destination (the parse job's
`*/*.htm.gz` glob does not match it); `pipelines.spark.load_filings_html` loads it into Iceberg.

* `bytes` and `sha256` describe the STORED object (the gzip), so they match what S3 holds.
* A failed fetch is a record too (`http_status`, no `s3_uri`/`bytes`/`sha256`) and is retried on
  the next run. A filing is skipped only when its record says 200 AND the object still exists, so
  a half-written or deleted object heals itself. Reruns are idempotent and byte-stable.
* Run it from AWS for the full list (bandwidth), not from a home connection. With an `s3://`
  destination it needs `s3:PutObject` and `s3:GetObject` on the prefix AND `s3:ListBucket` on the
  bucket: without ListBucket S3 answers 403, not 404, for a key that does not exist, so even the
  first run (no manifest yet) fails. Claude only runs it locally on a few CIKs (CLAUDE.md rule 8).
* A store failure (not a failed filing) or Ctrl-C stops the run immediately and still writes the
  manifest, so completed filings are not fetched again. One accession listed under several CIKs
  is fetched once (the lake is keyed on accession).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import logging
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from pipelines.common.object_store import ObjectStore, open_store
from pipelines.fetch.list_filings import CSV_HEADER, FilingTarget
from pipelines.fetch.sec_client import SecClient, SecClientConfigError, SecRequestError

log = logging.getLogger(__name__)

MANIFEST_KEY = "_manifest.jsonl"
DEFAULT_DEST = "data/raw/filings"
DEFAULT_TARGETS = Path("data/targets/filings.csv")


class FetchRecord(BaseModel):
    """One `raw.filings_html` row."""

    model_config = ConfigDict(frozen=True)

    accession: str
    cik: int
    form: str
    filed: date
    report_date: date | None = None  # added after the first manifests; older rows lack it
    url: str
    s3_uri: str | None
    bytes: int | None
    sha256: str | None
    http_status: int | None
    fetched_at: datetime


@dataclass
class Summary:
    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    stored_bytes: int = 0


def object_key(cik: int, accession: str) -> str:
    return f"{cik}/{accession}.htm.gz"


def gzip_deterministic(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(data)
    return buf.getvalue()


def _sort_key(t: FilingTarget) -> tuple[int, date, str]:
    return (t.cik, t.filed, t.accession)


def read_targets(path: Path) -> list[FilingTarget]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != CSV_HEADER:
            raise ValueError(f"{path} header is {reader.fieldnames}, expected {CSV_HEADER}")
        return [
            FilingTarget(
                cik=int(r["cik"]),
                accession=r["accession"],
                form=r["form"],
                filed=date.fromisoformat(r["filed"]),
                report_date=date.fromisoformat(r["report_date"]) if r["report_date"] else None,
                primary_doc=r["primary_doc"],
                url=r["url"],
            )
            for r in reader
        ]


def read_manifest(store: ObjectStore) -> list[FetchRecord]:
    try:
        text = store.get(MANIFEST_KEY).decode("utf-8")
    except KeyError:
        return []
    return [FetchRecord.model_validate_json(line) for line in text.splitlines() if line.strip()]


def write_manifest(store: ObjectStore, records: Iterable[FetchRecord]) -> None:
    ordered = sorted(records, key=lambda r: r.accession)
    store.put(MANIFEST_KEY, "".join(r.model_dump_json() + "\n" for r in ordered).encode("utf-8"))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _failure(t: FilingTarget, status: int | None, now: datetime) -> FetchRecord:
    return FetchRecord(
        accession=t.accession, cik=t.cik, form=t.form, filed=t.filed,
        report_date=t.report_date, url=t.url, s3_uri=None, bytes=None, sha256=None,
        http_status=status, fetched_at=now,
    )  # fmt: skip


def _fetch_one(
    target: FilingTarget,
    store: ObjectStore,
    client: SecClient,
    previous: FetchRecord | None,
    now: Callable[[], datetime],
) -> tuple[FetchRecord, str]:
    """Returns the record and one of 'skipped', 'fetched', 'failed'."""
    key = object_key(target.cik, target.accession)
    if (
        previous is not None
        and previous.http_status == 200
        and previous.sha256 is not None
        and store.exists(key)
    ):
        if previous.report_date != target.report_date:  # an older manifest row: refresh, no refetch
            previous = previous.model_copy(update={"report_date": target.report_date})
        return previous, "skipped"
    try:
        raw = client.get(target.url)
    except SecRequestError as exc:
        log.warning("%s: %s", target.accession, exc)
        return _failure(target, exc.status_code, now()), "failed"
    except httpx.HTTPError as exc:
        log.warning("%s: %s", target.accession, exc)
        return _failure(target, None, now()), "failed"
    stored = gzip_deterministic(raw)
    store.put(key, stored)
    record = FetchRecord(
        accession=target.accession, cik=target.cik, form=target.form, filed=target.filed,
        report_date=target.report_date, url=target.url, s3_uri=store.uri(key), bytes=len(stored),
        sha256=hashlib.sha256(stored).hexdigest(), http_status=200, fetched_at=now(),
    )  # fmt: skip
    return record, "fetched"


def fetch_all(
    targets: Iterable[FilingTarget],
    store: ObjectStore,
    client: SecClient,
    *,
    workers: int = 4,
    limit: int | None = None,
    flush_every: int = 200,
    now: Callable[[], datetime] = _utcnow,
) -> tuple[list[FetchRecord], Summary]:
    """Fetch every target not already stored; return this run's records and the counts."""
    chosen = sorted(targets, key=_sort_key)
    # `raw.filings_html` and the manifest are keyed on accession alone. A combined filing listed
    # under several co-registrant CIKs would collide there, so only its first CIK is fetched.
    unique = {t.accession: t for t in reversed(chosen)}
    if len(unique) != len(chosen):
        log.warning(
            "%d targets share an accession with another CIK; keeping one", len(chosen) - len(unique)
        )
        chosen = sorted(unique.values(), key=_sort_key)
    if limit is not None:
        chosen = chosen[:limit]
    state = {r.accession: r for r in read_manifest(store)}
    summary = Summary()
    mine: dict[str, FetchRecord] = {}
    done = 0

    def finish(record: FetchRecord, kind: str) -> None:
        nonlocal done
        mine[record.accession] = record
        state[record.accession] = record
        done += 1
        if kind == "skipped":
            summary.skipped += 1
        elif kind == "fetched":
            summary.fetched += 1
            summary.stored_bytes += record.bytes or 0
        else:
            summary.failed += 1
        if done % flush_every == 0:
            write_manifest(store, state.values())
        if done % 500 == 0:
            log.info("%d/%d filings processed", done, len(chosen))

    # A failure of the store itself (S3 AccessDenied, a full disk) or Ctrl-C is not a failed
    # filing: stop at once instead of downloading the rest of the list into the void, and still
    # write the manifest so what WAS stored is not fetched again on the rerun.
    try:
        if workers <= 1:
            for t in chosen:
                finish(*_fetch_one(t, store, client, state.get(t.accession), now))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_fetch_one, t, store, client, state.get(t.accession), now)
                    for t in chosen
                ]
                try:
                    for fut in as_completed(futures):
                        finish(*fut.result())
                except BaseException:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
    finally:
        write_manifest(store, state.values())
    return sorted(mine.values(), key=lambda r: r.accession), summary


def main(
    argv: list[str] | None = None,
    *,
    client: SecClient | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> int:
    parser = argparse.ArgumentParser(description="Fetch filing HTML into an object store.")
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--dest", default=DEFAULT_DEST, help="a directory or s3://bucket/prefix")
    parser.add_argument("--cik", type=int, action="append", help="repeatable; restricts the run")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        targets = read_targets(args.targets)
    except (OSError, ValueError) as exc:
        log.error("cannot read the targets: %s", exc)
        return 2
    if args.cik:
        wanted = set(args.cik)
        targets = [t for t in targets if t.cik in wanted]
    if not targets:
        log.error("no targets to fetch")
        return 2

    load_dotenv()
    owns_client = client is None
    try:
        sec = client or SecClient()
    except SecClientConfigError as exc:
        log.error("%s", exc)
        return 2
    store = open_store(args.dest)
    try:
        _, summary = fetch_all(targets, store, sec, workers=args.workers, limit=args.limit, now=now)
    finally:
        if owns_client:
            sec.close()

    print(
        f"fetched {summary.fetched}, skipped {summary.skipped}, failed {summary.failed}; "
        f"{summary.stored_bytes} bytes stored this run"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
