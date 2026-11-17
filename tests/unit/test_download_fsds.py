"""Unit tests for ``pipelines/fetch/download_fsds.py`` (M01 section 3.2, contracts section 1.1).

Everything runs against ``respx`` mocks of the SEC ZIP URLs and synthetic in-memory ZIPs, so no
test touches the network (CLAUDE.md rule 4) and none really sleeps. All requests go through a
real ``SecClient`` (rule 9) built with an injected fake-clock ``TokenBucket`` and a no-op
backoff sleep; the unit under test is never mocked.

Error contract assumed here (the spec leaves the class name open): a ZIP that is corrupt, or
that lacks one of ``sub/num/tag/pre``, raises a ``ValueError`` (or subclass). HTTP failures
raise ``SecRequestError`` from the SEC client.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import io
import json
import random
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipelines.fetch import sec_client as sec_client_mod
from pipelines.fetch.download_fsds import (
    FIRST_QUARTER,
    FSDS_FILES,
    Quarter,
    QuarterResult,
    current_quarter,
    download_quarter,
    download_range,
    main,
    quarter_range,
)
from pipelines.fetch.sec_client import SecClient, SecRequestError, TokenBucket

UA = "Acme Research research-team@acme-research.org"
Q3 = Quarter(2023, 3)
Q4 = Quarter(2023, 4)
MANIFEST = "_manifest.json"
FSDS_URL_PREFIX = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/"

Members = dict[str, bytes]


# --------------------------------------------------------------------------------------
# Test doubles and builders
# --------------------------------------------------------------------------------------


class FakeTime:
    """Fake monotonic clock; sleeping just advances it, so nothing really waits."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_client(fake: FakeTime) -> SecClient:
    """A real SecClient with a private fake-clock bucket and a no-op backoff sleep."""
    bucket = TokenBucket(rate=8, clock=fake.clock, sleep=fake.sleep)
    return SecClient(user_agent=UA, bucket=bucket, sleep=lambda s: None, rng=random.Random(1234))


def make_members(variant: str = "v1") -> Members:
    """Synthetic ZIP members. Tabs, a double quote, non-ASCII UTF-8, multi-line and CRLF are
    all present so a lossy text-mode copy, newline translation or re-encoding would be caught.
    """
    return {
        "sub.txt": (
            "adsh\tcik\tname\tform\n"
            f'0000320193-23-000106\t320193\tApple "Inc." café € {variant}\t10-K\n'
            "0000789019-23-000050\t789019\tMicro\tsoft\n"
        ).encode(),
        "num.txt": (
            "adsh\ttag\tversion\tddate\tqtrs\tuom\tsegments\tcoreg\tvalue\tfootnote\n"
            f"0000320193-23-000106\tAssets\tus-gaap/2023\t20230930\t0\tUSD\t\t\t{variant}\t\n"
        ).encode(),
        "tag.txt": (b'tag\tversion\tcustom\nAssets\tus-gaap/2023\t0\t"quoted\n'),
        "pre.txt": b"adsh\treport\tline\r\n0000320193-23-000106\t2\t1\r\nno trailing newline",
        "readme.htm": f"<html><body>FSDS readme é {variant}</body></html>".encode(),
        # Present in real FSDS ZIPs and out of scope for this downloader: must be ignored.
        "ren.txt": b"ignored ren",
        "cal.txt": b"ignored cal",
    }


def build_zip(members: Mapping[str, bytes]) -> bytes:
    """An in-memory ZIP. Raw ``ZipInfo`` names allow hostile member names like ``../x``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            info = zipfile.ZipInfo(name, date_time=(2023, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return buf.getvalue()


def zip_without(*names: str, variant: str = "v1") -> bytes:
    return build_zip({k: v for k, v in make_members(variant).items() if k not in names})


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serve(router: respx.MockRouter, quarter: Quarter, body: bytes) -> respx.Route:
    return router.get(quarter.url).mock(return_value=httpx.Response(200, content=body))


def serve_404(router: respx.MockRouter, quarter: Quarter) -> respx.Route:
    return router.get(quarter.url).mock(return_value=httpx.Response(404))


def reserve(route: respx.Route, body: bytes) -> None:
    route.mock(return_value=httpx.Response(200, content=body))


def snapshot(root: Path) -> dict[str, bytes]:
    """Relative path -> bytes of every file under ``root`` ({} if it does not exist)."""
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def load_manifest(dest: Path) -> dict[str, Any]:
    data = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def assert_quarter_ok(
    dest: Path, quarter: Quarter, members: Members, zip_body: bytes, readme: bool = True
) -> None:
    """The full per-quarter contract: exact file set, byte-exact gunzip, matching manifest."""
    qdir = dest / str(quarter)
    expected_names = {f"{n}.txt.gz" for n in FSDS_FILES} | ({"readme.htm"} if readme else set())
    assert {p.name for p in qdir.iterdir()} == expected_names
    for name in FSDS_FILES:
        assert gzip.decompress((qdir / f"{name}.txt.gz").read_bytes()) == members[f"{name}.txt"]
    if readme:
        assert (qdir / "readme.htm").read_bytes() == members["readme.htm"]

    entry = load_manifest(dest)[str(quarter)]
    assert entry["source_url"] == quarter.url
    assert entry["zip_bytes"] == len(zip_body)
    assert entry["zip_sha256"] == sha256(zip_body)
    assert set(entry["files"]) == set(FSDS_FILES)
    for name in FSDS_FILES:
        on_disk = (qdir / f"{name}.txt.gz").read_bytes()
        assert entry["files"][name] == {"bytes": len(on_disk), "sha256": sha256(on_disk)}


# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    """Mock router that allows unused routes (skip tests expect zero calls) but fails any
    request to an unmocked URL, which is how "never requests 2009q1" is enforced."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def client() -> Iterator[SecClient]:
    c = make_client(FakeTime())
    yield c
    c.close()


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    """A destination that does not exist yet, nested so parents must be created too."""
    return tmp_path / "sandbox" / "out" / "fsds"


# --------------------------------------------------------------------------------------
# 1. Quarter
# --------------------------------------------------------------------------------------


def test_constants_match_spec():
    """FSDS_FILES and FIRST_QUARTER pin the contract: four tables, and 2009q1 (headers only)
    is excluded."""
    assert FSDS_FILES == ("sub", "num", "tag", "pre")
    assert FIRST_QUARTER == Quarter(2009, 2)


def test_quarter_str_is_lowercase_yyyyqn():
    """str(Quarter) is the directory name and manifest key, so its form is a contract."""
    assert str(Quarter(2023, 3)) == "2023q3"
    assert str(Quarter(2009, 2)) == "2009q2"


@pytest.mark.parametrize("text", ["2023q3", "2023Q3"])
def test_parse_accepts_either_case(text: str):
    """The CLI may be given 2023Q3; both spellings must give the same quarter."""
    assert Quarter.parse(text) == Quarter(2023, 3)


@pytest.mark.parametrize(
    "text",
    ["2023q5", "2023q0", "2023q10", "junk", "", "2023", "q3", "2023q", "2023qq3", "2023q3x",
     "2023q3.zip", "2023-q3", "q32023"],
)  # fmt: skip
def test_parse_rejects_malformed_text(text: str):
    """Anything that is not YYYYqN with N in 1..4 is a ValueError, never a silent guess."""
    with pytest.raises(ValueError):
        Quarter.parse(text)


def test_quarters_order_chronologically():
    """order=True: year dominates, then q (so 2024q1 is after 2023q4)."""
    shuffled = [Quarter(2024, 1), Quarter(2023, 4), Quarter(2009, 2), Quarter(2023, 3)]
    assert sorted(shuffled) == [
        Quarter(2009, 2),
        Quarter(2023, 3),
        Quarter(2023, 4),
        Quarter(2024, 1),
    ]
    assert Quarter(2023, 4) < Quarter(2024, 1)
    assert not Quarter(2023, 4) < Quarter(2023, 4)


def test_quarter_is_frozen_and_hashable():
    """Quarters are dict keys and set members; mutation must be impossible."""
    q = Quarter(2023, 3)
    assert {q, Quarter(2023, 3)} == {q}
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.year = 2024  # type: ignore[misc]


def test_next_within_year_and_across_year_boundary():
    """next() rolls q4 into the following year's q1."""
    assert Quarter(2023, 3).next() == Quarter(2023, 4)
    assert Quarter(2023, 4).next() == Quarter(2024, 1)
    assert Quarter(2009, 2).next() == Quarter(2009, 3)


def test_quarter_url():
    """The URL is the documented DERA path with the lowercase quarter name."""
    assert Quarter(2023, 3).url == (
        "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2023q3.zip"
    )


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2023, 1, 1), Quarter(2023, 1)),
        (date(2023, 3, 31), Quarter(2023, 1)),
        (date(2023, 4, 1), Quarter(2023, 2)),
        (date(2023, 6, 30), Quarter(2023, 2)),
        (date(2023, 7, 1), Quarter(2023, 3)),
        (date(2023, 9, 30), Quarter(2023, 3)),
        (date(2023, 10, 1), Quarter(2023, 4)),
        (date(2023, 12, 31), Quarter(2023, 4)),
        (date(2026, 11, 17), Quarter(2026, 4)),
    ],
)
def test_current_quarter_calendar_boundaries(today: date, expected: Quarter):
    """Calendar quarter containing ``today``; first and last day of each quarter are the
    boundaries where an off-by-one would show."""
    assert current_quarter(today) == expected


def test_quarter_range_2009q2_to_2010q1_has_four_items():
    """Spec example: the first FSDS year has exactly four quarters, inclusive both ends."""
    assert quarter_range(Quarter(2009, 2), Quarter(2010, 1)) == [
        Quarter(2009, 2),
        Quarter(2009, 3),
        Quarter(2009, 4),
        Quarter(2010, 1),
    ]


def test_quarter_range_single_quarter_is_inclusive():
    """start == end yields that one quarter (inclusive range)."""
    assert quarter_range(Q3, Q3) == [Q3]


def test_quarter_range_full_history_length():
    """2009q2..2026q3 is 70 quarters (matches the runbook's 'about 70 quarters')."""
    qs = quarter_range(FIRST_QUARTER, Quarter(2026, 3))
    assert len(qs) == 70
    assert qs[0] == FIRST_QUARTER
    assert qs[-1] == Quarter(2026, 3)


def test_quarter_range_rejects_start_after_end():
    """A reversed range is a caller bug, not an empty list."""
    with pytest.raises(ValueError):
        quarter_range(Q4, Q3)


@pytest.mark.parametrize("start", [Quarter(2009, 1), Quarter(2008, 4), Quarter(1999, 2)])
def test_quarter_range_rejects_start_before_first_quarter(start: Quarter):
    """2009q1 is headers only; no range may begin before 2009q2."""
    with pytest.raises(ValueError):
        quarter_range(start, Quarter(2010, 1))


# --------------------------------------------------------------------------------------
# 2. download_quarter: layout, content, manifest
# --------------------------------------------------------------------------------------


def test_download_quarter_writes_exact_layout_and_content(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """One ZIP becomes dest/{q}/{sub,num,tag,pre}.txt.gz (byte-exact when gunzipped) plus an
    uncompressed readme.htm. Extra members are ignored, and nothing else (no .zip, .part or
    temp file) is left anywhere under dest except the manifest."""
    members = make_members()
    body = build_zip(members)
    serve(router, Q3, body)

    result = download_quarter(client, Q3, dest)

    assert result == QuarterResult(quarter=Q3, status="downloaded")
    assert_quarter_ok(dest, Q3, members, body)
    assert set(snapshot(dest)) == {
        MANIFEST,
        *(f"2023q3/{n}.txt.gz" for n in FSDS_FILES),
        "2023q3/readme.htm",
    }


def test_download_quarter_without_readme_succeeds(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """readme.htm is optional in the ZIP: its absence is not an error and no empty
    readme.htm is invented."""
    members = {k: v for k, v in make_members().items() if k != "readme.htm"}
    body = build_zip(members)
    serve(router, Q3, body)

    assert download_quarter(client, Q3, dest).status == "downloaded"
    assert_quarter_ok(dest, Q3, members, body, readme=False)


def test_download_goes_through_sec_client_with_user_agent(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """CLAUDE.md rule 9: exactly one GET of the quarter URL, carrying the configured
    User-Agent (i.e. it used SecClient rather than its own HTTP)."""
    route = serve(router, Q3, build_zip(make_members()))

    download_quarter(client, Q3, dest)

    assert route.call_count == 1
    request = route.calls[0].request
    assert str(request.url) == Q3.url
    assert request.headers["User-Agent"] == UA


def test_gzip_output_is_deterministic_across_destinations(
    router: respx.MockRouter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Same ZIP, two dests, different wall-clock times: the .gz files are byte-identical and
    carry mtime 0 in the gzip header (so reruns never churn files or checksums)."""
    body = build_zip(make_members())
    serve(router, Q3, body)
    dest_a, dest_b = tmp_path / "a", tmp_path / "b"

    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    with make_client(FakeTime()) as c1:
        download_quarter(c1, Q3, dest_a)
    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    with make_client(FakeTime()) as c2:
        download_quarter(c2, Q3, dest_b)

    for name in FSDS_FILES:
        a = (dest_a / "2023q3" / f"{name}.txt.gz").read_bytes()
        b = (dest_b / "2023q3" / f"{name}.txt.gz").read_bytes()
        assert a == b, f"{name}.txt.gz differs between destinations"
        assert a[:2] == b"\x1f\x8b"
        assert int.from_bytes(a[4:8], "little") == 0, "gzip header mtime must be 0"
    assert load_manifest(dest_a)["2023q3"]["files"] == load_manifest(dest_b)["2023q3"]["files"]


def test_manifest_schema_and_sorted_keys(client: SecClient, router: respx.MockRouter, dest: Path):
    """The manifest is valid JSON with sorted keys at every level and exactly the documented
    fields; per-file bytes and sha256 describe the .gz as stored on disk."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, build_zip(make_members("v2")))
    download_quarter(client, Q4, dest)  # out of order on purpose: sorting must not depend on it
    download_quarter(client, Q3, dest)

    def assert_sorted(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), f"keys not sorted: {keys}"
        return dict(pairs)

    json.loads((dest / MANIFEST).read_text(encoding="utf-8"), object_pairs_hook=assert_sorted)
    manifest = load_manifest(dest)
    assert list(manifest) == ["2023q3", "2023q4"]
    entry = manifest["2023q3"]
    assert set(entry) == {"source_url", "zip_bytes", "zip_sha256", "downloaded_at", "files"}
    assert isinstance(entry["zip_bytes"], int)
    assert isinstance(entry["downloaded_at"], str)
    datetime.fromisoformat(entry["downloaded_at"])  # raises if not ISO 8601
    for name in FSDS_FILES:
        assert set(entry["files"][name]) == {"bytes", "sha256"}


def test_manifest_zip_checksum_matches_served_bytes(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """zip_bytes / zip_sha256 describe the ZIP that was served (provenance of the raw input),
    even though the ZIP itself is not kept."""
    body = build_zip(make_members("provenance"))
    serve(router, Q3, body)

    download_quarter(client, Q3, dest)

    entry = load_manifest(dest)["2023q3"]
    assert entry["zip_bytes"] == len(body)
    assert entry["zip_sha256"] == sha256(body)
    assert not [p for p in dest.rglob("*") if p.suffix in {".zip", ".part", ".tmp"}]


def test_second_quarter_preserves_first_quarters_manifest_entry(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """Downloading q4 adds its entry and leaves q3's entry byte-for-byte equal."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, build_zip(make_members("v2")))
    download_quarter(client, Q3, dest)
    q3_entry = load_manifest(dest)["2023q3"]

    download_quarter(client, Q4, dest)

    manifest = load_manifest(dest)
    assert set(manifest) == {"2023q3", "2023q4"}
    assert manifest["2023q3"] == q3_entry


def test_manifest_entries_for_unrelated_quarters_are_preserved(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A manifest from an earlier run (other quarters, possibly files not on this machine)
    must survive: the downloader only ever touches the entry of the quarter it processes."""
    dest.mkdir(parents=True)
    foreign = {"2020q1": {"source_url": "u", "zip_bytes": 1, "zip_sha256": "ab", "files": {}}}
    (dest / MANIFEST).write_text(json.dumps(foreign), encoding="utf-8")
    serve(router, Q3, build_zip(make_members()))

    download_quarter(client, Q3, dest)

    manifest = load_manifest(dest)
    assert manifest["2020q1"] == foreign["2020q1"]
    assert "2023q3" in manifest


def test_redownload_updates_only_that_quarters_entry(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """Re-running q3 (forced by a missing file) against a changed ZIP rewrites q3's entry and
    files, and leaves q4's entry and files untouched."""
    route3 = serve(router, Q3, build_zip(make_members("old")))
    serve(router, Q4, build_zip(make_members("q4")))
    download_quarter(client, Q3, dest)
    download_quarter(client, Q4, dest)
    before = load_manifest(dest)
    q4_files_before = snapshot(dest / "2023q4")
    (dest / "2023q3" / "num.txt.gz").unlink()

    new_members = make_members("new")
    new_body = build_zip(new_members)
    reserve(route3, new_body)
    assert download_quarter(client, Q3, dest).status == "downloaded"

    after = load_manifest(dest)
    assert after["2023q4"] == before["2023q4"]
    assert snapshot(dest / "2023q4") == q4_files_before
    assert after["2023q3"]["zip_sha256"] == sha256(new_body) != before["2023q3"]["zip_sha256"]
    assert_quarter_ok(dest, Q3, new_members, new_body)


# --------------------------------------------------------------------------------------
# 3. Idempotent skip and re-download triggers
# --------------------------------------------------------------------------------------


def test_rerun_skips_without_http_and_changes_no_bytes(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A complete, manifest-consistent quarter is 'skipped' with zero requests, and nothing
    on disk changes (including the manifest, so downloaded_at is not refreshed)."""
    route = serve(router, Q3, build_zip(make_members()))
    assert download_quarter(client, Q3, dest).status == "downloaded"
    before = snapshot(dest)
    route.reset()
    assert route.call_count == 0

    result = download_quarter(client, Q3, dest)

    assert result == QuarterResult(quarter=Q3, status="skipped")
    assert route.call_count == 0
    assert snapshot(dest) == before


def _edit_manifest(dest: Path, edit: Callable[[dict[str, Any]], Any]) -> None:
    manifest = load_manifest(dest)
    (dest / MANIFEST).write_text(json.dumps(edit(manifest) or manifest), encoding="utf-8")


def _append_byte(path: Path) -> None:
    path.write_bytes(path.read_bytes() + b"x")


def _truncate(path: Path) -> None:
    path.write_bytes(path.read_bytes()[:-5])


def _drop_files_key(m: dict[str, Any]) -> None:
    del m["2023q3"]["files"]["pre"]


def _bytes_not_int(m: dict[str, Any]) -> None:
    m["2023q3"]["files"]["sub"]["bytes"] = "not-a-number"


BREAKAGES: dict[str, Callable[[Path], None]] = {
    "sub_gz_missing": lambda d: (d / "2023q3" / "sub.txt.gz").unlink(),
    "num_gz_missing": lambda d: (d / "2023q3" / "num.txt.gz").unlink(),
    "tag_gz_missing": lambda d: (d / "2023q3" / "tag.txt.gz").unlink(),
    "pre_gz_missing": lambda d: (d / "2023q3" / "pre.txt.gz").unlink(),
    "sub_gz_grown": lambda d: _append_byte(d / "2023q3" / "sub.txt.gz"),
    "pre_gz_truncated": lambda d: _truncate(d / "2023q3" / "pre.txt.gz"),
    "manifest_deleted": lambda d: (d / MANIFEST).unlink(),
    "manifest_empty_object": lambda d: (d / MANIFEST).write_text("{}", encoding="utf-8"),
    "manifest_other_quarter_only": lambda d: _edit_manifest(
        d, lambda m: {"2020q1": m.pop("2023q3")}
    ),
    "manifest_corrupt_json": lambda d: (d / MANIFEST).write_text("{not json", encoding="utf-8"),
    "manifest_empty_file": lambda d: (d / MANIFEST).write_text("", encoding="utf-8"),
    "manifest_truncated_json": lambda d: (d / MANIFEST).write_text(
        (d / MANIFEST).read_text(encoding="utf-8")[:40], encoding="utf-8"
    ),
    "manifest_top_level_list": lambda d: (d / MANIFEST).write_text("[]", encoding="utf-8"),
    "manifest_entry_not_object": lambda d: _edit_manifest(d, lambda m: {"2023q3": "junk"}),
    "manifest_entry_empty": lambda d: _edit_manifest(d, lambda m: {"2023q3": {}}),
    "manifest_entry_missing_file_key": lambda d: _edit_manifest(d, _drop_files_key),
    "manifest_recorded_bytes_not_int": lambda d: _edit_manifest(d, _bytes_not_int),
}


@pytest.mark.parametrize("breakage", sorted(BREAKAGES))
def test_incomplete_or_inconsistent_state_triggers_redownload(
    breakage: str, client: SecClient, router: respx.MockRouter, dest: Path
):
    """Skip is allowed only when the manifest entry exists, is well formed, and all four .gz
    files exist with the recorded sizes. Any other state (a missing or resized file, a
    missing, corrupt or malformed manifest) re-downloads, never crashes, and ends in a fully
    consistent state identical to a clean run."""
    members = make_members()
    body = build_zip(members)
    route = serve(router, Q3, body)
    download_quarter(client, Q3, dest)
    good_quarter_files = snapshot(dest / "2023q3")
    BREAKAGES[breakage](dest)
    route.reset()

    result = download_quarter(client, Q3, dest)

    assert result == QuarterResult(quarter=Q3, status="downloaded")
    assert route.call_count == 1
    assert_quarter_ok(dest, Q3, members, body)
    assert snapshot(dest / "2023q3") == good_quarter_files


# --------------------------------------------------------------------------------------
# 4. Failure safety
# --------------------------------------------------------------------------------------


def _assert_nothing_for_quarter(dest: Path, quarter: Quarter) -> None:
    """No file left in the quarter directory, no stray temp file under dest, and no manifest
    entry for the quarter (the manifest itself may exist or not)."""
    assert snapshot(dest / str(quarter)) == {}
    assert set(snapshot(dest)) <= {MANIFEST}
    if (dest / MANIFEST).exists():
        assert str(quarter) not in load_manifest(dest)


@pytest.mark.parametrize("missing", FSDS_FILES)
def test_zip_missing_required_member_raises_and_writes_nothing(
    missing: str, client: SecClient, router: respx.MockRouter, dest: Path
):
    """A ZIP without sub/num/tag/pre.txt is a ValueError naming the problem, and the quarter
    leaves no partial files, no temp files and no manifest entry (never a half quarter that a
    later run would mistake for done)."""
    serve(router, Q3, zip_without(f"{missing}.txt"))

    with pytest.raises(ValueError, match=missing):
        download_quarter(client, Q3, dest)

    _assert_nothing_for_quarter(dest, Q3)


def test_zip_missing_all_required_members_raises(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A ZIP holding only ignorable members (readme, ren, cal) is rejected, not 'downloaded'."""
    serve(router, Q3, zip_without(*(f"{n}.txt" for n in FSDS_FILES)))

    with pytest.raises(ValueError):
        download_quarter(client, Q3, dest)

    _assert_nothing_for_quarter(dest, Q3)


_GOOD_ZIP = build_zip(make_members())


@pytest.mark.parametrize(
    "body",
    [
        b"this is definitely not a zip file",
        b"",
        b"<html>503 Service Unavailable page served with 200</html>",
        _GOOD_ZIP[: len(_GOOD_ZIP) // 2],
        _GOOD_ZIP[:-30],
    ],
    ids=["text", "empty", "html_200", "truncated_half", "truncated_directory"],
)
def test_corrupt_zip_body_raises_and_writes_nothing(
    body: bytes, client: SecClient, router: respx.MockRouter, dest: Path
):
    """A 200 whose body is not a readable ZIP (garbage, empty, an HTML error page, a
    truncated transfer) raises ValueError (not zipfile.BadZipFile) and leaves nothing."""
    serve(router, Q3, body)

    with pytest.raises(ValueError):
        download_quarter(client, Q3, dest)

    _assert_nothing_for_quarter(dest, Q3)


def test_failure_does_not_damage_an_existing_good_quarter(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """q4 fails (bad ZIP) after q3 succeeded: q3's files and manifest entry are untouched."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, zip_without("tag.txt"))
    download_quarter(client, Q3, dest)
    files_before = snapshot(dest / "2023q3")
    manifest_before = load_manifest(dest)

    with pytest.raises(ValueError):
        download_quarter(client, Q4, dest)

    assert snapshot(dest / "2023q3") == files_before
    assert load_manifest(dest) == manifest_before
    assert snapshot(dest / "2023q4") == {}
    assert set(snapshot(dest)) == {MANIFEST, *(f"2023q3/{p}" for p in files_before)}


def test_http_404_raises_sec_request_error_and_writes_nothing(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A 404 surfaces as SecRequestError(status 404) from download_quarter (the caller, not
    the downloader, decides whether that means 'not released'); nothing is written."""
    route = serve_404(router, Q3)

    with pytest.raises(SecRequestError) as excinfo:
        download_quarter(client, Q3, dest)

    assert excinfo.value.status_code == 404
    assert route.call_count == 1  # 404 is permanent: not retried
    _assert_nothing_for_quarter(dest, Q3)


def test_persistent_server_error_raises_and_writes_nothing(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A 503 that never clears exhausts the client's retries (fake sleep, so instant) and
    raises SecRequestError without leaving files behind."""
    router.get(Q3.url).mock(return_value=httpx.Response(503))

    with pytest.raises(SecRequestError) as excinfo:
        download_quarter(client, Q3, dest)

    assert excinfo.value.status_code == 503
    _assert_nothing_for_quarter(dest, Q3)


# --------------------------------------------------------------------------------------
# 5. Zip-slip
# --------------------------------------------------------------------------------------

HOSTILE_NAMES = [
    "../evil.txt",
    "../../evil.txt",
    "../../../evil.txt",
    "../../../../evil.txt",
    "/abs.txt",
    "../../../../../../../../tmp/edgarqa_zipslip_evil.txt",
    "2023q3/../../evil.txt",
]


def test_zip_slip_members_are_never_written_outside_quarter_dir(
    client: SecClient, router: respx.MockRouter, dest: Path, tmp_path: Path
):
    """Members named with ../ or an absolute path are ignored. Walking up from dest/{q}
    reaches every ancestor up to tmp_path, so the whole tree must contain only the expected
    quarter files and the manifest, and the real filesystem root and /tmp stay untouched."""
    members = make_members()
    hostile = {name: b"EVIL" for name in HOSTILE_NAMES}
    body = build_zip({**members, **hostile})
    serve(router, Q3, body)

    result = download_quarter(client, Q3, dest)

    assert result.status == "downloaded"
    assert_quarter_ok(dest, Q3, members, body)
    assert set(snapshot(tmp_path)) == {f"sandbox/out/fsds/{p}" for p in snapshot(dest)}
    assert not Path("/abs.txt").exists()
    assert not Path("/tmp/edgarqa_zipslip_evil.txt").exists()


def test_traversal_named_member_does_not_satisfy_a_required_member(
    client: SecClient, router: respx.MockRouter, dest: Path, tmp_path: Path
):
    """Only genuine top-level members count: a ZIP whose sole 'sub.txt' is '../sub.txt' is
    missing sub and is rejected, and nothing is written outside dest."""
    members = make_members()
    renamed = {("../sub.txt" if k == "sub.txt" else k): v for k, v in members.items()}
    serve(router, Q3, build_zip(renamed))

    with pytest.raises(ValueError):
        download_quarter(client, Q3, dest)

    _assert_nothing_for_quarter(dest, Q3)
    assert not (tmp_path / "sandbox" / "out" / "sub.txt").exists()


# --------------------------------------------------------------------------------------
# 6. download_range
# --------------------------------------------------------------------------------------


def _requested_urls(router: respx.MockRouter) -> list[str]:
    return [str(call.request.url) for call in router.calls]


def test_range_downloads_exactly_the_two_quarters_in_ascending_order(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """2023q3..2023q4 requests exactly those two URLs, in ascending order, and returns one
    result per quarter in the same order."""
    serve(router, Q4, build_zip(make_members("v2")))
    serve(router, Q3, build_zip(make_members()))

    results = download_range(client, Q3, Q4, dest)

    assert results == [QuarterResult(Q3, "downloaded"), QuarterResult(Q4, "downloaded")]
    assert _requested_urls(router) == [Q3.url, Q4.url]
    assert set(load_manifest(dest)) == {"2023q3", "2023q4"}


def test_range_rerun_skips_every_quarter_with_no_requests(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """Resuming a finished range is free: all 'skipped', zero HTTP calls."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, build_zip(make_members("v2")))
    download_range(client, Q3, Q4, dest)
    before = snapshot(dest)
    router.reset()

    results = download_range(client, Q3, Q4, dest)

    assert results == [QuarterResult(Q3, "skipped"), QuarterResult(Q4, "skipped")]
    assert len(router.calls) == 0
    assert snapshot(dest) == before


def test_range_mixes_skipped_and_downloaded(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A partially complete range only fetches what is missing."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, build_zip(make_members("v2")))
    download_quarter(client, Q3, dest)
    router.reset()

    results = download_range(client, Q3, Q4, dest)

    assert results == [QuarterResult(Q3, "skipped"), QuarterResult(Q4, "downloaded")]
    assert _requested_urls(router) == [Q4.url]


@pytest.mark.parametrize("start", [Quarter(2009, 1), Quarter(2008, 4)])
def test_range_start_before_first_quarter_raises_before_any_request(
    start: Quarter, client: SecClient, router: respx.MockRouter, dest: Path
):
    """2009q1 must never be requested. With no routes registered and assert_all_mocked, any
    request would blow up with a different error, so ValueError proves validation came first."""
    with pytest.raises(ValueError):
        download_range(client, start, Quarter(2009, 3), dest)

    assert len(router.calls) == 0
    assert snapshot(dest) == {}


def test_range_start_after_end_raises_before_any_request(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A reversed explicit range is a ValueError with no traffic."""
    with pytest.raises(ValueError):
        download_range(client, Q4, Q3, dest)

    assert len(router.calls) == 0


def test_latest_treats_trailing_404_as_not_released(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """today=2026-11-17 -> end is 2026q4; 2026q3 exists and 2026q4 is 404, which is reported
    as 'not_released' rather than raised, and leaves no files or manifest entry for it."""
    q3, q4 = Quarter(2026, 3), Quarter(2026, 4)
    members = make_members()
    body = build_zip(members)
    serve(router, q3, body)
    serve_404(router, q4)

    results = download_range(client, q3, "latest", dest, today=date(2026, 11, 17))

    assert results == [QuarterResult(q3, "downloaded"), QuarterResult(q4, "not_released")]
    assert_quarter_ok(dest, q3, members, body)
    assert snapshot(dest / "2026q4") == {}
    assert "2026q4" not in load_manifest(dest)


def test_latest_with_several_trailing_404s(client: SecClient, router: respx.MockRouter, dest: Path):
    """Every trailing 404 is 'not_released': 2026q2 and q3 exist; q4 and 2027q1 do not
    (today=2027-01-05)."""
    q2, q3, q4, q1_27 = Quarter(2026, 2), Quarter(2026, 3), Quarter(2026, 4), Quarter(2027, 1)
    serve(router, q2, build_zip(make_members("a")))
    serve(router, q3, build_zip(make_members("b")))
    serve_404(router, q4)
    serve_404(router, q1_27)

    results = download_range(client, q2, "latest", dest, today=date(2027, 1, 5))

    assert results == [
        QuarterResult(q2, "downloaded"),
        QuarterResult(q3, "downloaded"),
        QuarterResult(q4, "not_released"),
        QuarterResult(q1_27, "not_released"),
    ]


def test_latest_when_current_quarter_is_available(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """If the end quarter exists, 'latest' just downloads it; it never asks beyond
    current_quarter(today). Here today is in 2026q3, so 2026q4 is not even requested (it is
    unmocked and would raise)."""
    q2, q3 = Quarter(2026, 2), Quarter(2026, 3)
    serve(router, q2, build_zip(make_members("a")))
    serve(router, q3, build_zip(make_members("b")))

    results = download_range(client, q2, "latest", dest, today=date(2026, 9, 30))

    assert results == [QuarterResult(q2, "downloaded"), QuarterResult(q3, "downloaded")]
    assert _requested_urls(router) == [q2.url, q3.url]


def test_latest_all_quarters_unreleased(client: SecClient, router: respx.MockRouter, dest: Path):
    """If even the first requested quarter is a 404, there is nothing after it that succeeded,
    so it is trailing: 'not_released', not an error."""
    q4 = Quarter(2026, 4)
    serve_404(router, q4)

    results = download_range(client, q4, "latest", dest, today=date(2026, 11, 17))

    assert results == [QuarterResult(q4, "not_released")]


def test_latest_404_in_the_middle_is_an_error(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A gap (2026q3 is 404 but 2026q4 downloads fine) is corruption of the range, not an
    unreleased quarter, and must raise."""
    q2, q3, q4 = Quarter(2026, 2), Quarter(2026, 3), Quarter(2026, 4)
    serve(router, q2, build_zip(make_members("a")))
    serve_404(router, q3)
    serve(router, q4, build_zip(make_members("c")))

    with pytest.raises(SecRequestError) as excinfo:
        download_range(client, q2, "latest", dest, today=date(2026, 11, 17))

    assert excinfo.value.status_code == 404
    assert q3.url in excinfo.value.url


def test_explicit_end_404_at_end_raises(client: SecClient, router: respx.MockRouter, dest: Path):
    """Only 'latest' tolerates unreleased quarters; asking explicitly for 2026q4 and getting
    404 is an error."""
    q3, q4 = Quarter(2026, 3), Quarter(2026, 4)
    serve(router, q3, build_zip(make_members()))
    serve_404(router, q4)

    with pytest.raises(SecRequestError) as excinfo:
        download_range(client, q3, q4, dest, today=date(2026, 11, 17))

    assert excinfo.value.status_code == 404


def test_explicit_end_404_in_the_middle_raises(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """An explicit range with a 404 for its first quarter raises too."""
    serve_404(router, Q3)
    serve(router, Q4, build_zip(make_members()))

    with pytest.raises(SecRequestError):
        download_range(client, Q3, Q4, dest)


def test_range_bad_zip_propagates_as_value_error(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A corrupt quarter inside a range is not swallowed or reported as 'not_released'."""
    serve(router, Q3, b"garbage")

    with pytest.raises(ValueError):
        download_range(client, Q3, Q4, dest)


# --------------------------------------------------------------------------------------
# 7. main (CLI)
# --------------------------------------------------------------------------------------


@pytest.fixture
def cli_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> FakeTime:
    """Hermetic CLI environment: SEC_USER_AGENT set, cwd in an empty tmp dir, the developer's
    real .env neither read nor needed, and the process-wide SEC bucket replaced by a
    fake-clock one so ``main`` (which builds its own SecClient) never really waits."""
    fake = FakeTime()
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr("dotenv.main.find_dotenv", lambda *a, **k: "")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sec_client_mod,
        "_shared_bucket",
        TokenBucket(rate=8, clock=fake.clock, sleep=fake.sleep),
    )
    return fake


def run_main(argv: list[str]) -> int:
    """Exit status of main(argv), whether it returns or exits (argparse raises SystemExit)."""
    try:
        status = main(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    return status


def _all_output(capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> str:
    captured = capsys.readouterr()
    return f"{captured.out}\n{captured.err}\n{caplog.text}"


def test_main_downloads_one_quarter_and_returns_zero(
    cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """End to end through the CLI: one quarter is fetched with the User-Agent from
    SEC_USER_AGENT, files and manifest are written, exit status 0."""
    members = make_members()
    body = build_zip(members)
    route = serve(router, Q3, body)
    out = tmp_path / "raw" / "fsds"

    status = run_main(["--from", "2023q3", "--to", "2023q3", "--dest", str(out)])

    assert status == 0
    assert route.call_count == 1
    assert route.calls[0].request.headers["User-Agent"] == UA
    assert_quarter_ok(out, Q3, members, body)


def test_main_accepts_uppercase_quarters(
    cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """The CLI accepts 2023Q3 as well as 2023q3 (Quarter.parse is case-insensitive)."""
    serve(router, Q3, build_zip(make_members()))
    out = tmp_path / "fsds"

    assert run_main(["--from", "2023Q3", "--to", "2023Q3", "--dest", str(out)]) == 0
    assert (out / "2023q3" / "sub.txt.gz").exists()


def test_main_rerun_is_a_no_op(cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path):
    """Running the same command twice: both exit 0, the second makes no request and changes
    no byte on disk."""
    route = serve(router, Q3, build_zip(make_members()))
    out = tmp_path / "fsds"
    argv = ["--from", "2023q3", "--to", "2023q3", "--dest", str(out)]
    assert run_main(argv) == 0
    before = snapshot(out)
    route.reset()

    assert run_main(argv) == 0

    assert route.call_count == 0
    assert snapshot(out) == before


def test_main_missing_user_agent_fails_without_any_request(
    cli_env: FakeTime,
    router: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    """No SEC_USER_AGENT: the SEC blocks anonymous clients, so main must refuse before
    sending anything, exit nonzero and say which variable is missing."""
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    route = serve(router, Q3, build_zip(make_members()))
    out = tmp_path / "fsds"

    status = run_main(["--from", "2023q3", "--to", "2023q3", "--dest", str(out)])

    assert status != 0
    assert route.call_count == 0
    assert snapshot(out) == {}
    assert "SEC_USER_AGENT" in _all_output(capsys, caplog)


@pytest.mark.parametrize(
    "argv",
    [
        ["--from", "2023q5", "--to", "2023q5"],
        ["--from", "junk", "--to", "2023q3"],
        ["--from", "2023q3", "--to", "junk"],
        ["--from", "2023q3", "--to", "2023q0"],
    ],
    ids=["q5", "from_junk", "to_junk", "q0"],
)
def test_main_bad_quarter_argument_is_nonzero_with_no_request(
    argv: list[str], cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """Invalid quarter text is an argument error (argparse exit 2 or a nonzero return) and
    nothing is fetched."""
    route = serve(router, Q3, build_zip(make_members()))

    status = run_main([*argv, "--dest", str(tmp_path / "fsds")])

    assert status != 0
    assert route.call_count == 0
    assert len(router.calls) == 0


def test_main_start_before_2009q2_is_nonzero_with_no_request(
    cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """--from 2009q1 (headers-only data set) is a failure exit, never a request. Unmocked URLs
    would raise, so a clean nonzero status proves the range was rejected first."""
    status = run_main(["--from", "2009q1", "--to", "2009q3", "--dest", str(tmp_path / "fsds")])

    assert status != 0
    assert len(router.calls) == 0


def test_main_http_404_with_explicit_end_is_nonzero(
    cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """A missing quarter requested explicitly is a failed run, reported by exit status."""
    serve_404(router, Q3)

    status = run_main(["--from", "2023q3", "--to", "2023q3", "--dest", str(tmp_path / "fsds")])

    assert status != 0


def test_main_corrupt_zip_is_nonzero(cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path):
    """A bad ZIP fails the run (nonzero) and leaves no quarter content."""
    serve(router, Q3, b"not a zip")
    out = tmp_path / "fsds"

    status = run_main(["--from", "2023q3", "--to", "2023q3", "--dest", str(out)])

    assert status != 0
    assert snapshot(out / "2023q3") == {}


def test_main_s3_destination_is_rejected_with_clear_message(
    cli_env: FakeTime,
    router: respx.MockRouter,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
):
    """--dest s3://... is not implemented yet: exit nonzero, say so plainly, make no request,
    and do not fall through to creating a local directory literally named 's3:'."""
    route = serve(router, Q3, build_zip(make_members()))

    status = run_main(["--from", "2023q3", "--to", "2023q3", "--dest", "s3://my-lake/raw/fsds/"])

    text = _all_output(capsys, caplog).lower()
    assert status != 0
    assert route.call_count == 0
    assert "s3" in text
    assert any(w in text for w in ("not supported", "unsupported", "not yet", "not implemented"))
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------------------
# 8. Review hardening: manifest safety, concurrent writers, progress, `latest` via main
# --------------------------------------------------------------------------------------


def test_corrupt_manifest_is_preserved_aside_and_other_quarters_files_survive(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A corrupt manifest must never be silently overwritten: keep the bad file for inspection,
    leave every other quarter's data files alone, and write a valid manifest for what we did."""
    body3, body4 = build_zip(make_members("a")), build_zip(make_members("b"))
    serve(router, Q3, body3)
    route4 = serve(router, Q4, body4)
    download_range(client, Q3, Q4, dest)
    q3_before = snapshot(dest / str(Q3))
    corrupt = b'{"2023q3": {"files": '  # truncated JSON
    (dest / MANIFEST).write_bytes(corrupt)
    route4.reset()
    reserve(route4, body4)

    download_quarter(client, Q4, dest)

    asides = [p for p in dest.iterdir() if p.name.startswith(MANIFEST + ".corrupt-")]
    assert len(asides) == 1
    assert asides[0].read_bytes() == corrupt
    assert snapshot(dest / str(Q3)) == q3_before  # other quarter's files untouched
    manifest = load_manifest(dest)  # a valid manifest again
    assert str(Q4) in manifest


def test_unreadable_manifest_aborts_before_any_file_is_written(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """An I/O error reading the manifest (not just bad JSON) must not lead to an overwrite."""
    serve(router, Q3, build_zip(make_members()))
    dest.mkdir(parents=True)
    (dest / MANIFEST).mkdir()  # reading a directory as a file raises OSError

    with pytest.raises(OSError):
        download_quarter(client, Q3, dest)

    assert (dest / MANIFEST).is_dir()
    assert not (dest / str(Q3)).exists()


def test_concurrent_writers_on_one_dest_keep_every_manifest_entry(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """Overlapping runs sharing a --dest must not drop each other's manifest entries."""
    quarters = [Quarter(2023, 1), Quarter(2023, 2), Q3, Q4]
    for i, q in enumerate(quarters):
        serve(router, q, build_zip(make_members(f"v{i}")))
    errors: list[BaseException] = []

    def work(q: Quarter) -> None:
        try:
            download_quarter(client, q, dest)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(q,)) for q in quarters]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert set(load_manifest(dest)) == {str(q) for q in quarters}


def test_download_range_reports_each_result_as_it_happens(
    client: SecClient, router: respx.MockRouter, dest: Path
):
    """A progress callback sees finished quarters even if a later one fails."""
    serve(router, Q3, build_zip(make_members()))
    serve(router, Q4, b"not a zip at all")
    seen: list[QuarterResult] = []

    with pytest.raises(ValueError):
        download_range(client, Q3, Q4, dest, progress=seen.append)

    assert [(r.quarter, r.status) for r in seen] == [(Q3, "downloaded")]


def test_main_latest_end_reports_not_released_and_exits_zero(
    cli_env: FakeTime,
    router: respx.MockRouter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """`--to latest` through the CLI: the unreleased trailing quarter is not an error, and each
    quarter's status is printed."""

    class FixedDate(date):
        @classmethod
        def today(cls) -> FixedDate:
            return cls(2026, 11, 17)

    monkeypatch.setattr("pipelines.fetch.download_fsds.date", FixedDate)
    q26_3, q26_4 = Quarter(2026, 3), Quarter(2026, 4)
    serve(router, q26_3, build_zip(make_members()))
    serve_404(router, q26_4)
    out = tmp_path / "fsds"

    status = run_main(["--from", "2026q3", "--to", "latest", "--dest", str(out)])

    assert status == 0
    printed = capsys.readouterr().out
    assert "2026q3: downloaded" in printed
    assert "2026q4: not_released" in printed


def test_main_failure_midway_still_prints_finished_quarters(
    cli_env: FakeTime,
    router: respx.MockRouter,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    serve(router, Q3, build_zip(make_members()))
    serve_404(router, Q4)

    status = run_main(["--from", "2023q3", "--to", "2023q4", "--dest", str(tmp_path / "f")])

    assert status == 1
    assert "2023q3: downloaded" in capsys.readouterr().out


def test_main_range_errors_use_the_usage_exit_code(
    cli_env: FakeTime, router: respx.MockRouter, tmp_path: Path
):
    """`--from` after `--to` is a usage error (2), the same class as a malformed quarter."""
    assert run_main(["--from", "2023q4", "--to", "2023q3", "--dest", str(tmp_path / "f")]) == 2
