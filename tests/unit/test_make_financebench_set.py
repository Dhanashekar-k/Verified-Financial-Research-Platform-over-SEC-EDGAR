"""eval/gen/make_financebench_set.py on 3 synthetic rows and a tiny chunks parquet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from iceberg_testing import add_stale_file, make_chunks_table

from eval.gen import make_financebench_set as gen
from eval.schemas import FinanceBenchRow


def _record(fid: str, doc: str, evidence: object) -> dict[str, object]:
    return {
        "financebench_id": fid,
        "company": "3M",
        "doc_name": doc,
        "question_type": "metrics-generated",
        "question_reasoning": "Information extraction",
        "question": f"q {fid}",
        "answer": "$1577.00",
        "justification": "x",
        "evidence": evidence,
    }


RECORDS = [
    # a list (what the HF file holds)
    _record("financebench_id_03029", "DOC_A", [{"evidence_text": "t1", "evidence_page_num": 0}]),
    # a JSON string
    _record(
        "financebench_id_00807",
        "DOC_A",
        json.dumps([{"evidence_text": "t2", "evidence_page_num": 4}]),
    ),
    # a Python-literal string (single quotes), two evidence items, one on a page without chunks
    _record(
        "financebench_id_7",
        "DOC_B",
        str(
            [
                {"evidence_text": "t3", "evidence_page_num": 1},
                {"evidence_text": "t4", "evidence_page_num": 8},
            ]
        ),
    ),
]


def _rows(docs: dict[str, list[int]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for doc, page_list in docs.items():
        for page in page_list:
            for idx in range(2):
                rows.append(
                    {
                        "chunk_id": f"fb:{doc}#p{page}#{idx:04d}",
                        "page": page,
                        "doc_source": "financebench_pdf",
                        "text": "t",
                        "n_tokens": 50,
                    }
                )
    rows.append(
        {
            "chunk_id": "0000320193-23-000106#7#0001",
            "page": None,
            "doc_source": "edgar_html",
            "text": "t",
            "n_tokens": 50,
        }
    )
    return rows


def _warehouse(tmp_path: Path, docs: dict[str, list[int]]) -> Path:
    return make_chunks_table(tmp_path, _rows(docs))


def test_convert_ids_pages_and_evidence_formats() -> None:
    rows = [gen.convert(r) for r in RECORDS]
    assert [r.id for r in rows] == ["fb-03029", "fb-00807", "fb-00007"]
    assert [e.page for e in rows[0].evidence] == [1]  # 0-based + 1
    assert [e.page for e in rows[1].evidence] == [5]
    assert [(e.page, e.text) for e in rows[2].evidence] == [(2, "t3"), (9, "t4")]
    assert all(isinstance(r, FinanceBenchRow) for r in rows)
    assert rows[0].reasoning == "Information extraction"


def test_parse_evidence_rejects_non_lists() -> None:
    with pytest.raises(ValueError):
        gen.parse_evidence("{'a': 1}")


def test_build_maps_evidence_pages_to_chunk_ids(tmp_path: Path) -> None:
    pages = gen.load_chunk_pages(_warehouse(tmp_path, {"DOC_A": [1, 5], "DOC_B": [2]}))
    rows, gold, summary = gen.build(RECORDS, pages)
    assert gold["fb-03029"] == ["fb:DOC_A#p1#0000", "fb:DOC_A#p1#0001"]
    assert gold["fb-00807"] == ["fb:DOC_A#p5#0000", "fb:DOC_A#p5#0001"]
    assert gold["fb-00007"] == ["fb:DOC_B#p2#0000", "fb:DOC_B#p2#0001"]
    assert summary["evidence_pages_without_chunks"] == [
        {"id": "fb-00007", "doc_name": "DOC_B", "page": 9}
    ]
    assert summary["documents_without_chunks"] == []
    assert summary["evidence_items"] == 4


def test_main_writes_drafts_only(tmp_path: Path) -> None:
    src = tmp_path / "fb.jsonl"
    src.write_text("".join(json.dumps(r) + "\n" for r in RECORDS))
    wh = _warehouse(tmp_path, {"DOC_A": [1, 5], "DOC_B": [2]})
    out = tmp_path / "drafts"
    assert (
        gen.main(["--source", str(src), "--warehouse-chunks", str(wh), "--out-dir", str(out)]) == 0
    )
    lines = (out / gen.OUT_ROWS).read_text().splitlines()
    assert len(lines) == 3
    sidecar = json.loads((out / gen.OUT_CHUNKS).read_text())
    assert sidecar["summary"]["source"]["iceberg_metadata"] == "v9.metadata.json"
    assert isinstance(sidecar["summary"]["source"]["iceberg_snapshot_id"], int)
    assert "cross-check" in sidecar["summary"]["source"]["caveat"]


def test_main_fails_when_a_document_has_no_chunks(tmp_path: Path) -> None:
    src = tmp_path / "fb.jsonl"
    src.write_text("".join(json.dumps(r) + "\n" for r in RECORDS))
    wh = _warehouse(tmp_path, {"DOC_A": [1, 5]})
    with pytest.raises(gen.MissingDocumentsError, match="DOC_B"):
        gen.main(["--source", str(src), "--warehouse-chunks", str(wh), "--out-dir", str(tmp_path)])


def test_main_refuses_gold_dir() -> None:
    with pytest.raises(SystemExit, match="refusing"):
        gen.main(["--out-dir", "eval/sets"])


def test_stale_files_outside_the_current_snapshot_are_ignored(tmp_path: Path) -> None:
    """A file an older snapshot left in data/ must not add or duplicate gold chunk ids."""
    wh = _warehouse(tmp_path, {"DOC_A": [1, 5], "DOC_B": [2]})
    stale = _rows({"DOC_A": [1, 7]})  # duplicates p1 ids and adds a p7 that isn't current
    add_stale_file(wh, stale)
    pages = gen.load_chunk_pages(wh)
    assert pages["DOC_A"][1] == ["fb:DOC_A#p1#0000", "fb:DOC_A#p1#0001"]
    assert 7 not in pages["DOC_A"]
