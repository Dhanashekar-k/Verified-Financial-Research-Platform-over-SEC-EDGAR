"""report.py: engine-vs-baseline paired comparison, composer Pareto and retrieval ablation tables.

The inputs are small synthetic results files with known answers; nothing here is a measurement.
"""

from __future__ import annotations

import json
from typing import Any

from eval import report


def run(
    system: str, composer: str, split: str, correct: dict[str, bool | None], **kw: Any
) -> dict[str, Any]:
    qs = [
        {"id": i, "set": "numeric", "correct": c, "route": "r", "cost_usd": 0.0, "latency_ms": 0}
        for i, c in correct.items()
    ]
    n = len(qs)
    acc = sum(bool(c) for c in correct.values()) / n
    return {
        "run_id": kw.get("run_id", f"2026-12-01T00-00-00Z_{system}_{composer}_{split}"),
        "config": {
            "system": system,
            "composer": composer,
            "split": split,
            "mode": "replay",
            "sets": ["numeric"],
            "judge": None,
        },
        "sets": {"numeric": {"n": n, "accuracy": acc, "ci95": [acc, acc]}},
        "latency_ms": {"p50": None, "p95": kw.get("p95")},
        "cost": {"total_usd": 1.0, "per_question_usd": kw.get("per_q", 0.01)},
        "per_question": qs,
    }


def ids(n: int) -> list[str]:
    return [f"num-{i:04d}" for i in range(n)]


def test_a_clear_gain_is_reported_with_a_paired_ci_that_excludes_zero() -> None:
    base = run("baseline", "sonnet", "test", {i: False for i in ids(100)})
    eng = run("engine", "sonnet", "test", {i: (k < 60) for k, i in enumerate(ids(100))})
    md = "\n".join(report.render_comparison([base, eng]))
    assert "| test | numeric | 100 | 60.0% | 0.0% | +60.0 pp |" in md
    assert "engine higher" in md and "paired bootstrap" in md


def test_a_wash_says_no_difference_shown() -> None:
    flags = {i: (k % 2 == 0) for k, i in enumerate(ids(40))}
    flipped = {i: (k % 4 < 2) for k, i in enumerate(ids(40))}
    md = "\n".join(
        report.render_comparison(
            [run("baseline", "sonnet", "dev", flags), run("engine", "sonnet", "dev", flipped)]
        )
    )
    assert "no difference shown" in md and "engine higher" not in md


def test_a_loss_is_reported_as_baseline_higher() -> None:
    base = run("baseline", "sonnet", "test", {i: True for i in ids(50)})
    eng = run("engine", "sonnet", "test", {i: (k < 10) for k, i in enumerate(ids(50))})
    assert "baseline higher" in "\n".join(report.render_comparison([base, eng]))


def test_only_questions_both_runs_scored_are_paired() -> None:
    base = run("baseline", "sonnet", "test", {"a": True, "b": False, "c": None, "d": True})
    eng = run("engine", "sonnet", "test", {"a": True, "b": True, "c": True, "e": False})
    md = "\n".join(report.render_comparison([base, eng]))
    assert "| test | numeric | 2 |" in md, (
        "a and b only: c is unscored in one, d and e are unmatched"
    )


def test_splits_are_never_mixed() -> None:
    base = run("baseline", "sonnet", "dev", {i: False for i in ids(10)})
    eng = run("engine", "sonnet", "test", {i: True for i in ids(10)})
    md = "\n".join(report.render_comparison([base, eng]))
    assert "NOT MEASURED" in md and "| numeric |" not in md


def test_no_engine_run_means_not_measured() -> None:
    base = run("baseline", "sonnet", "dev", {"a": True})
    assert "NOT MEASURED" in "\n".join(report.render_comparison([base]))
    assert "NOT MEASURED" in "\n".join(report.render_pareto([base]))


def test_pareto_lists_each_engine_composer_with_cost_and_latency() -> None:
    h = run(
        "engine",
        "claude-haiku-5-5",
        "dev",
        {"a": True, "b": False},
        per_q=0.002,
        p95=900.0,
        run_id="2026-12-02T00-00-00Z_engine_haiku",
    )
    s = run(
        "engine",
        "claude-sonnet-5-5",
        "dev",
        {"a": True, "b": True},
        per_q=0.011,
        run_id="2026-12-02T01-00-00Z_engine_sonnet",
    )
    for doc, each in ((h, "0.002"), (s, "0.011")):
        for q in doc["per_question"]:
            q["system_cost_usd"] = each
    md = "\n".join(report.render_pareto([h, s, run("baseline", "sonnet", "dev", {"a": True})]))
    assert "claude-haiku-5-5 | 50.0% | NOT MEASURED | NOT MEASURED | 0.00200 | 900 |" in md
    assert (
        "claude-sonnet-5-5 | 100.0% | NOT MEASURED | NOT MEASURED | 0.01100 | NOT MEASURED |" in md
    )
    assert "baseline" not in md.split("---|")[-1]


def ablation_doc() -> dict[str, Any]:
    def row(name: str, hits8: list[float], p50: float) -> dict[str, Any]:
        qs = [
            {
                "id": f"n{i}",
                "set": "narrative",
                "latency_ms": p50,
                "relaxed_year_filter": False,
                "hits": {"1": h, "3": h, "5": h, "8": h},
            }
            for i, h in enumerate(hits8)
        ]
        rate = sum(hits8) / len(hits8)
        st: dict[str, Any] = {"n": len(qs)}
        for k in (1, 3, 5, 8):
            st[f"recall_at_{k}"] = rate
            st[f"recall_at_{k}_ci95"] = [rate, rate]
        return {
            "name": name,
            "config": {},
            "per_question": qs,
            "sets": {"narrative": st},
            "latency_ms": {"p50": p50, "p95": p50 * 2},
            "n_relaxed_year_filter": 0,
        }

    return {
        "run_id": "2026-12-03T00-00-00Z_retrieval-ablation",
        "corpus_version": 1,
        "config": {
            "n_questions": 20,
            "plan_source": "oracle: the gold company and fiscal year",
            "reranker": "Xenova/ms-marco-MiniLM-L-6-v2",
            "latency_note": "client-side",
        },
        "rows": [
            row("lexical only", [1.0] * 5 + [0.0] * 15, 10.0),
            row("hybrid (RRF)", [1.0] * 10 + [0.0] * 10, 30.0),
        ],
    }


def test_ablation_table_has_every_row_and_a_paired_difference() -> None:
    md = "\n".join(report.render_ablation([ablation_doc()]))
    assert "lexical only | 20 | 25.0% [25.0, 25.0]" in md and "hybrid (RRF) | 20 | 50.0%" in md
    assert "| - | 10 | 20 |" in md, "the first row has no previous row"
    assert "+25.0 pp" in md, "hybrid minus lexical, paired by question"
    assert "oracle" in md and "corpus version 1" in md


def test_no_ablation_is_not_measured() -> None:
    assert "NOT MEASURED" in "\n".join(report.render_ablation([]))


def test_render_carries_all_three_sections_and_the_ablation_alone_is_enough() -> None:
    base = run("baseline", "sonnet", "dev", {i: False for i in ids(10)})
    full = report.render([base], None, [ablation_doc()])
    for heading in ("### Engine vs baseline", "### Composer Pareto", "### Retrieval ablation"):
        assert heading in full
    assert "### Retrieval ablation" in report.render([], None, [ablation_doc()])
    assert report.render([], None, None).startswith("_No results files yet")


def test_the_ablation_heading_shows_the_split_it_was_run_on() -> None:
    doc = ablation_doc()
    doc["config"]["split"] = "all"
    assert "### Retrieval ablation (all split)" in "\n".join(report.render_ablation([doc]))


def latency_doc(composer: str, rid: str, p95: float, n: int = 40) -> dict[str, Any]:
    doc = run("engine", composer, "dev", {f"q{i}": None for i in range(n)}, run_id=rid)
    assert doc["config"]["system"] == "engine"
    doc["config"].update(mode="live", sample=10)
    doc["latency_ms"] = {"p50": p95 / 2, "p95": p95}
    return doc


def planner_doc(mode: str, cost: float, rid: str, n: int = 147) -> dict[str, Any]:
    config = {"mode": mode, "split": "dev", "n_questions": n}
    return {"run_id": rid, "config": config, "cost_usd": cost}


def test_pareto_takes_p95_from_the_live_sample_of_the_same_composer_and_says_what_it_is() -> None:
    h = run("engine", "claude-haiku-5-5", "dev", {"a": True}, run_id="h1")
    s = run("engine", "claude-sonnet-5-5", "dev", {"a": True}, run_id="s1")
    samples = [
        latency_doc("claude-haiku-5-5", "2026-12-03T00-00-00Z_old", 7000.0),
        latency_doc("claude-haiku-5-5", "2026-12-04T00-00-00Z_new", 4321.0, n=38),
    ]
    md = "\n".join(report.render_pareto([h, s], latency=samples))
    haiku, sonnet = (
        next(line for line in md.splitlines() if line.startswith(f"| `{r}`")) for r in ("h1", "s1")
    )
    assert haiku.endswith("| 4321 (dev live sample, n=38) |"), "the newest of that composer"
    assert sonnet.endswith("| NOT MEASURED |"), "no sample for this composer"
    assert "2026-12-04T00-00-00Z_new" in md and "n=38" in md and "old" not in md
    assert "network" in md and "not a server-side" in md


def test_pareto_says_dev_cost_leaves_the_cached_planner_out_and_gives_its_price() -> None:
    e = run("engine", "claude-haiku-5-5", "dev", {"a": True}, run_id="h1")
    docs = [
        planner_doc("batch", 0.0147, "2026-12-01T00-00-00Z_intent"),
        planner_doc("replay", 0.0, "2026-12-02T00-00-00Z_intent"),  # a replay costs nothing
    ]
    md = "\n".join(report.render_pareto([e], planner=docs))
    assert "2026-12-01T00-00-00Z_intent" in md and "$0.00010 per question" in md
    assert "replay" not in md and "2026-12-02" not in md


def test_a_latency_sample_never_replaces_a_full_run_in_the_tables(tmp_path: Any) -> None:
    """Same system, composer and split as the full run and newer: it must live in latency/."""
    full = run("engine", "claude-haiku-5-5", "dev", {"a": True, "b": True}, run_id="2026-a")
    (tmp_path / "latency").mkdir()
    (tmp_path / "a.json").write_text(json.dumps(full))
    sample = latency_doc("claude-haiku-5-5", "2026-b", 10.0, n=1)
    (tmp_path / "latency" / "b.json").write_text(json.dumps(sample))
    assert [r["run_id"] for r in report.select_runs(tmp_path)] == ["2026-a"]


# --- review before the paid runs: what may stand for a run ------------------------------------


def write_all(folder: Any, *docs: dict[str, Any]) -> None:
    for d in docs:
        (folder / f"{d['run_id']}.json").write_text(json.dumps(d))


def with_config(doc: dict[str, Any], **kw: Any) -> dict[str, Any]:
    doc["config"].update(kw)
    return doc


def test_a_newer_replay_does_not_replace_the_paid_run(tmp_path: Any) -> None:
    paid = with_config(run("engine", "h", "dev", {"a": True}, run_id="2026-1"), mode="batch")
    replay = with_config(run("engine", "h", "dev", {"a": True}, run_id="2026-2"), mode="replay")
    write_all(tmp_path, paid, replay)
    assert [r["run_id"] for r in report.select_runs(tmp_path)] == ["2026-1"]


def test_a_replay_still_shows_when_it_is_the_only_run(tmp_path: Any) -> None:
    write_all(tmp_path, with_config(run("baseline", "s", "test", {"a": True}, run_id="r1")))
    assert [r["run_id"] for r in report.select_runs(tmp_path)] == ["r1"]


def test_a_newer_paid_run_replaces_an_older_paid_run(tmp_path: Any) -> None:
    old = with_config(run("engine", "h", "dev", {"a": True}, run_id="2026-1"), mode="batch")
    new = with_config(run("engine", "h", "dev", {"a": False}, run_id="2026-2"), mode="batch")
    write_all(tmp_path, old, new)
    assert [r["run_id"] for r in report.select_runs(tmp_path)] == ["2026-2"]


def test_a_sampled_run_in_the_top_folder_never_replaces_or_enters_the_comparisons(
    tmp_path: Any,
) -> None:
    full = with_config(run("engine", "h", "dev", {"a": True}, run_id="2026-1"), mode="batch")
    sample = with_config(
        run("engine", "h", "dev", {"a": False}, run_id="2026-2"), mode="live", sample=10
    )
    write_all(tmp_path, full, sample)
    chosen = report.select_runs(tmp_path)
    assert "2026-1" in [r["run_id"] for r in chosen]
    assert "2026-2" not in "\n".join(report.render_pareto(chosen))


def test_only_an_engine_sample_with_the_same_settings_fills_p95() -> None:
    row = run("engine", "claude-sonnet-5-5", "test", {"a": True}, run_id="e1")
    row["config"].update(system_version="0.6.0", ef_search=800, top_k=8)
    row["corpus_version"] = 1

    def sample(rid: str, p95: float, system: str = "engine", **over: Any) -> dict[str, Any]:
        d = latency_doc("claude-sonnet-5-5", rid, p95)
        d["config"].update(system=system, system_version="0.6.0", ef_search=800, top_k=8)
        d["corpus_version"] = 1
        for k, v in over.items():
            if k == "corpus_version":
                d[k] = v
            else:
                d["config"][k] = v
        return d

    wrong = [
        sample("2026-9a", 1111.0, system="baseline"),
        sample("2026-9b", 2222.0, ef_search=200),
        sample("2026-9c", 3333.0, system_version="0.5.0"),
        sample("2026-9d", 4444.0, corpus_version=2),
        sample("2026-9e", 5555.0, top_k=20),
    ]
    md = "\n".join(report.render_pareto([row], latency=wrong))
    (cell,) = [line for line in md.splitlines() if line.startswith("| `e1`")]
    assert "live sample" not in md and cell.endswith("| NOT MEASURED |")
    md = "\n".join(report.render_pareto([row], latency=[*wrong, sample("2026-1", 9000.0)]))
    assert "| 9000 (dev live sample, n=40) |" in md, "the cell names the sample's split and size"


def test_pareto_cost_is_the_systems_own_spend_not_the_judges() -> None:
    e = run("engine", "claude-haiku-5-5", "dev", {"a": True, "b": True}, run_id="e1", per_q=0.9)
    for q, cost in zip(e["per_question"], ("0.0010", "0.0030"), strict=True):
        q["system_cost_usd"], q["judge_cost_usd"] = cost, "0.5"
    md = "\n".join(report.render_pareto([e]))
    assert "| 0.00200 |" in md and "0.90000" not in md
    assert "judge" in md


def test_a_sampled_ablation_never_replaces_the_full_one() -> None:
    full, smoke = ablation_doc(), ablation_doc()
    full["run_id"], smoke["run_id"] = "2026-1_full", "2026-2_smoke"
    smoke["config"]["sample"] = 3
    md = "\n".join(report.render_ablation([full, smoke]))
    assert "2026-1_full" in md and "2026-2_smoke" not in md


def test_the_ablation_table_says_what_its_figures_do_not_mean() -> None:
    md = " ".join("\n".join(report.render_ablation([ablation_doc()])).split())
    for needle in (
        "PDF copy",
        "one query",
        "do not isolate",
        "not each one added component",
    ):
        assert needle in md, needle


def test_the_filtered_ablation_is_a_second_table_that_does_not_replace_the_first() -> None:
    main_doc, extra = ablation_doc(), ablation_doc()
    main_doc["run_id"], extra["run_id"] = "2026-1_cumulative", "2026-2_filtered"
    md = report.render([], None, [main_doc], filtered=[extra])
    assert "2026-1_cumulative" in md and "2026-2_filtered" in md
    full = report.render(
        [run("engine", "h", "dev", {"a": True})], None, [main_doc], filtered=[extra]
    )
    assert "2026-1_cumulative" in full and "2026-2_filtered" in full
    assert report.FILTERED_TITLE in full
