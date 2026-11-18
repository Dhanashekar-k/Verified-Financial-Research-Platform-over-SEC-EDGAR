"""Unit tests for ``pipelines/fetch/fetch_financebench.py`` (M02 section 3.2, FinanceBench part).

FinanceBench is hosted on GitHub, not SEC, so it uses a plain ``httpx`` client with the generic
User-Agent (never ``SEC_USER_AGENT``, which holds contact details). All HTTP is a respx mock.
The PDFs fetched are the ones the 150 questions cite (their ``doc_name``), unless ``--all-pdfs``.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pipelines.common.object_store import LocalStore
from pipelines.fetch.fetch_financebench import (
    JSONL_KEY,
    JSONL_URL,
    MANIFEST_KEY,
    PDF_URL,
    company_names,
    fetch_financebench,
    main,
    parse_questions,
)

PDF = b"%PDF-1.7\n" + b"x" * 300


def jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


QUESTIONS = [
    {"financebench_id": "fb-1", "company": "3M", "doc_name": "3M_2018_10K", "question": "q1"},
    {"financebench_id": "fb-2", "company": "3M", "doc_name": "3M_2018_10K", "question": "q2"},
    {"financebench_id": "fb-3", "company": "Nike", "doc_name": "NIKE_2022_10K", "question": "q3"},
]


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a unit test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "Real Person real.person@contact-details.invalid")


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as r:
        yield r


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "fb")


def mock_all(router: respx.MockRouter) -> dict[str, respx.Route]:
    router.get(JSONL_URL).mock(return_value=httpx.Response(200, content=jsonl(QUESTIONS)))
    return {
        d: router.get(PDF_URL.format(doc_name=d)).mock(
            return_value=httpx.Response(200, content=PDF)
        )
        for d in ("3M_2018_10K", "NIKE_2022_10K")
    }


def run(store: LocalStore, **kw: Any) -> Any:
    with httpx.Client() as http:
        return fetch_financebench(http, store, sleep=lambda s: None, **kw)


def test_parse_questions_keeps_company_and_doc_name_and_ignores_other_fields():
    qs = parse_questions(jsonl(QUESTIONS).decode())

    assert [(q.company, q.doc_name) for q in qs] == [
        ("3M", "3M_2018_10K"),
        ("3M", "3M_2018_10K"),
        ("Nike", "NIKE_2022_10K"),
    ]


def test_parse_questions_rejects_a_row_without_a_doc_name():
    with pytest.raises(ValueError, match="doc_name"):
        parse_questions('{"company": "3M"}\n')


def test_company_names_are_distinct_and_sorted():
    assert company_names(parse_questions(jsonl(QUESTIONS).decode())) == ["3M", "Nike"]


def test_fetch_downloads_the_jsonl_and_each_distinct_cited_pdf_once(
    router: respx.MockRouter, store: LocalStore
):
    routes = mock_all(router)

    result = run(store)

    assert [r.call_count for r in routes.values()] == [1, 1]
    assert store.get("pdfs/3M_2018_10K.pdf") == PDF
    assert store.get(JSONL_KEY) == jsonl(QUESTIONS)
    assert (result.fetched, result.skipped, result.failed) == (2, 0, 0)


def test_requests_use_the_generic_user_agent_never_the_sec_contact_one(
    router: respx.MockRouter, store: LocalStore
):
    routes = mock_all(router)

    run(store)

    for route in routes.values():
        ua = route.calls.last.request.headers["User-Agent"]
        assert "contact-details" not in ua and "edgar-verified-qa" in ua


def test_rerun_skips_stored_pdfs_with_no_request(router: respx.MockRouter, store: LocalStore):
    routes = mock_all(router)
    run(store)

    result = run(store)

    assert [r.call_count for r in routes.values()] == [1, 1]
    assert (result.fetched, result.skipped) == (0, 2)


def test_a_non_pdf_body_is_a_failure_and_stores_nothing(
    router: respx.MockRouter, store: LocalStore
):
    mock_all(router)
    router.get(PDF_URL.format(doc_name="NIKE_2022_10K")).mock(
        return_value=httpx.Response(200, content=b"<html>rate limited</html>")
    )

    result = run(store)

    assert result.failed == 1
    assert not store.exists("pdfs/NIKE_2022_10K.pdf")
    assert store.exists("pdfs/3M_2018_10K.pdf")


def test_a_404_is_recorded_in_the_manifest_and_the_rest_continue(
    router: respx.MockRouter, store: LocalStore
):
    mock_all(router)
    router.get(PDF_URL.format(doc_name="NIKE_2022_10K")).mock(return_value=httpx.Response(404))

    result = run(store)

    rows = [json.loads(line) for line in store.get(MANIFEST_KEY).decode().splitlines()]
    assert {r["doc_name"]: r["http_status"] for r in rows} == {
        "3M_2018_10K": 200,
        "NIKE_2022_10K": 404,
    }
    assert result.failed == 1


def test_transient_server_errors_are_retried(router: respx.MockRouter, store: LocalStore):
    mock_all(router)
    route = router.get(PDF_URL.format(doc_name="NIKE_2022_10K"))
    route.side_effect = [httpx.Response(503), httpx.Response(200, content=PDF)]

    result = run(store)

    assert route.call_count == 2
    assert result.failed == 0


def test_the_manifest_has_bytes_and_sha256_of_each_stored_pdf(
    router: respx.MockRouter, store: LocalStore
):
    mock_all(router)
    run(store)

    rows = {
        r["doc_name"]: r
        for r in (json.loads(line) for line in store.get(MANIFEST_KEY).decode().splitlines())
    }
    assert rows["3M_2018_10K"]["bytes"] == len(PDF)
    assert len(rows["3M_2018_10K"]["sha256"]) == 64


def test_main_prints_the_companies_the_owner_must_map_to_ciks(
    router: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    mock_all(router)

    rc = main(["--dest", str(tmp_path / "out")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "financebench_companies.csv" in out and "Nike" in out and "3M" in out
