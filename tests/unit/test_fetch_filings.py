"""Unit tests for ``pipelines/fetch/fetch_filings.py`` (M02 section 3.2).

Every SEC request goes through a real ``SecClient`` over a ``respx`` mock (rules 4 and 9). Stores
are a temp directory and moto's S3. The output contract is ``raw.filings_html`` rows (contract
2.1) kept in a JSONL manifest next to the objects.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import socket
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
import respx
from moto import mock_aws

from pipelines.common.object_store import LocalStore, S3Store
from pipelines.fetch.fetch_filings import (
    MANIFEST_KEY,
    FetchRecord,
    fetch_all,
    gzip_deterministic,
    main,
    object_key,
    read_manifest,
)
from pipelines.fetch.list_filings import FilingTarget, write_targets
from pipelines.fetch.sec_client import SecClient, TokenBucket
from tests.fake_time import FakeTime

SEC_UA = "test-agent test@example.invalid"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def target(n: int, cik: int = 320193, form: str = "10-K") -> FilingTarget:
    accession = f"{cik:010d}-23-{n:06d}"
    doc = f"doc{n}.htm"
    return FilingTarget(
        cik=cik,
        accession=accession,
        form=form,
        filed=date(2023, 11, 3),
        report_date=date(2023, 9, 30),
        primary_doc=doc,
        url=f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}",
    )


def body(n: int) -> bytes:
    return f"<html><body>filing {n} ".encode() + b"x" * 500 + b"</body></html>"


def make_client(fake: FakeTime) -> SecClient:
    bucket = TokenBucket(rate=8, clock=fake.clock, sleep=fake.bucket_sleep)
    return SecClient(
        user_agent=SEC_UA, bucket=bucket, sleep=fake.backoff_sleep, rng=random.Random(1234)
    )


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a unit test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr("dotenv.main.find_dotenv", lambda *a, **k: "")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "raw" / "filings")


def mock_ok(router: respx.MockRouter, t: FilingTarget, n: int) -> respx.Route:
    return router.get(t.url).mock(return_value=httpx.Response(200, content=body(n)))


def run(
    targets: list[FilingTarget], store: Any, fake: FakeTime, **kw: Any
) -> tuple[list[FetchRecord], Any]:
    with make_client(fake) as client:
        return fetch_all(targets, store, client, now=lambda: NOW, **kw)


# --------------------------------------------------------------------------------------
# object layout and gzip
# --------------------------------------------------------------------------------------


def test_object_key_is_cik_slash_accession_with_htm_gz_suffix():
    assert object_key(320193, "0000320193-23-000106") == "320193/0000320193-23-000106.htm.gz"


def test_gzip_is_deterministic_so_a_refetch_writes_identical_bytes():
    assert gzip_deterministic(b"abc" * 100) == gzip_deterministic(b"abc" * 100)
    assert gzip.decompress(gzip_deterministic(b"abc" * 100)) == b"abc" * 100


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------


def test_fetch_stores_gzipped_html_and_returns_a_complete_record(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    mock_ok(router, t, 1)

    records, summary = run([t], store, fake)

    stored = store.get(object_key(t.cik, t.accession))
    assert gzip.decompress(stored) == body(1)
    [rec] = records
    assert rec == FetchRecord(
        accession=t.accession,
        cik=t.cik,
        form="10-K",
        filed=date(2023, 11, 3),
        url=t.url,
        s3_uri=store.uri(object_key(t.cik, t.accession)),
        bytes=len(stored),
        sha256=hashlib.sha256(stored).hexdigest(),
        http_status=200,
        fetched_at=NOW,
    )
    assert (summary.fetched, summary.skipped, summary.failed) == (1, 0, 0)


def test_requests_carry_the_sec_user_agent_from_the_client(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    route = mock_ok(router, t, 1)

    run([t], store, fake)

    assert route.calls.last.request.headers["User-Agent"] == SEC_UA


def test_the_manifest_is_written_as_sorted_jsonl_and_round_trips(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    ts = [target(2), target(1), target(3, cik=789019)]
    for i, t in enumerate(ts):
        mock_ok(router, t, i)

    run(ts, store, fake)

    lines = store.get(MANIFEST_KEY).decode().splitlines()
    assert [json.loads(line)["accession"] for line in lines] == sorted(t.accession for t in ts)
    assert {r.accession for r in read_manifest(store)} == {t.accession for t in ts}


def test_rerun_skips_everything_without_a_single_request(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    ts = [target(1), target(2)]
    routes = [mock_ok(router, t, i) for i, t in enumerate(ts)]
    run(ts, store, fake)
    first = store.get(MANIFEST_KEY)

    _, summary = run(ts, store, fake)

    assert [r.call_count for r in routes] == [1, 1]
    assert (summary.fetched, summary.skipped) == (0, 2)
    assert store.get(MANIFEST_KEY) == first


def test_object_without_a_manifest_row_is_fetched_again(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    route = mock_ok(router, t, 1)
    store.put(object_key(t.cik, t.accession), b"stale partial object")

    records, summary = run([t], store, fake)

    assert route.call_count == 1
    assert summary.fetched == 1
    assert gzip.decompress(store.get(object_key(t.cik, t.accession))) == body(1)
    assert records[0].http_status == 200


def test_manifest_row_whose_object_vanished_is_fetched_again(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore, tmp_path: Path
):
    t = target(1)
    route = mock_ok(router, t, 1)
    run([t], store, fake)
    (tmp_path / "raw" / "filings" / object_key(t.cik, t.accession)).unlink()

    _, summary = run([t], store, fake)

    assert route.call_count == 2
    assert summary.fetched == 1


def test_a_404_is_recorded_without_an_object_and_retried_on_the_next_run(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    route = router.get(t.url).mock(return_value=httpx.Response(404))

    records, summary = run([t], store, fake)

    [rec] = records
    assert (rec.http_status, rec.s3_uri, rec.bytes, rec.sha256) == (404, None, None, None)
    assert not store.exists(object_key(t.cik, t.accession))
    assert summary.failed == 1

    route.mock(return_value=httpx.Response(200, content=body(1)))
    records, summary = run([t], store, fake)
    assert records[0].http_status == 200 and summary.fetched == 1


def test_exhausted_retries_record_the_last_status(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    router.get(t.url).mock(return_value=httpx.Response(503))

    records, summary = run([t], store, fake)

    assert records[0].http_status == 503
    assert summary.failed == 1


def test_a_connection_failure_is_recorded_with_no_status(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    t = target(1)
    router.get(t.url).mock(side_effect=httpx.ConnectError("boom"))

    records, _ = run([t], store, fake)

    assert records[0].http_status is None
    assert records[0].s3_uri is None


def test_one_failure_does_not_stop_the_other_filings(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    good, bad = target(1), target(2)
    mock_ok(router, good, 1)
    router.get(bad.url).mock(return_value=httpx.Response(404))

    records, summary = run([bad, good], store, fake)

    assert sorted(r.http_status for r in records if r.http_status) == [200, 404]
    assert (summary.fetched, summary.failed) == (1, 1)


def test_limit_fetches_only_the_first_n_sorted_targets(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore
):
    ts = [target(3), target(1), target(2)]
    routes = {t.accession: mock_ok(router, t, i) for i, t in enumerate(ts)}

    run(ts, store, fake, limit=2)

    called = sorted(a for a, r in routes.items() if r.call_count)
    assert called == sorted(t.accession for t in ts)[:2]


def test_worker_count_does_not_change_the_result(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    ts = [target(i) for i in range(1, 9)]
    for i, t in enumerate(ts):
        mock_ok(router, t, i)
    one, many = LocalStore(tmp_path / "one"), LocalStore(tmp_path / "many")

    run(ts, one, fake, workers=1)
    run(ts, many, fake, workers=4)

    def stripped(st: LocalStore) -> list[dict[str, Any]]:  # s3_uri differs: it names the store
        return [r.model_dump(exclude={"s3_uri"}) for r in read_manifest(st)]

    assert stripped(one) == stripped(many)
    for t in ts:
        assert one.get(object_key(t.cik, t.accession)) == many.get(object_key(t.cik, t.accession))


def test_the_manifest_is_flushed_during_a_long_run(
    router: respx.MockRouter, fake: FakeTime, store: LocalStore, monkeypatch: pytest.MonkeyPatch
):
    ts = [target(i) for i in range(1, 6)]
    for i, t in enumerate(ts):
        mock_ok(router, t, i)
    writes: list[int] = []
    real_put = store.put

    def spy(key: str, data: bytes) -> None:
        if key == MANIFEST_KEY:
            writes.append(len(data.splitlines()))
        real_put(key, data)

    monkeypatch.setattr(store, "put", spy)

    run(ts, store, fake, flush_every=2, workers=1)

    assert writes[:2] == [2, 4] and writes[-1] == 5


def test_s3_destination_writes_objects_under_the_prefix_and_s3_uris(
    router: respx.MockRouter, fake: FakeTime
):
    t = target(1)
    mock_ok(router, t, 1)
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="lake")
        s3 = S3Store("lake", "raw/filings", client=client)

        records, _ = run([t], s3, fake)

        keys = sorted(o["Key"] for o in client.list_objects_v2(Bucket="lake")["Contents"])
        assert keys == [
            "raw/filings/320193/0000320193-23-000001.htm.gz",
            "raw/filings/_manifest.jsonl",
        ]
        assert records[0].s3_uri == "s3://lake/raw/filings/320193/0000320193-23-000001.htm.gz"


# --------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------


def run_main(argv: list[str], fake: FakeTime) -> int:
    with make_client(fake) as client:
        return main(argv, client=client, now=lambda: NOW)


def test_main_reads_the_csv_and_fetches_into_the_destination(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    ts = [target(1), target(2)]
    for i, t in enumerate(ts):
        mock_ok(router, t, i)
    csv_path = tmp_path / "filings.csv"
    write_targets(csv_path, ts)
    dest = tmp_path / "out"

    rc = run_main(["--targets", str(csv_path), "--dest", str(dest)], fake)

    assert rc == 0
    assert (dest / "320193" / "0000320193-23-000001.htm.gz").is_file()
    assert (dest / "_manifest.jsonl").is_file()


def test_main_returns_1_when_any_filing_failed_but_still_writes_the_manifest(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    good, bad = target(1), target(2)
    mock_ok(router, good, 1)
    router.get(bad.url).mock(return_value=httpx.Response(404))
    csv_path = tmp_path / "filings.csv"
    write_targets(csv_path, [good, bad])

    rc = run_main(["--targets", str(csv_path), "--dest", str(tmp_path / "out")], fake)

    assert rc == 1
    assert len((tmp_path / "out" / "_manifest.jsonl").read_text().splitlines()) == 2


def test_main_cik_filter_restricts_the_fetch(
    router: respx.MockRouter, fake: FakeTime, tmp_path: Path
):
    a, b = target(1), target(2, cik=789019)
    ra, rb = mock_ok(router, a, 1), mock_ok(router, b, 2)
    csv_path = tmp_path / "filings.csv"
    write_targets(csv_path, [a, b])

    run_main(["--targets", str(csv_path), "--dest", str(tmp_path / "o"), "--cik", "789019"], fake)

    assert (ra.call_count, rb.call_count) == (0, 1)


def test_main_with_a_missing_targets_file_is_an_error(fake: FakeTime, tmp_path: Path):
    assert run_main(["--targets", str(tmp_path / "nope.csv"), "--dest", str(tmp_path)], fake) == 2
