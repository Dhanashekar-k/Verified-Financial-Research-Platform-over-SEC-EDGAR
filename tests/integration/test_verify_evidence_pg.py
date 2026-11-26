"""The verifier's evidence reads against a real Postgres 17 + pgvector, with the serving tables
created from the serving templates. The container's memory is capped: the Docker VM is shared."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.engine.answer import Answer, ChunkProvenance, Claim, ExprProvenance
from app.engine.verify.evidence import load_evidence

pytest.importorskip("testcontainers")
psycopg = pytest.importorskip("psycopg")

TEMPLATES = Path(__file__).resolve().parents[2] / "serving" / "sql" / "templates"
NEGATIVE_ID = -6651281536542086215


@pytest.fixture(scope="module")
def conn() -> Iterator[Any]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    box = PostgresContainer("pgvector/pgvector:pg17", driver=None).with_kwargs(
        mem_limit="768m", memswap_limit="768m"
    )
    with box as pg, psycopg.connect(pg.get_connection_url(), autocommit=True) as c:
        c.execute("CREATE EXTENSION vector")
        for name in ("facts", "chunks"):
            c.execute((TEMPLATES / f"{name}.sql").read_text().format(v=1))
            c.execute(f"CREATE VIEW {name} AS SELECT * FROM {name}_v1")
        add_fact(c, NEGATIVE_ID, "100.5", derived_from=[2, 3])
        add_fact(c, 2, "300.5")
        add_fact(c, 3, "200")
        for chunk_id in ("shown", "unseen"):
            c.execute(
                "INSERT INTO chunks_v1 (chunk_id, cik, text, embedding) VALUES (%s, %s, %s, %s)",
                [chunk_id, 320193, f"body of {chunk_id}", "[" + ",".join(["0"] * 384) + "]"],
            )
        yield c


def add_fact(c: Any, fact_id: int, value: str, derived_from: list[int] | None = None) -> None:
    c.execute(
        "INSERT INTO facts_v1 (fact_id, fact_key, cik, tag, taxonomy, uom, qtrs, period_end, "
        "fiscal_year, fiscal_period, value, accession, form, filed, is_latest, is_original, "
        "derived_from) VALUES (%s, %s, 320193, 'Revenues', 'us-gaap', 'USD', 1, '2023-12-31', "
        "2023, 'Q4', %s, 'a', '10-K', '2024-02-01', true, true, %s)",
        [fact_id, fact_id, Decimal(value), derived_from],
    )


def test_reads_cited_facts_with_parents_and_only_shown_chunks(conn: Any) -> None:
    answer = Answer(
        answer_markdown="m",
        claims=[
            Claim(
                id="c1",
                kind="numeric",
                text="t",
                provenance=ExprProvenance(
                    type="expr", expr=f"f:{NEGATIVE_ID} + f:404", fact_ids=[NEGATIVE_ID, 404]
                ),
            ),
            *(
                Claim(
                    id=f"c{n}",
                    kind="text",
                    text="t",
                    provenance=ChunkProvenance(type="chunk", chunk_id=chunk_id, quote="q"),
                )
                for n, chunk_id in enumerate(("shown", "unseen"), start=2)
            ),
        ],
    )
    evidence = load_evidence(conn, answer, [], ["shown"])
    assert sorted(evidence.facts) == [NEGATIVE_ID, 2, 3]
    derived = evidence.facts[NEGATIVE_ID]
    assert derived.derived_from == [2, 3] and derived.value == Decimal("100.5")
    assert isinstance(derived.value, Decimal)
    assert list(evidence.chunks) == ["shown"] and evidence.chunks["shown"].cik == 320193


def test_an_answer_with_no_claims_reads_nothing(conn: Any) -> None:
    evidence = load_evidence(conn, Answer(answer_markdown="m"), [], [])
    assert evidence.facts == {} and evidence.chunks == {}
