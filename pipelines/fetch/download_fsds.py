"""Download the SEC Financial Statement Data Sets (FSDS) quarterly ZIPs (M01 section 3.2).

For each quarter ``YYYYqN`` this writes, under ``--dest``::

    {q}/sub.txt.gz  {q}/num.txt.gz  {q}/tag.txt.gz  {q}/pre.txt.gz  {q}/readme.htm
    _manifest.json      # per quarter: source URL, ZIP size + SHA-256, size + SHA-256 of each .gz

Spark reads the gzip files directly. The ZIP itself is not kept.

Properties that matter for the pipeline:

* **Idempotent.** A quarter whose four ``.gz`` files exist with the byte sizes recorded in the
  manifest is skipped without any HTTP request. Anything else (missing file, size drift, missing
  or corrupt manifest entry) re-downloads it.
* **Deterministic output.** gzip headers carry no name or mtime, so the same ZIP always yields
  byte-identical files and hashes.
* **All-or-nothing per quarter.** The ZIP is fully validated (all four members present, readable)
  before anything is written under ``--dest``; each file is renamed into place atomically and the
  manifest entry is written last, so a crash never leaves a quarter that looks complete.
* **SEC rules.** All HTTP goes through :class:`SecClient`. ZIP members are matched by exact name
  and read as streams, so a hostile member name can never become a path.

2009q1 is headers only and is never requested. S3 destinations are not supported yet.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from pipelines.fetch.sec_client import SecClient, SecClientConfigError, SecRequestError

log = logging.getLogger(__name__)

FSDS_FILES = ("sub", "num", "tag", "pre")
_BASE_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets"
_CHUNK = 1024 * 1024
_QUARTER_RE = re.compile(r"^(\d{4})[qQ]([1-4])$")
MANIFEST_NAME = "_manifest.json"


@dataclass(frozen=True, order=True)
class Quarter:
    year: int
    q: int

    def __post_init__(self) -> None:
        if not 1 <= self.q <= 4:
            raise ValueError(f"quarter must be 1..4, got {self.q}")

    def __str__(self) -> str:
        return f"{self.year}q{self.q}"

    @classmethod
    def parse(cls, text: str) -> Quarter:
        match = _QUARTER_RE.match(text)
        if match is None:
            raise ValueError(f"not a quarter like '2023q3': {text!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    def next(self) -> Quarter:
        return Quarter(self.year + 1, 1) if self.q == 4 else Quarter(self.year, self.q + 1)

    @property
    def url(self) -> str:
        return f"{_BASE_URL}/{self}.zip"


FIRST_QUARTER = Quarter(2009, 2)  # 2009q1.zip is headers only


def quarter_range(start: Quarter, end: Quarter) -> list[Quarter]:
    """Inclusive, ascending."""
    if start < FIRST_QUARTER:
        raise ValueError(f"FSDS starts at {FIRST_QUARTER} (2009q1 is headers only), got {start}")
    if start > end:
        raise ValueError(f"start {start} is after end {end}")
    quarters = [start]
    while quarters[-1] < end:
        quarters.append(quarters[-1].next())
    return quarters


def current_quarter(today: date) -> Quarter:
    return Quarter(today.year, (today.month - 1) // 3 + 1)


@dataclass(frozen=True)
class QuarterResult:
    quarter: Quarter
    status: Literal["downloaded", "skipped", "not_released"]


# -- manifest ---------------------------------------------------------------------------


def _load_manifest(dest: Path) -> dict[str, Any]:
    """Read-only view for the skip check: anything unreadable or malformed counts as empty."""
    try:
        data = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_manifest_for_update(dest: Path) -> dict[str, Any]:
    """The manifest to merge a new entry into. Never silently discards other quarters.

    A missing file starts a new manifest. An unreadable file (I/O or permission error) is
    raised: overwriting it would lose every other quarter's provenance. A present but invalid
    file is moved aside to ``_manifest.json.corrupt-<timestamp>`` with a warning, so it can be
    inspected, and a fresh manifest starts.
    """
    path = dest / MANIFEST_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            return data
    except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
        pass
    aside = path.with_name(f"{MANIFEST_NAME}.corrupt-{datetime.now(UTC):%Y%m%dT%H%M%S%f}")
    os.replace(path, aside)
    log.warning("%s is not a valid manifest; moved to %s and starting a new one", path, aside)
    return {}


@contextlib.contextmanager
def _dest_lock(dest: Path) -> Iterator[None]:
    """Exclusive advisory lock on the destination directory (no stray lock file).

    Serializes extraction and the manifest read-modify-write between processes that share a
    ``--dest``, so overlapping runs on different ranges can't drop each other's entries.
    """
    dest.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _is_complete(dest: Path, quarter: Quarter) -> bool:
    """True if the manifest entry is well-formed and all four .gz files match its sizes."""
    entry = _load_manifest(dest).get(str(quarter))
    if not isinstance(entry, dict) or not isinstance(entry.get("files"), dict):
        return False
    for name in FSDS_FILES:
        info = entry["files"].get(name)
        if not isinstance(info, dict):
            return False
        size = info.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool):
            return False
        path = dest / str(quarter) / f"{name}.txt.gz"
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
        except OSError:
            return False
    return True


# -- one quarter ------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _gzip_member(zf: zipfile.ZipFile, member: str, out_dir: Path, final_name: str) -> Path:
    """Stream one ZIP member into ``out_dir/final_name`` as a deterministic gzip file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / final_name
    fd, tmp_name = tempfile.mkstemp(dir=out_dir, prefix=f".{final_name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw:
            # filename="" and mtime=0: no name or timestamp in the header, so output is
            # byte-identical across runs and destinations.
            with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
                with zf.open(member) as src:
                    while chunk := src.read(_CHUNK):
                        gz.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        tmp.chmod(0o644)
        os.replace(tmp, final)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return final


def download_quarter(client: SecClient, quarter: Quarter, dest: Path) -> QuarterResult:
    """Download, extract and gzip one quarter into ``dest``; skip it if already complete."""
    dest = Path(dest)
    if _is_complete(dest, quarter):
        log.info("%s: already complete, skipping", quarter)
        return QuarterResult(quarter, "skipped")

    # The ZIP lives in a throwaway directory outside ``dest``, so a failure leaves nothing behind.
    with tempfile.TemporaryDirectory(prefix=f"fsds-{quarter}-") as scratch:
        zip_path = Path(scratch) / f"{quarter}.zip"
        zip_sha256 = client.download(quarter.url, zip_path)
        zip_bytes = zip_path.stat().st_size
        try:
            with _dest_lock(dest):
                # Load first: an unreadable manifest must stop us before we touch any file.
                manifest = _load_manifest_for_update(dest)
                files = _extract(zip_path, quarter, dest)
                manifest[str(quarter)] = {
                    "source_url": quarter.url,
                    "zip_bytes": zip_bytes,
                    "zip_sha256": zip_sha256,
                    "downloaded_at": datetime.now(UTC).isoformat(),
                    "files": files,
                }
                # Written last: until now the quarter does not count as complete.
                _write_atomic(
                    dest / MANIFEST_NAME,
                    (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            raise ValueError(f"{quarter}: {quarter.url} is not a usable ZIP ({exc})") from exc
    log.info("%s: downloaded (%d bytes zipped)", quarter, zip_bytes)
    return QuarterResult(quarter, "downloaded")


def _extract(zip_path: Path, quarter: Quarter, dest: Path) -> dict[str, dict[str, Any]]:
    """Validate the ZIP, then write the four gzip files (and readme) under ``dest/{quarter}``."""
    qdir = dest / str(quarter)
    with zipfile.ZipFile(zip_path) as zf:
        names = {info.filename for info in zf.infolist() if not info.is_dir()}
        missing = [f"{n}.txt" for n in FSDS_FILES if f"{n}.txt" not in names]
        if missing:
            raise ValueError(f"{quarter}: ZIP is missing required member(s): {', '.join(missing)}")

        written: list[Path] = []
        files: dict[str, dict[str, Any]] = {}
        try:
            for name in FSDS_FILES:
                path = _gzip_member(zf, f"{name}.txt", qdir, f"{name}.txt.gz")
                written.append(path)
                files[name] = {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}
            if "readme.htm" in names:
                with zf.open("readme.htm") as src:
                    _write_atomic(qdir / "readme.htm", src.read())
        except BaseException:
            # Do not leave a half-written quarter behind (it has no manifest entry either way).
            for path in written:
                path.unlink(missing_ok=True)
            if qdir.is_dir() and not any(qdir.iterdir()):
                shutil.rmtree(qdir, ignore_errors=True)
            raise
    return files


# -- a range of quarters ----------------------------------------------------------------


def download_range(
    client: SecClient,
    start: Quarter,
    end: Quarter | Literal["latest"],
    dest: Path,
    today: date | None = None,
    progress: Callable[[QuarterResult], None] | None = None,
) -> list[QuarterResult]:
    """Download ``start..end`` in ascending order.

    With ``end="latest"`` the end is the current calendar quarter. Quarters the SEC hasn't
    published yet answer 404 and are reported as ``not_released``, but only if every later
    quarter is also missing: a 404 followed by a quarter that exists is a real error.
    ``progress`` is called with each result as it happens, so a long run that fails part-way
    still reports what it finished.
    """
    latest = end == "latest"
    end_q = current_quarter(today or date.today()) if end == "latest" else end
    assert isinstance(end_q, Quarter)
    quarters = quarter_range(start, end_q)  # validates before any request

    results: list[QuarterResult] = []
    first_missing: SecRequestError | None = None
    for quarter in quarters:
        try:
            result = download_quarter(client, quarter, dest)
        except SecRequestError as exc:
            if not (latest and exc.status_code == 404):
                raise
            first_missing = first_missing or exc
            results.append(QuarterResult(quarter, "not_released"))
            if progress:
                progress(results[-1])
            continue
        if first_missing is not None:
            # A quarter after a 404 exists, so that 404 was a hole, not "not released yet".
            raise first_missing
        results.append(result)
        if progress:
            progress(result)
    return results


# -- command line -----------------------------------------------------------------------


def _quarter_arg(text: str) -> Quarter:
    try:
        return Quarter.parse(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _end_arg(text: str) -> Quarter | Literal["latest"]:
    return "latest" if text.lower() == "latest" else _quarter_arg(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download SEC Financial Statement Data Sets.")
    parser.add_argument("--from", dest="start", type=_quarter_arg, default=FIRST_QUARTER)
    parser.add_argument("--to", dest="end", type=_end_arg, default="latest")
    parser.add_argument("--dest", default="data/raw/fsds")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if str(args.dest).startswith("s3://"):
        print(
            "error: s3:// destinations are not supported yet (needs boto3); use a local path.",
            file=sys.stderr,
        )
        return 2

    # Usage errors (bad range) are exit 2, like argparse's; failures while running are exit 1.
    try:
        end_q = current_quarter(date.today()) if args.end == "latest" else args.end
        quarter_range(args.start, end_q)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    load_dotenv()  # SEC_USER_AGENT lives in .env; never overrides a variable already set
    try:
        client = SecClient()
        with client:
            download_range(
                client,
                args.start,
                args.end,
                Path(args.dest),
                progress=lambda r: print(f"{r.quarter}: {r.status}", flush=True),
            )
    except SecClientConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, SecRequestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
