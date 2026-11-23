"""Pure parts of the box-session tools: FB chunk check, narrative sampling/drafting, unanswerable
template, FSDS labels. No database or network: the LLM boundary is a fake batch SDK."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm.client import LLMClient
from app.llm.types import LLMMode
from eval.gen import check_fb_chunks as fb
from eval.gen import draft_narrative as dn


def test_fb_compare_reports_missing_ids_and_lost_pages() -> None:
    gold = {
        "fb-1": ["fb:A_2020_10K#p3#0000", "fb:A_2020_10K#p3#0001"],
        "fb-2": ["fb:B_2021_10K#p9#0000"],
    }
    served = {"fb:A_2020_10K#p3#0001"}
    r = fb.compare(gold, served)
    assert r["gold_ids"] == 3 and r["served"] == 1 and r["missing"] == 2
    # fb-1 keeps one served chunk on page 3 (page hit@k unaffected); fb-2 lost its page
    assert r["rows_with_an_unserved_evidence_page"] == {"fb-2": ["B_2021_10K#p9"]}


def test_fb_page_key_rejects_non_fb_ids() -> None:
    with pytest.raises(ValueError):
        fb.page_key("0000320193-23-000106#7#0012")


@pytest.mark.parametrize(
    ("sic", "div"),
    [
        (100, "agriculture"),
        (1311, "mining"),
        (3571, "manufacturing"),
        (6022, "finance"),
        (7372, "services"),
        (9721, "public_admin"),
        (None, "unknown"),
    ],
)
def test_sic_division(sic: int | None, div: str) -> None:
    assert dn.sic_division(sic) == div


def _cands() -> list[dn.Candidate]:
    out = []
    for i in range(200):
        sic = [3571, 6022, 7372, 1311][i % 4]
        out.append(
            dn.Candidate(
                chunk_id=f"acc{i}#{['1A', '7', '7A'][i % 3]}#{i:04d}",
                cik=1000 + i % 37,
                fiscal_year=2020 + i % 5,
                section=["1A", "7", "7A"][i % 3],
                sic=sic,
                company=f"Co{i % 37}",
                accession=f"acc{i}",
                chunk_index=i,
            )
        )
    return out


def test_stratified_sample_is_deterministic_balanced_and_sized() -> None:
    a = dn.stratified_sample(_cands(), 24)
    b = dn.stratified_sample(list(reversed(_cands())), 24)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]  # input order doesn't matter
    assert len(a) == 24
    strata: dict[tuple[str, str], int] = {}
    for c in a:
        k = (dn.sic_division(c.sic), c.section)
        strata[k] = strata.get(k, 0) + 1
    assert len(strata) == 12 and set(strata.values()) == {2}
    # one company per stratum
    for k in strata:
        ciks = [c.cik for c in a if (dn.sic_division(c.sic), c.section) == k]
        assert len(ciks) == len(set(ciks))


def test_unanswerable_template_has_five_categories_of_ten() -> None:
    rows = dn.unanswerable_template()
    assert len(rows) == 50 and len({r["id"] for r in rows}) == 50
    for cat in dn.UNANSWERABLE_CATEGORIES:
        assert sum(r["reason"] == cat for r in rows) == 10
    assert all(r["question"] == "" and r["expected"] == "refuse" for r in rows)


def test_estimate_batch_is_half_of_sync() -> None:
    msgs = ["x" * 4000] * 10
    assert dn.estimate_usd(msgs, batch=True) * 2 == dn.estimate_usd(msgs, batch=False)


class _Batches:
    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []

    def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
        self.created.append(requests)
        return SimpleNamespace(id="b1")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id: str) -> list[SimpleNamespace]:
        out = []
        for r in self.created[-1]:
            ids = [
                ln.split("=", 1)[1].split("\n")[0]
                for ln in r["params"]["messages"][0]["content"].split("chunk_id")[1:]
            ]
            draft = dn.NarrativeDraft(
                question="What supply risks did Co disclose for FY2021?",
                gold_points=["single supplier", "tariffs"],
                gold_chunk_ids=ids[:1],
                uses_second_chunk=False,
                usable=True,
                note="",
            )
            msg = SimpleNamespace(
                content=[SimpleNamespace(type="text", text=draft.model_dump_json())],
                usage=SimpleNamespace(
                    input_tokens=1500,
                    output_tokens=200,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
                stop_reason="end_turn",
            )
            out.append(
                SimpleNamespace(
                    custom_id=r["custom_id"], result=SimpleNamespace(type="succeeded", message=msg)
                )
            )
        return out


def test_draft_all_runs_one_batch_and_counts_cost_once(tmp_path: Path) -> None:
    batches = _Batches()
    client = LLMClient(
        mode=LLMMode.batch,
        cassette_dir=tmp_path,
        sdk_client=SimpleNamespace(messages=SimpleNamespace(batches=batches)),
        registry={"composer": dn.DRAFT_MODEL},
        sleep=lambda s: None,
    )
    picked = dn.stratified_sample(_cands(), 6)
    passages = {
        c.chunk_id: [(c.chunk_id, "text one"), (c.chunk_id + "x", "text two")] for c in picked
    }
    results, errors = dn.draft_all(client, picked, passages)
    assert len(batches.created) == 1 and len(batches.created[0]) == 6
    assert errors == {} and len(results) == 6
    per = {u.cost_usd for _, u in results.values()}
    # Sonnet batch: (1500 * 2 + 200 * 10) / 1e6 / 2 = 0.0025
    assert per == {Decimal("0.0025")}
    req = batches.created[0][0]["params"]
    assert req["model"] == "claude-sonnet-5-5" and req["system"][0]["text"] == dn.SYSTEM


def test_fb_page_still_served_through_a_sibling_chunk() -> None:
    gold = {"fb-1": ["fb:A_2020_10K#p3#0000"], "fb-2": ["fb:B_2021_10K#p9#0000"]}
    # the gold chunk on A p3 was re-chunked away but p3 still has a served chunk
    r = fb.compare(gold, set(), {"fb:A_2020_10K#p3#0002"})
    assert r["missing"] == 2
    assert r["rows_with_an_unserved_evidence_page"] == {"fb-2": ["B_2021_10K#p9"]}


def test_out_path_must_be_under_drafts(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="under"):
        dn.check_out_path(Path("eval/sets/narrative.jsonl"), overwrite=True)
    with pytest.raises(SystemExit, match="under"):
        dn.check_out_path(tmp_path / "x.jsonl", overwrite=True)


def test_existing_drafts_need_overwrite_and_curated_ones_are_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dn, "DRAFTS_DIR", tmp_path)
    out = tmp_path / "narrative_drafts.jsonl"
    dn.check_out_path(out, overwrite=False)  # absent: fine
    out.write_text(json.dumps({"verified_by": None}) + "\n")
    with pytest.raises(SystemExit, match="--overwrite"):
        dn.check_out_path(out, overwrite=False)
    dn.check_out_path(out, overwrite=True)
    out.write_text(json.dumps({"verified_by": "shekar"}) + "\n")
    with pytest.raises(SystemExit, match="verified"):
        dn.check_out_path(out, overwrite=True)


def test_unanswerable_template_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "unanswerable_template.jsonl"
    dn.write_template_if_absent(path)
    assert len(path.read_text().splitlines()) == 50
    path.write_text('{"id": "un-001", "question": "curated"}\n')
    dn.write_template_if_absent(path)
    assert "curated" in path.read_text()


def test_stub_mode_is_not_offered() -> None:
    with pytest.raises(SystemExit):
        dn.main(["--mode", "stub", "--estimate-only"])


def test_worst_case_estimate_bounds_the_typical_one() -> None:
    msgs = ["x" * 4000] * 10
    typical = dn.estimate_usd(msgs, batch=True)
    worst = dn.estimate_usd(msgs, batch=True, output_tokens=dn.MAX_TOKENS)
    assert worst > typical


class _FailFirst(_Batches):
    def results(self, batch_id: str) -> list[SimpleNamespace]:
        out = super().results(batch_id)
        out[0] = SimpleNamespace(custom_id=out[0].custom_id, result=SimpleNamespace(type="errored"))
        return out


def test_batch_failures_are_reported_by_chunk_id(tmp_path: Path) -> None:
    client = LLMClient(
        mode=LLMMode.batch,
        cassette_dir=tmp_path,
        sdk_client=SimpleNamespace(messages=SimpleNamespace(batches=_FailFirst())),
        registry={"composer": dn.DRAFT_MODEL},
        sleep=lambda s: None,
    )
    picked = dn.stratified_sample(_cands(), 3)
    passages = {c.chunk_id: [(c.chunk_id, "text")] for c in picked}
    results, errors = dn.draft_all(client, picked, passages)
    assert len(results) == 2
    assert list(errors) == [picked[0].chunk_id]
