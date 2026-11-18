"""List the 10-K and 10-Q filings to fetch (M02 section 3.1; contract section 1.3).

    uv run python -m pipelines.fetch.list_filings [--tracked data/targets/tracked_ciks.csv]
        [--cik 320193 ...] [--dest data/targets/filings.csv] [--from-date 2020-01-01]
        [--to-date YYYY-MM-DD] [--keep-going]

For every tracked CIK it reads the submissions JSON (`filings.recent` plus the older pages named
in `filings.files`), keeps forms 10-K, 10-K/A, 10-Q and 10-Q/A whose report date falls in the
requested window, and writes one row per filing with the primary-document URL.

* All requests go through `SecClient` (CLAUDE.md rule 9: User-Agent from `SEC_USER_AGENT`,
  <= 8 req/s, retries with backoff).
* The window tests `report_date`; a filing with no report date is tested on `filed` instead (and
  its `report_date` cell stays empty). Older pages whose `filingTo` is before `--from-date` are
  never requested.
* A filing with no `primaryDocument` cannot be fetched; it is counted and logged, not dropped
  silently.
* Idempotent: the output is sorted by (cik, filed, accession) and written atomically, so a rerun
  over unchanged submissions produces the same bytes. If any CIK cannot be listed nothing is
  written (a partial list must not replace a good one) unless `--keep-going` is given.
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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict

from pipelines.fetch.sec_client import SecClient, SecClientConfigError, SecRequestError

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{primary_doc}"

FILING_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
DEFAULT_FROM_DATE = date(2020, 1, 1)
DEFAULT_TRACKED = Path("data/targets/tracked_ciks.csv")
DEFAULT_DEST = Path("data/targets/filings.csv")
CSV_HEADER = ["cik", "accession", "form", "filed", "report_date", "primary_doc", "url"]
_COLUMNS = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")


class SubmissionsFormatError(ValueError):
    """The submissions JSON does not have the documented columnar shape."""


class FilingTarget(BaseModel):
    """One row of `filings.csv`."""

    model_config = ConfigDict(frozen=True)

    cik: int
    accession: str
    form: str
    filed: date
    report_date: date | None
    primary_doc: str
    url: str


@dataclass
class ListResult:
    targets: list[FilingTarget] = field(default_factory=list)
    no_primary_doc: set[str] = field(default_factory=set)  # accessions, so overlaps count once

    @property
    def skipped_no_primary_doc(self) -> int:
        return len(self.no_primary_doc)

    def extend(self, other: ListResult) -> None:
        self.targets.extend(other.targets)
        self.no_primary_doc |= other.no_primary_doc


def _parse_date(value: str, column: str, accession: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SubmissionsFormatError(
            f"{column} {value!r} of accession {accession} is not an ISO date"
        ) from exc


def parse_block(
    cik: int, block: Mapping[str, Any], *, from_date: date, to_date: date | None = None
) -> ListResult:
    """Turn one columnar block (`filings.recent` or an older page) into targets."""
    columns: dict[str, Sequence[Any]] = {}
    for name in _COLUMNS:
        values = block.get(name)
        if not isinstance(values, list):
            raise SubmissionsFormatError(f"submissions block for CIK {cik} lacks column {name}")
        columns[name] = values
    n = len(columns["accessionNumber"])
    for name, values in columns.items():
        if len(values) != n:
            raise SubmissionsFormatError(
                f"column {name} has {len(values)} entries, accessionNumber has {n} (CIK {cik})"
            )

    result = ListResult()
    for i in range(n):
        form = columns["form"][i]
        if form not in FILING_FORMS:
            continue
        accession = columns["accessionNumber"][i]
        filed = _parse_date(columns["filingDate"][i], "filingDate", accession)
        report_text = columns["reportDate"][i]
        report = _parse_date(report_text, "reportDate", accession) if report_text else None
        effective = report or filed
        if effective < from_date or (to_date is not None and effective > to_date):
            continue
        primary_doc = columns["primaryDocument"][i]
        if not primary_doc:
            log.warning("CIK %d accession %s has no primaryDocument; skipped", cik, accession)
            result.no_primary_doc.add(accession)
            continue
        result.targets.append(
            FilingTarget(
                cik=cik,
                accession=accession,
                form=form,
                filed=filed,
                report_date=report,
                primary_doc=primary_doc,
                url=ARCHIVE_URL.format(
                    cik=cik, accession_nodash=accession.replace("-", ""), primary_doc=primary_doc
                ),
            )
        )
    return result


def _page_in_range(entry: Mapping[str, Any], from_date: date) -> bool:
    """False only when the page provably holds no filing in the window.

    Only `filingTo < from_date` is a sound test: a report date is never later than its filing
    date. There is no matching test for `to_date`, because a report inside the window can be
    filed long after it, on a page that starts after `to_date`.
    """
    try:
        if entry.get("filingTo") and date.fromisoformat(entry["filingTo"]) < from_date:
            return False
    except ValueError:
        return True
    return True


def collect_cik(
    client: SecClient, cik: int, *, from_date: date, to_date: date | None = None
) -> ListResult:
    """All in-window filings of one CIK: the recent block plus every relevant older page."""
    main_doc = client.get_json(SUBMISSIONS_URL.format(cik=cik))
    filings = main_doc.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict):
        raise SubmissionsFormatError(f"CIK {cik}: no filings.recent in the submissions JSON")

    result = parse_block(cik, filings["recent"], from_date=from_date, to_date=to_date)
    for entry in filings.get("files") or []:
        if not _page_in_range(entry, from_date):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise SubmissionsFormatError(f"CIK {cik}: a filings.files entry has no name: {entry!r}")
        page = client.get_json(SUBMISSIONS_PAGE_URL.format(name=name))
        result.extend(parse_block(cik, page, from_date=from_date, to_date=to_date))

    unique = {t.accession: t for t in result.targets}  # recent and older pages may overlap
    result.targets = list(unique.values())
    return result


# -- output ------------------------------------------------------------------------------


def _sorted(targets: Iterable[FilingTarget]) -> list[FilingTarget]:
    return sorted(targets, key=lambda t: (t.cik, t.filed, t.accession))


def write_targets(dest: Path, targets: Iterable[FilingTarget]) -> None:
    """Write the CSV sorted and atomically (temp file in the same directory, then rename)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for t in _sorted(targets):
        writer.writerow(
            [
                t.cik,
                t.accession,
                t.form,
                t.filed.isoformat(),
                t.report_date.isoformat() if t.report_date else "",
                t.primary_doc,
                t.url,
            ]
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; the CSV is a shared input, not a secret
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def summarize(targets: Iterable[FilingTarget]) -> dict[tuple[str, int], int]:
    """Counts by (form, year of report_date, or of filed when there is none)."""
    counts: Counter[tuple[str, int]] = Counter()
    for t in targets:
        counts[(t.form, (t.report_date or t.filed).year)] += 1
    return dict(sorted(counts.items()))


def render_summary(counts: Mapping[tuple[str, int], int], *, n_ciks: int) -> str:
    forms = sorted({form for form, _ in counts})
    years = sorted({year for _, year in counts})
    lines = ["year  " + "".join(f"{f:>8}" for f in forms) + f"{'total':>8}"]
    for year in years:
        row = [counts.get((f, year), 0) for f in forms]
        lines.append(f"{year}  " + "".join(f"{v:>8}" for v in row) + f"{sum(row):>8}")
    totals = [sum(v for (f, _), v in counts.items() if f == form) for form in forms]
    lines.append("all   " + "".join(f"{v:>8}" for v in totals) + f"{sum(totals):>8}")
    lines.append(f"({n_ciks} CIKs listed)")
    return "\n".join(lines)


# -- command line ------------------------------------------------------------------------


def write_summary(
    path: Path,
    result: ListResult,
    counts: Mapping[tuple[str, int], int],
    ciks: Sequence[int],
    failed: Sequence[int],
    args: argparse.Namespace,
) -> None:
    """The counts as JSON: the one source the M02 report quotes for the target list."""
    summary = {
        "n_ciks_requested": len(ciks),
        "n_ciks_listed": len(ciks) - len(failed),
        "failed_ciks": list(failed),
        "n_targets": len(result.targets),
        "skipped_no_primary_doc": result.skipped_no_primary_doc,
        "from_date": args.from_date.isoformat(),
        "to_date": args.to_date.isoformat() if args.to_date else None,
        "by_form": dict(Counter(t.form for t in result.targets)),
        "by_form_and_year": {f"{form}|{year}": n for (form, year), n in counts.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_tracked(path: Path) -> list[int]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "cik" not in reader.fieldnames:
            raise ValueError(f"{path} has no cik column")
        return [int(row["cik"]) for row in reader]


def main(argv: list[str] | None = None, *, client: SecClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="List 10-K/10-Q filings to fetch.")
    parser.add_argument("--tracked", type=Path, default=DEFAULT_TRACKED)
    parser.add_argument("--cik", type=int, action="append", help="repeatable; replaces --tracked")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--from-date", type=date.fromisoformat, default=DEFAULT_FROM_DATE)
    parser.add_argument("--to-date", type=date.fromisoformat, default=None)
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--summary-out", type=Path, default=None, help="write the counts as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.to_date is not None and args.from_date > args.to_date:
        log.error("--from-date %s is after --to-date %s", args.from_date, args.to_date)
        return 2
    try:
        ciks = sorted(set(args.cik)) if args.cik else sorted(set(read_tracked(args.tracked)))
    except (OSError, ValueError) as exc:
        log.error("cannot read the CIK list: %s", exc)
        return 2
    if not ciks:
        log.error("no CIKs to list")
        return 2

    load_dotenv()
    owns_client = client is None
    try:
        sec = client or SecClient()
    except SecClientConfigError as exc:
        log.error("%s", exc)
        return 2

    result = ListResult()
    failed: list[int] = []
    try:
        for cik in ciks:
            try:
                result.extend(collect_cik(sec, cik, from_date=args.from_date, to_date=args.to_date))
            except (SecRequestError, SubmissionsFormatError, ValueError) as exc:
                log.error("CIK %d could not be listed: %s", cik, exc)
                failed.append(cik)
    finally:
        if owns_client:
            sec.close()

    if failed and not args.keep_going:
        log.error(
            "%d of %d CIKs failed (%s); nothing written. Rerun, or use --keep-going.",
            len(failed),
            len(ciks),
            ", ".join(str(c) for c in failed),
        )
        return 1

    write_targets(args.dest, result.targets)
    counts = summarize(result.targets)
    print(render_summary(counts, n_ciks=len(ciks) - len(failed)))
    if args.summary_out:
        write_summary(args.summary_out, result, counts, ciks, failed, args)
    if result.skipped_no_primary_doc:
        log.warning("%d filings skipped: no primaryDocument", result.skipped_no_primary_doc)
    if failed:
        log.warning("--keep-going: CIKs not listed: %s", ", ".join(str(c) for c in failed))
    log.info("wrote %d targets to %s", len(result.targets), args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
