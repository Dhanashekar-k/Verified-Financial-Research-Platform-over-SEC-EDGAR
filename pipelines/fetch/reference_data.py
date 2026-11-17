"""Fetch the two reference files behind `curated.companies` (M01 section 2, contract section 1.5).

    uv run python -m pipelines.fetch.reference_data [--dest data] [--date 2026-11-17] [--refresh]

* `company_tickers.json` (cik, ticker, name) comes from sec.gov, so it goes through `SecClient`
  (CLAUDE.md rule 9: User-Agent from `SEC_USER_AGENT`, rate limit, retries).
* The S&P 500 constituents come from a public GitHub dataset, NOT from the SEC: `SecClient`
  refuses non-SEC hosts by design. That request is a plain `httpx` GET with `GENERIC_USER_AGENT`;
  `SEC_USER_AGENT` holds contact details and must never be sent to a third party.

Both are frozen to dated files (`company_tickers_<date>.json`, `sp500_<date>.csv`): index
membership changes over time, so the build is reproducible from the snapshot, and an existing
snapshot is never overwritten unless `--refresh` is given.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from pipelines.fetch.sec_client import SecClient, SecClientConfigError, SecRequestError

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SP500_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)
GENERIC_USER_AGENT = "edgar-verified-qa/0.1 (portfolio project)"
MIN_SP500_ROWS = 400  # a sanity floor: the index has about 500 rows; fewer means a broken source


@dataclass(frozen=True)
class TickerRow:
    cik: int
    ticker: str
    name: str
    rank: int  # the integer key of the SEC file ("0", "1", ...): its own ordering of tickers


def parse_company_tickers(data: dict[str, Any]) -> list[TickerRow]:
    """Rows of the SEC ticker map, in the file's own order (by rank)."""
    if not data:
        raise ValueError("company_tickers.json is empty")
    rows: list[TickerRow] = []
    for key, entry in data.items():
        try:
            rows.append(
                TickerRow(
                    cik=_cik(entry["cik_str"]),
                    ticker=_text(entry["ticker"]),
                    name=_text(entry["title"]),
                    rank=int(key),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"company_tickers.json entry {key!r} is malformed: {entry!r}") from exc
    return sorted(rows, key=lambda r: r.rank)


def _cik(value: Any) -> int:
    """An int or a numeric string. A float or bool would be truncated onto another company."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"cik_str must be an int or a numeric string, got {value!r}")
    return int(value)


def _text(value: Any) -> str:
    """A non-blank string: `str(None)` would store the text 'None' as a ticker."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected a non-blank string, got {value!r}")
    return value


def _atomic_write(dest: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, so a failure never leaves a partial file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch_company_tickers(client: SecClient, dest: Path) -> Path:
    """Download the SEC ticker map, validate it, and freeze the original JSON at `dest`."""
    data = client.get_json(TICKERS_URL)
    parse_company_tickers(data)
    _atomic_write(dest, json.dumps(data, sort_keys=False).encode())
    return dest


def fetch_sp500(
    dest: Path,
    *,
    http: httpx.Client | None = None,
    url: str = SP500_URL,
    min_rows: int = MIN_SP500_ROWS,
) -> Path:
    """Download the S&P 500 constituents and freeze them as a `ticker,name` CSV at `dest`."""
    owns_client = http is None
    client = http or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        resp = client.get(url, headers={"User-Agent": GENERIC_USER_AGENT})
        resp.raise_for_status()
        text = resp.text
    finally:
        if owns_client:
            client.close()

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not {"Symbol", "Security"} <= set(reader.fieldnames):
        raise ValueError(f"S&P 500 CSV header lacks Symbol/Security: {reader.fieldnames!r}")
    rows: list[tuple[str, str]] = []
    for r in reader:
        symbol = (r["Symbol"] or "").strip()
        if not symbol:
            raise ValueError(f"S&P 500 CSV has a blank Symbol on row {r!r}")
        rows.append((symbol, (r["Security"] or "").strip()))
    if len(rows) < min_rows:
        raise ValueError(f"S&P 500 CSV has {len(rows)} rows, fewer than the {min_rows} expected")

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["ticker", "name"])
    writer.writerows(sorted(rows))
    _atomic_write(dest, out.getvalue().encode())
    return dest


def frozen_paths(directory: Path, day: date) -> tuple[Path, Path]:
    """(ticker map, S&P 500 CSV) snapshot paths for `day`. The CSV name is the contract's."""
    return (
        directory / f"company_tickers_{day.isoformat()}.json",
        directory / f"sp500_{day.isoformat()}.csv",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze the ticker map and S&P 500 snapshot.")
    parser.add_argument("--dest", type=Path, default=Path("data"))
    parser.add_argument("--date", type=date.fromisoformat, default=None)
    parser.add_argument("--refresh", action="store_true", help="overwrite existing snapshots")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    tickers_path, sp500_path = frozen_paths(args.dest, args.date or date.today())
    load_dotenv()  # SEC_USER_AGENT lives in .env; never overrides a variable already set

    try:
        if tickers_path.exists() and not args.refresh:
            log.info("%s exists; keeping the snapshot (use --refresh to replace it)", tickers_path)
        else:
            client = SecClient()
            try:
                fetch_company_tickers(client, tickers_path)
            finally:
                client.close()
            log.info("froze %s", tickers_path)

        if sp500_path.exists() and not args.refresh:
            log.info("%s exists; keeping the snapshot (use --refresh to replace it)", sp500_path)
        else:
            fetch_sp500(sp500_path)
            log.info("froze %s", sp500_path)
    except SecClientConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, SecRequestError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
