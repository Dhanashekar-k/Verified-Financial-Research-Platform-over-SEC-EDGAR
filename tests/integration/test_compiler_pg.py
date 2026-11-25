"""The plan compiler (05_data_contracts.md 4.5, ADR-042/043/046) against a real Postgres 17.

Test-first: `app/engine/plan/compiler.py` is a stub that raises NotImplementedError, so every test
that calls `compile_plan` fails until Shekar implements it. Nothing here is skipped or xfailed.

The data is `tests/fixtures/compiler_facts.jsonl`: 156 REAL rows of `curated.facts` for five
companies, written once by `tests/fixtures/make_compiler_facts.py` (never edited by hand), loaded
into `facts_v1` plus the `facts` view exactly as 05 section 3 defines them. Expected values and
fact ids below are literals copied from those rows (`test_literals_match_the_fixture_file` proves
the copy is faithful). The expression check (`eval_expr`) is an independent Decimal evaluator
written for the tests; it shares no code with the compiler.

The five companies and what each one supplies (the CIKs are the keys of `COMPANY`):
  * Texas Instruments (97476): ONE revenue tag; both cost tags that agree exactly; GrossProfit
    reported; EPS; instants; a derived Q4 with its 9M parent; Q2/Q3 rows next to H1/9M rows.
  * Insulet (1145197): both revenue tags agree (FY2020/FY2021) and conflict (FY2022, Q2-Q4) with
    GrossProfit settling it; FY2021 contract revenue is restated; a restated derived Q4; the full
    Q2/H1/Q3/9M/FY ladder; a net-loss quarter (non-positive growth base).
  * Dollar Tree (935703): revenue tags that agree only INSIDE the 4-significant-figure tolerance
    (FY2021/FY2022/Q2 2022) and disagree just outside it with nothing to settle it (FY2023,
    Q2 2023); no GrossProfit (fallback formula); net loss; no R&D; restated share counts.
  * Aflac (4977): one revenue tag, no OperatingIncomeLoss at all, restated revenue / net income /
    EPS / assets and a restated derived Q4.
  * Inogen (1294133): both revenue tags AND both cost tags conflict, GrossProfit settles them;
    operating losses; negative EPS; FY2023 revenue conflict with no cost or GrossProfit to check.

Contract ambiguities this file had to pick a side on are marked `ASSUMPTION` in the docstring; the
reviewer should confirm or overrule each one.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.engine.plan.catalog import MetricDef, load_catalog
from app.engine.plan.compiler import compile_plan
from app.engine.plan.schema import (
    CompileOutcome,
    NotFound,
    NumericResult,
    QueryPlan,
    UnsupportedPlan,
)

pytest.importorskip("testcontainers")
psycopg = pytest.importorskip("psycopg")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "compiler_facts.jsonl"
D = Decimal

# ---- the five companies -------------------------------------------------------------------

TI, INSULET, DT, AFLAC, INOGEN = 97476, 1145197, 935703, 4977, 1294133
COMPANY = {
    TI: "Texas Instruments",
    INSULET: "Insulet",
    DT: "Dollar Tree",
    AFLAC: "Aflac",
    INOGEN: "Inogen",
}
CIKS = list(COMPANY)

# ---- fact ids and values copied from the fixture rows (real, signed) ------------------------

# Texas Instruments
TI_REV_FY21, TI_REV_FY21_V = -8138443950304199831, D("18344000000")
TI_REV_FY22, TI_REV_FY22_V = -2531737455286848, D("20028000000")
TI_REV_Q2_21, TI_REV_Q2_21_V = -3583253794184470323, D("4580000000")
TI_REV_Q2_22, TI_REV_Q2_22_V = 6172526386912960551, D("5212000000")
TI_REV_Q3_22, TI_REV_Q3_22_V = 426795109496763176, D("5241000000")
TI_REV_H1_22_V, TI_REV_9M_22_V = D("10117000000"), D("15358000000")
TI_REV_Q4_22, TI_REV_Q4_22_V = -3470622262422902640, D("4670000000")  # derived Q4
TI_COST_C1_FY21, TI_COST_C2_FY21 = 6513721436419384064, -1268201653605429138  # both 5968000000
TI_COST_FY21_V = D("5968000000")
TI_COST_C1_Q2_22, TI_COST_Q2_22_V = 2045185986912386706, D("1587000000")  # CostOfRevenue only
TI_COST_C1_Q4_22, TI_COST_Q4_22_V = 7617584080526714415, D("1583000000")  # derived; both tags
TI_GP_FY22, TI_GP_FY22_V = 6148964966655617968, D("13771000000")
TI_GP_FY21, TI_GP_FY21_V = -5974049095950045777, D("12376000000")
TI_OPI_FY22, TI_OPI_FY22_V = -7410015342165464637, D("10140000000")
TI_NI_FY22, TI_NI_FY22_V = -2886022854582201940, D("8749000000")
TI_NI_FY21, TI_NI_FY21_V = 8584021482898302638, D("7769000000")
TI_EPS_Q2_22, TI_EPS_Q2_22_V = -8098547722402139458, D("2.45")
TI_EPS_FY22, TI_EPS_FY22_V = -2735545078737693060, D("9.41")
TI_EPS_Q4_22, TI_EPS_Q4_22_V = 8452419949765907000, D("2.14")  # derived Q4
TI_AST_FY22, TI_AST_FY22_V = -3390292580615710233, D("27207000000")
TI_AST_Q2_22, TI_AST_Q2_22_V = 6164305160178594990, D("24723000000")
TI_FY22_9M_REV_ID = 2982149343067358978  # the 9M parent of the derived Q4

# Insulet
INS_REVC_FY20, INS_REVT_FY20, INS_REV_FY20_V = (
    5400229028569984373,
    3290290436441401118,
    D("904400000"),
)
INS_REVC_FY21_ORIG, INS_REVC_FY21_LATEST = -4430868043319158479, 1598062349662999847
INS_REVC_FY21_ORIG_V, INS_REVC_FY21_LATEST_V = D("1098800000"), D("1040600000")
INS_REVT_FY21 = -3919144136140708684  # 1098800000, agrees with the ORIGINAL contract revenue
INS_REVT_FY22, INS_REVT_FY22_V = 168824007082624363, D("1305300000")
INS_REVC_FY22_V = D("1055400000")  # the losing candidate in FY2022
INS_REVT_Q2_21, INS_REVT_Q2_21_V = 8407906995039503184, D("263200000")
INS_REVT_Q3_21, INS_REVT_Q3_21_V = -60979555467799153, D("275600000")
INS_REVT_Q2_22, INS_REVT_Q2_22_V = -6694066425036893721, D("299400000")
INS_REVT_Q4_21, INS_REVT_Q4_21_V = 6466151538776921547, D("307700000")  # derived Q4, settled
INS_REVC_Q4_21_V = D("327000000")  # the ORIGINAL derived Q4 of the losing, restated candidate
INS_GP_FY22, INS_GP_FY22_V = -2474052563551103619, D("805600000")
INS_GP_FY21, INS_GP_FY21_V = 5446979760851940432, D("752100000")
INS_OPI_FY22, INS_OPI_FY22_V = -1812698869326136075, D("37600000")
INS_COGS_Q2_22, INS_COGS_Q2_22_V = -6580370943640833481, D("109100000")
INS_NI_Q2_21, INS_NI_Q2_22 = 2069492378125032870, 5234110313094585983  # -25.0M, -35.0M
INS_NI_Q3_21, INS_NI_Q3_21_V = 1948889070247635815, D("12600000")
INS_NI_Q3_22, INS_NI_Q3_22_V = -239626298460350612, D("-5200000")
INS_NI_FY22 = 77818095085228358

# Dollar Tree (fiscal_year 2023 = the year ended early 2024)
DT_REVC_FY21, DT_REVC_FY21_V = 2375501028036906643, D("26309800000")  # REVT is 26321200000
DT_REVC_FY22, DT_REVC_FY22_V = 4252780990993461653, D("28318200000")
DT_REVC_Q2_22, DT_REVC_Q2_22_V = -133274397978246619, D("6765300000")
DT_COGS_FY21, DT_COGS_FY21_V = -228792152466623438, D("18583900000")
DT_COGS_FY22, DT_COGS_FY22_V = 1191220970606318013, D("19396300000")
DT_NI_FY22, DT_NI_FY22_V = -6646726363556383197, D("1615400000")
DT_NI_FY23, DT_NI_FY23_V = -4295613761578619926, D("-998400000")
DT_OPI_FY23_V = D("-881800000")
DT_SHS_FY22, DT_SHS_FY22_V = 5621630359336862787, D("221222984")
DT_SHS_Q2_22_ORIG, DT_SHS_Q2_22_ORIG_V = -4421669141249879103, D("223900000")
DT_SHS_Q2_22_LATEST, DT_SHS_Q2_22_LATEST_V = -2872683304479176672, D("223932187")
DT_SHS_FY21, DT_SHS_FY21_V = 5452712244716194883, D("225100198")  # is_restated but ONE row

# Aflac (restated: the original row is what the compiler returns)
AF_REV_FY20, AF_REV_FY20_V = 1520895412222400922, D("22147000000")
AF_REV_FY21, AF_REV_FY21_V = -8987823700213260798, D("22106000000")
AF_REV_FY21_LATEST, AF_REV_FY21_LATEST_V = -344214307090522509, D("21554000000")
AF_NI_FY20, AF_NI_FY20_V = 4438489024787484884, D("4778000000")
AF_NI_FY21, AF_NI_FY21_V = -1991459840442217201, D("4325000000")
AF_NI_FY21_LATEST, AF_NI_FY21_LATEST_V = -845883351950726951, D("4231000000")
AF_NI_FY22, AF_NI_FY22_V = 847080019115577403, D("4201000000")
AF_EPS_FY21, AF_EPS_FY21_V = -2587698500844075776, D("6.39")
AF_EPS_FY21_LATEST, AF_EPS_FY21_LATEST_V = -7509180716557412239, D("6.25")
AF_AST_FY22, AF_AST_FY22_V = -5791169715122297340, D("131017000000")
AF_AST_FY22_LATEST, AF_AST_FY22_LATEST_V = -802123625710104372, D("131738000000")
AF_REV_Q4_21, AF_REV_Q4_21_V = 3083153222120644404, D("5436000000")  # derived AND restated
AF_REV_Q4_21_LATEST, AF_REV_Q4_21_LATEST_V = -277040052394166803, D("4884000000")

# Inogen
IN_REVC_FY21_V, IN_REVT_FY21, IN_REVT_FY21_V = D("311730000"), 7017358241867393259, D("358003000")
IN_REVT_FY20, IN_REVT_FY20_V = -5970818930517585455, D("308487000")
IN_REVT_FY22, IN_REVT_FY22_V = -3107944191305133987, D("377241000")
IN_REVT_Q2_21, IN_REVT_Q2_21_V = -2245673061240271149, D("101563000")
IN_COST_C1_FY21, IN_COST_C1_FY21_V = 5373825662756305338, D("181520000")  # COGS is 161824000
IN_COST_C1_FY20 = 6943014802466258328
IN_COST_C1_Q2_21, IN_COST_C1_Q2_21_V = -8394676926316439093, D("51228000")
IN_GP_FY21, IN_GP_FY21_V = -3989266372192634942, D("176483000")
IN_OPI_FY20, IN_OPI_FY20_V = 5130801923829040296, D("-12025000")
IN_OPI_FY21, IN_OPI_FY21_V = 4588054223895326990, D("9240000")
IN_OPI_FY22, IN_OPI_FY22_V = -2492916765975372266, D("-85243000")
IN_EPS_FY22, IN_EPS_FY22_V = 397323307206113056, D("-3.67")

# ---- the database -----------------------------------------------------------------------------

DDL = """
CREATE TABLE facts_v1 (
  fact_id bigint PRIMARY KEY, fact_key bigint NOT NULL, cik int NOT NULL, tag text NOT NULL,
  taxonomy text NOT NULL, uom text NOT NULL, qtrs smallint NOT NULL, period_start date,
  period_end date NOT NULL, fiscal_year smallint NOT NULL, fiscal_period text NOT NULL,
  calendar_frame text, value numeric NOT NULL, accession text NOT NULL, form text NOT NULL,
  filed date NOT NULL, is_latest boolean NOT NULL, is_original boolean NOT NULL,
  is_restated boolean NOT NULL DEFAULT false, anomaly_score real, derived_from bigint[]);
CREATE INDEX ON facts_v1 (cik, tag, fiscal_year, fiscal_period, uom) WHERE is_original;
CREATE INDEX ON facts_v1 (fact_key);
CREATE INDEX ON facts_v1 (tag, fiscal_year, fiscal_period);
CREATE OR REPLACE VIEW facts AS SELECT * FROM facts_v1;
"""
COLUMNS = (
    "fact_id fact_key cik tag taxonomy uom qtrs period_start period_end fiscal_year "
    "fiscal_period calendar_frame value accession form filed is_latest is_original is_restated "
    "anomaly_score derived_from"
).split()
INSERT = f"INSERT INTO facts_v1 ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})"
_DATE_COLUMNS = ("period_start", "period_end", "filed")


@dataclass(frozen=True)
class Row:
    fact_id: int
    cik: int
    tag: str
    uom: str
    qtrs: int
    fiscal_year: int
    fiscal_period: str
    value: Decimal
    is_original: bool
    is_latest: bool
    is_restated: bool
    derived_from: tuple[int, ...] | None


def load_fixture() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


RAW = load_fixture()
ROWS = [
    Row(
        r["fact_id"], r["cik"], r["tag"], r["uom"], r["qtrs"], r["fiscal_year"],
        r["fiscal_period"], D(r["value"]), r["is_original"], r["is_latest"], r["is_restated"],
        tuple(r["derived_from"]) if r["derived_from"] else None,
    )
    for r in RAW
]  # fmt: skip
VALUE_OF: dict[int, Decimal] = {r.fact_id: r.value for r in ROWS}
ORIGINAL_IDS = {r.fact_id for r in ROWS if r.is_original}


def db_values(r: dict[str, Any]) -> list[Any]:
    out: list[Any] = []
    for col in COLUMNS:
        v = r[col]
        if col in _DATE_COLUMNS and v is not None:
            v = date.fromisoformat(v)
        elif col == "value":
            v = D(v)
        out.append(v)
    return out


def load_into(conn: Any, rows: Sequence[dict[str, Any]]) -> None:
    conn.execute(DDL)
    with conn.cursor() as cur:
        cur.executemany(INSERT, [db_values(r) for r in rows])


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer  # type: ignore[no-redef]
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def conn(pg_url: str) -> Iterator[Any]:
    """`facts_v1` + the `facts` view loaded with the real fixture rows, plus a poisoned
    `facts_dimensional` table (segmented twins with 7x values) that the compiler must never read."""
    with psycopg.connect(pg_url, autocommit=True) as c:
        load_into(c, RAW)
        c.execute("CREATE TABLE facts_dimensional (LIKE facts_v1)")
        c.execute("ALTER TABLE facts_dimensional ADD COLUMN segments text DEFAULT 'Segment=X'")
        c.execute(
            "INSERT INTO facts_dimensional (fact_id, fact_key, cik, tag, taxonomy, uom, qtrs, "
            "period_start, period_end, fiscal_year, fiscal_period, calendar_frame, value, "
            "accession, form, filed, is_latest, is_original, is_restated, anomaly_score, "
            "derived_from) SELECT fact_id # 1, fact_key, cik, tag, taxonomy, uom, qtrs, "
            "period_start, period_end, fiscal_year, fiscal_period, calendar_frame, value * 7, "
            "accession, form, filed, is_latest, is_original, is_restated, anomaly_score, "
            "derived_from FROM facts_v1"
        )
        yield c


# ---- helpers ------------------------------------------------------------------------------------

Period = tuple[int, str]
FACT_RE = re.compile(r"f:(-?\d+)")


def make_plan(
    metrics: Sequence[str],
    ciks: Sequence[int | None],
    periods: Sequence[Period],
    op: str | None = "value",
    *,
    intent: str = "numeric",
) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "intent": intent,
            "companies": [
                {"mention": COMPANY.get(c, "Unknown Corp") if c else "Unknown Corp", "cik": c}
                for c in ciks
            ],
            "periods": [{"fiscal_year": y, "fiscal_period": p} for y, p in periods],
            "metrics": list(metrics),
            "operation": op,
        }
    )


def run(
    conn: Any,
    metrics: Sequence[str],
    ciks: Sequence[int | None],
    periods: Sequence[Period],
    op: str | None = "value",
    **kw: Any,
) -> CompileOutcome:
    return compile_plan(make_plan(metrics, ciks, periods, op), conn, **kw)


def eval_expr(expr: str, values: Mapping[int, Decimal]) -> Decimal:
    """Independent evaluator of the 4.3 grammar: `f:<signed id>` tokens, numbers, + - * / and
    parentheses, all in Decimal under the default context (never float)."""
    names: dict[int, str] = {}

    def sub(m: re.Match[str]) -> str:
        return names.setdefault(int(m.group(1)), f"v{len(names)}")

    tree = ast.parse(FACT_RE.sub(sub, expr).strip(), mode="eval")
    env = {name: values[fid] for fid, name in names.items()}

    def go(n: ast.AST) -> Decimal:
        if isinstance(n, ast.Expression):
            return go(n.body)
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.Constant):
            return D(str(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -go(n.operand)
        if isinstance(n, ast.BinOp):
            a, b = go(n.left), go(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
        raise AssertionError(f"unexpected node {ast.dump(n)} in {expr!r}")

    return go(tree)


def tokens(expr: str) -> set[int]:
    return {int(i) for i in FACT_RE.findall(expr)}


def check_provenance(res: NumericResult) -> None:
    """The contract every result must meet: fact ids are real original-version facts, the
    expression's tokens are exactly `fact_ids`, and re-executing it reproduces `value`."""
    assert res.fact_ids, "a result without a fact id"
    assert tokens(res.expr) == set(res.fact_ids)
    assert set(res.fact_ids) <= ORIGINAL_IDS, "the default path cites is_original rows only"
    prov = res.provenance()
    assert prov.expr == res.expr and prov.fact_ids == res.fact_ids
    assert eval_expr(res.expr, VALUE_OF) == res.value
    assert isinstance(res.value, Decimal)
    if not re.fullmatch(r"f:-?\d+", res.expr):
        assert res.kind == "derived", "any arithmetic is 'derived'"


def one(out: CompileOutcome) -> NumericResult:
    assert len(out.results) == 1, out
    assert not out.not_found, out
    check_provenance(out.results[0])
    return out.results[0]


def only_not_found(out: CompileOutcome) -> NotFound:
    assert not out.results, f"a value was returned where none is supported: {out.results}"
    assert len(out.not_found) == 1, out
    return out.not_found[0]


def pick(
    out: CompileOutcome, *, op: str = "value", cik: int | None = None, **kw: Any
) -> NumericResult:
    hits = [
        r
        for r in out.results
        if r.operation == op and (cik is None or r.cik == cik)
        and all(getattr(r, k) == v for k, v in kw.items())
    ]  # fmt: skip
    assert len(hits) == 1, (op, cik, kw, out.results)
    check_provenance(hits[0])
    return hits[0]


def note(res: NumericResult, code: str) -> list[Any]:
    return [n for n in res.notes if n.code == code]


# ======================================================================
# 0. The fixture itself (these pass without the compiler; they pin that the literals are faithful)
# ======================================================================


def test_fixture_size_ids_and_columns() -> None:
    """The fixture is small (cap 300), has unique signed fact_ids (some negative, as in the real
    data) and carries every facts_v7 column, so the Postgres load is the real 05 section 3 shape."""
    assert 100 <= len(RAW) <= 300
    assert len({r["fact_id"] for r in RAW}) == len(RAW)
    assert any(r["fact_id"] < 0 for r in RAW) and any(r["fact_id"] > 0 for r in RAW)
    assert all(list(r) == COLUMNS for r in RAW)
    assert {r["cik"] for r in RAW} == set(CIKS)


def test_literals_match_the_fixture_file() -> None:
    """Every (fact_id -> value) literal this module asserts against is a real fixture row; a
    regenerated fixture that drifts fails here, not as a confusing compiler failure."""
    expected = {
        TI_REV_FY21: TI_REV_FY21_V, TI_REV_FY22: TI_REV_FY22_V, TI_REV_Q2_21: TI_REV_Q2_21_V,
        TI_REV_Q2_22: TI_REV_Q2_22_V, TI_REV_Q3_22: TI_REV_Q3_22_V, TI_REV_Q4_22: TI_REV_Q4_22_V,
        TI_COST_C1_FY21: TI_COST_FY21_V, TI_COST_C2_FY21: TI_COST_FY21_V,
        TI_COST_C1_Q2_22: TI_COST_Q2_22_V, TI_COST_C1_Q4_22: TI_COST_Q4_22_V,
        TI_GP_FY22: TI_GP_FY22_V, TI_GP_FY21: TI_GP_FY21_V, TI_OPI_FY22: TI_OPI_FY22_V,
        TI_NI_FY22: TI_NI_FY22_V, TI_NI_FY21: TI_NI_FY21_V, TI_EPS_Q2_22: TI_EPS_Q2_22_V,
        TI_EPS_FY22: TI_EPS_FY22_V, TI_EPS_Q4_22: TI_EPS_Q4_22_V, TI_AST_FY22: TI_AST_FY22_V,
        TI_AST_Q2_22: TI_AST_Q2_22_V,
        INS_REVC_FY20: INS_REV_FY20_V, INS_REVT_FY20: INS_REV_FY20_V,
        INS_REVC_FY21_ORIG: INS_REVC_FY21_ORIG_V, INS_REVC_FY21_LATEST: INS_REVC_FY21_LATEST_V,
        INS_REVT_FY21: INS_REVC_FY21_ORIG_V, INS_REVT_FY22: INS_REVT_FY22_V,
        INS_REVT_Q2_21: INS_REVT_Q2_21_V, INS_REVT_Q3_21: INS_REVT_Q3_21_V,
        INS_REVT_Q2_22: INS_REVT_Q2_22_V, INS_REVT_Q4_21: INS_REVT_Q4_21_V,
        INS_GP_FY22: INS_GP_FY22_V, INS_GP_FY21: INS_GP_FY21_V, INS_OPI_FY22: INS_OPI_FY22_V,
        INS_COGS_Q2_22: INS_COGS_Q2_22_V, INS_NI_Q2_21: D("-25000000"),
        INS_NI_Q2_22: D("-35000000"),
        INS_NI_Q3_21: INS_NI_Q3_21_V, INS_NI_Q3_22: INS_NI_Q3_22_V, INS_NI_FY22: D("4600000"),
        DT_REVC_FY21: DT_REVC_FY21_V, DT_REVC_FY22: DT_REVC_FY22_V,
        DT_REVC_Q2_22: DT_REVC_Q2_22_V,
        DT_COGS_FY21: DT_COGS_FY21_V, DT_COGS_FY22: DT_COGS_FY22_V, DT_NI_FY22: DT_NI_FY22_V,
        DT_NI_FY23: DT_NI_FY23_V, DT_SHS_FY22: DT_SHS_FY22_V, DT_SHS_FY21: DT_SHS_FY21_V,
        DT_SHS_Q2_22_ORIG: DT_SHS_Q2_22_ORIG_V, DT_SHS_Q2_22_LATEST: DT_SHS_Q2_22_LATEST_V,
        AF_REV_FY20: AF_REV_FY20_V, AF_REV_FY21: AF_REV_FY21_V,
        AF_REV_FY21_LATEST: AF_REV_FY21_LATEST_V,
        AF_NI_FY20: AF_NI_FY20_V, AF_NI_FY21: AF_NI_FY21_V, AF_NI_FY21_LATEST: AF_NI_FY21_LATEST_V,
        AF_NI_FY22: AF_NI_FY22_V, AF_EPS_FY21: AF_EPS_FY21_V,
        AF_EPS_FY21_LATEST: AF_EPS_FY21_LATEST_V,
        AF_AST_FY22: AF_AST_FY22_V, AF_AST_FY22_LATEST: AF_AST_FY22_LATEST_V,
        AF_REV_Q4_21: AF_REV_Q4_21_V, AF_REV_Q4_21_LATEST: AF_REV_Q4_21_LATEST_V,
        IN_REVT_FY21: IN_REVT_FY21_V, IN_REVT_FY20: IN_REVT_FY20_V, IN_REVT_FY22: IN_REVT_FY22_V,
        IN_REVT_Q2_21: IN_REVT_Q2_21_V, IN_COST_C1_FY21: IN_COST_C1_FY21_V,
        IN_COST_C1_Q2_21: IN_COST_C1_Q2_21_V, IN_GP_FY21: IN_GP_FY21_V, IN_OPI_FY20: IN_OPI_FY20_V,
        IN_OPI_FY21: IN_OPI_FY21_V, IN_OPI_FY22: IN_OPI_FY22_V, IN_EPS_FY22: IN_EPS_FY22_V,
    }  # fmt: skip
    for fid, value in expected.items():
        assert VALUE_OF.get(fid) == value, (fid, value, VALUE_OF.get(fid))


def test_facts_view_exposes_the_fixture(conn: Any) -> None:
    """The `facts` view over `facts_v1` returns every fixture row with Decimal values, and the
    poisoned `facts_dimensional` twin exists (so a compiler that read it would be caught)."""
    n, total = conn.execute("SELECT count(*), sum(value) FROM facts").fetchone()
    assert n == len(RAW) and isinstance(total, Decimal)
    assert conn.execute("SELECT count(*) FROM facts_dimensional").fetchone()[0] == len(RAW)


def test_derived_q4_fixture_row_is_fy_minus_9m() -> None:
    """The real derived Q4 in the fixture satisfies the ADR-022 identity with the parents it
    names in derived_from (FY id first, 9M id second), so the q4_derived tests use a true row."""
    q4 = next(r for r in ROWS if r.fact_id == TI_REV_Q4_22)
    assert q4.derived_from == (TI_REV_FY22, TI_FY22_9M_REV_ID) and q4.qtrs == 1
    assert VALUE_OF[TI_REV_FY22] - VALUE_OF[TI_FY22_9M_REV_ID] == q4.value == TI_REV_Q4_22_V


# ======================================================================
# 1. Single-tag metrics, periods, units, exactness
# ======================================================================


def test_single_tag_annual_value_is_one_reported_fact(conn: Any) -> None:
    """TI FY2022 revenue (one populated tag) comes back as exactly that fact: value in base USD,
    expr 'f:<id>', kind reported, resolution single_candidate, no notes."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")]))
    assert (res.metric, res.cik, res.fiscal_year, res.fiscal_period) == ("revenue", TI, 2022, "FY")
    assert res.operation == "value" and res.unit == "USD" and res.kind == "reported"
    assert res.value == TI_REV_FY22_V
    assert res.expr == f"f:{TI_REV_FY22}" and res.fact_ids == [TI_REV_FY22]
    assert res.resolution == "single_candidate"
    assert res.notes == [] and res.compare_cik is None and res.rank is None


def test_values_stay_in_base_units(conn: Any) -> None:
    """No scaling to millions or thousands anywhere: 20,028,000,000 stays 20,028,000,000
    (CLAUDE.md: values are stored and returned in base units; formatting is the composer's job)."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")]))
    assert res.value == D("20028000000") and res.value != D("20028")
    small = one(run(conn, ["net_income"], [INSULET], [(2022, "FY")]))
    assert small.value == D("4600000")


def test_decimal_exactness_no_float(conn: Any) -> None:
    """EPS 2.45 is exactly Decimal('2.45'): a float round trip (Decimal(2.45) != Decimal('2.45'))
    would break verification, and the field must be a Decimal, not a float."""
    res = one(run(conn, ["eps_diluted"], [TI], [(2022, "Q2")]))
    assert isinstance(res.value, Decimal)
    assert res.value == D("2.45") and res.value != D(2.45)
    assert res.fact_ids == [TI_EPS_Q2_22]


def test_eps_is_usd_per_share_although_facts_store_uom_usd(conn: Any) -> None:
    """ASSUMPTION (contract ambiguity): the catalog unit of eps_diluted is 'USD/shares' but the
    curated facts store EPS with uom 'USD' (FSDS records per-share amounts as USD; the gold policy
    admits that alias). The compiler must still find the fact and report unit 'USD/shares'. A
    literal `uom = metric.unit` filter would return no_fact for every EPS question."""
    res = one(run(conn, ["eps_diluted"], [TI], [(2022, "FY")]))
    assert res.value == TI_EPS_FY22_V and res.unit == "USD/shares"
    assert res.fact_ids == [TI_EPS_FY22]
    neg = one(run(conn, ["eps_diluted"], [INOGEN], [(2022, "FY")]))
    assert neg.value == IN_EPS_FY22_V == D("-3.67") and neg.unit == "USD/shares"


def test_shares_outstanding_instant_with_shares_unit(conn: Any) -> None:
    """shares_outstanding is an instant in 'shares': Dollar Tree FY2022 = 221,222,984, unit
    'shares', no currency scaling."""
    res = one(run(conn, ["shares_outstanding"], [DT], [(2022, "FY")]))
    assert res.value == DT_SHS_FY22_V and res.unit == "shares" and res.kind == "reported"
    assert res.fact_ids == [DT_SHS_FY22]


def test_instants_use_qtrs_zero_for_year_end_and_quarter_end(conn: Any) -> None:
    """Instant metrics read qtrs = 0: total_assets FY2022 and Q2 2022 are two different instants,
    each returned for its own period (TI has both)."""
    fy = one(run(conn, ["total_assets"], [TI], [(2022, "FY")]))
    q2 = one(run(conn, ["total_assets"], [TI], [(2022, "Q2")]))
    assert (fy.value, fy.fact_ids) == (TI_AST_FY22_V, [TI_AST_FY22])
    assert (q2.value, q2.fact_ids) == (TI_AST_Q2_22_V, [TI_AST_Q2_22])
    assert fy.unit == q2.unit == "USD"


@pytest.mark.parametrize(
    ("period", "fid", "value", "ytd_or_fy"),
    [
        ((2022, "Q2"), TI_REV_Q2_22, TI_REV_Q2_22_V, [TI_REV_H1_22_V, TI_REV_FY22_V]),
        ((2022, "Q3"), TI_REV_Q3_22, TI_REV_Q3_22_V, [TI_REV_9M_22_V, TI_REV_FY22_V]),
    ],
)
def test_quarter_request_returns_the_single_quarter_fact(
    conn: Any, period: Period, fid: int, value: Decimal, ytd_or_fy: list[Decimal]
) -> None:
    """A Q2 / Q3 request returns the qtrs = 1 fact, never the year-to-date (H1 qtrs 2, 9M qtrs 3)
    or full-year (qtrs 4) fact that sits next to it in the same fiscal year (05 4.5: 'qtrs = 1 for
    Q1 to Q4')."""
    res = one(run(conn, ["revenue"], [TI], [period]))
    assert (res.value, res.fact_ids, res.fiscal_period) == (value, [fid], period[1])
    assert res.value not in ytd_or_fy


def test_annual_request_returns_qtrs_4_not_q4_or_ytd(conn: Any) -> None:
    """An FY request returns the qtrs = 4 fact: not the derived Q4 (4.67B) nor 9M (15.36B)."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")]))
    assert res.value == TI_REV_FY22_V
    assert res.value not in (TI_REV_Q4_22_V, TI_REV_9M_22_V)


def test_insulet_quarter_ladder_picks_each_rung_by_period(conn: Any) -> None:
    """Insulet has Q2, H1, Q3, 9M, Q4 and FY rows for revenue; Q2 2021 must be the 263.2M quarter,
    not H1 (515.5M), 9M (791.1M) or FY (1,098.8M). The multi-tag resolution is exercised too."""
    res = one(run(conn, ["revenue"], [INSULET], [(2021, "Q2")]))
    assert res.value == INS_REVT_Q2_21_V and res.fact_ids == [INS_REVT_Q2_21]
    assert res.value not in (D("515500000"), D("791100000"), D("1098800000"))


def test_q4_returns_the_derived_row_with_a_q4_derived_note(conn: Any) -> None:
    """Q4 is the derived row (FY - 9M, qtrs = 1): TI Q4 2022 revenue 4.67B cites that fact and
    carries a q4_derived note so the composer says it is computed, not reported."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "Q4")]))
    assert res.value == TI_REV_Q4_22_V and res.fact_ids == [TI_REV_Q4_22]
    assert res.fiscal_period == "Q4"
    assert len(note(res, "q4_derived")) == 1 and note(res, "restated") == []


def test_a_derived_q4_per_share_amount_is_never_returned(conn: Any) -> None:
    """FY minus 9M is not valid for a per-share amount (05 2.2 excludes per-share facts from the
    derived Q4), but the lake still holds such rows (TI Q4 2022 EPS in the real data) because FSDS
    records per-share facts with uom USD and the exclusion looks for a '/' in the uom. The
    compiler must not hand the composer a number the arithmetic cannot support: NotFound, never
    a value. (The upstream data bug is logged for the pipeline owner.)"""
    nf = only_not_found(run(conn, ["eps_diluted"], [TI], [(2022, "Q4")]))
    assert nf.reason == "no_fact" and nf.cik == TI and nf.fiscal_period == "Q4"


def test_fiscal_year_is_the_facts_fiscal_year(conn: Any) -> None:
    """Dollar Tree's FY2023 is the year that ended in early 2024; the result reports the facts'
    fiscal_year (2023), never the filing year (M1 bug, 05 4.5 `fiscal_year`)."""
    res = one(run(conn, ["net_income"], [DT], [(2023, "FY")]))
    assert res.fiscal_year == 2023 and res.value == DT_NI_FY23_V == D("-998400000")
    assert res.value < 0, "a loss keeps its sign"


def test_quarter_with_no_fact_is_not_found_not_interpolated(conn: Any) -> None:
    """TI has no Q1 fact in the fixture. The answer is NotFound(no_fact) for exactly that
    (metric, cik, period): never a fill from Q2, H1 or the FY (the compiler never fills a gap)."""
    nf = only_not_found(run(conn, ["revenue"], [TI], [(2022, "Q1")]))
    assert (nf.metric, nf.cik, nf.fiscal_year, nf.fiscal_period) == ("revenue", TI, 2022, "Q1")
    assert nf.reason == "no_fact"


def test_period_beyond_the_data_is_not_found(conn: Any) -> None:
    """A fiscal year with no rows at all (2030) is NotFound(no_fact), not an error."""
    nf = only_not_found(run(conn, ["revenue"], [TI], [(2030, "FY")]))
    assert nf.reason == "no_fact" and nf.fiscal_year == 2030


def test_metric_the_company_never_reports_is_not_found(conn: Any) -> None:
    """Dollar Tree has no research-and-development fact: NotFound(no_fact), nothing substituted
    from SG&A or any other tag."""
    nf = only_not_found(run(conn, ["r_and_d"], [DT], [(2022, "FY")]))
    assert nf.reason == "no_fact" and nf.metric == "r_and_d" and nf.cik == DT


def test_each_requested_cell_gets_exactly_one_outcome(conn: Any) -> None:
    """Two metrics x two companies, one cell unsupported: three results and one NotFound; the
    outcome partitions the request, nothing is silently dropped or duplicated."""
    out = run(conn, ["revenue", "r_and_d"], [TI, DT], [(2022, "FY")])
    cells = {(r.metric, r.cik) for r in out.results} | {(n.metric, n.cik) for n in out.not_found}
    assert cells == {("revenue", TI), ("revenue", DT), ("r_and_d", TI), ("r_and_d", DT)}
    assert len(out.results) + len(out.not_found) == 4
    assert {(n.metric, n.cik) for n in out.not_found} == {("r_and_d", TI), ("r_and_d", DT)}


# ======================================================================
# 2. Multi-tag metrics (ADR-043 as ADR-046 narrows it): agree -> gross-profit identity -> abstain
# ======================================================================


def test_tags_that_agree_exactly_are_one_number_citing_the_first_tag(conn: Any) -> None:
    """TI FY2021 cost of revenue is 5,968M under both CostOfRevenue and CostOfGoodsAndServicesSold.
    One number (candidates_agree); the chain order only picks which tag to cite: CostOfRevenue."""
    res = one(run(conn, ["cost_of_revenue"], [TI], [(2021, "FY")]))
    assert res.value == TI_COST_FY21_V and res.resolution == "candidates_agree"
    assert res.fact_ids == [TI_COST_C1_FY21] and res.fact_ids != [TI_COST_C2_FY21]
    assert res.kind == "reported"


@pytest.mark.parametrize(
    ("period", "fid", "value"),
    [
        ((2021, "FY"), DT_REVC_FY21, DT_REVC_FY21_V),  # 26,309.8M vs 26,321.2M: 0.043%
        ((2022, "FY"), DT_REVC_FY22, DT_REVC_FY22_V),  # 28,318.2M vs 28,331.7M: 0.048%
        ((2022, "Q2"), DT_REVC_Q2_22, DT_REVC_Q2_22_V),  # 6,765.3M vs 6,768.5M: 0.047%
    ],
)
def test_tags_within_four_significant_figures_agree(
    conn: Any, period: Period, fid: int, value: Decimal
) -> None:
    """Dollar Tree's two revenue tags differ by under 0.05% (eval.gen.numeric_policy.agree: 4
    significant figures): candidates_agree, and the value is the first tag's own number, not the
    larger one and not an average."""
    res = one(run(conn, ["revenue"], [DT], [period]))
    assert res.resolution == "candidates_agree"
    assert res.value == value and res.fact_ids == [fid]


@pytest.mark.parametrize("period", [(2023, "FY"), (2023, "Q2")])
def test_tags_just_outside_tolerance_with_nothing_to_settle_it_abstain(
    conn: Any, period: Period
) -> None:
    """Dollar Tree FY2023 / Q2 2023: the tags differ by 0.072% / 0.071% (just outside 4 sig figs),
    there is no GrossProfit fact and no way to check the identity, so NotFound with
    conflicting_candidates. ADR-046: Revenues is NOT assumed to be the total (ADR-043 step 6 is not
    applied), and the first tag does not win."""
    nf = only_not_found(run(conn, ["revenue"], [DT], [period]))
    assert nf.reason == "conflicting_candidates" and nf.metric == "revenue" and nf.cik == DT
    assert (nf.fiscal_year, nf.fiscal_period) == (2023, period[1])


def test_conflict_settled_by_gross_profit_identity_beats_chain_order(conn: Any) -> None:
    """Inogen FY2021: contract revenue 311.73M, Revenues 358.003M. Only Revenues satisfies
    revenue - cost = GrossProfit (358.003M - 181.52M = 176.483M, reported). The result is
    Revenues with resolution reconciles_with_gross_profit; 'first tag wins' would return 311.73M."""
    res = one(run(conn, ["revenue"], [INOGEN], [(2021, "FY")]))
    assert res.value == IN_REVT_FY21_V and res.value != IN_REVC_FY21_V
    assert res.fact_ids == [IN_REVT_FY21] and res.resolution == "reconciles_with_gross_profit"
    assert res.kind == "reported"


def test_cost_conflict_is_settled_by_the_identity_too(conn: Any) -> None:
    """Inogen cost of revenue FY2021 (181.52M vs 161.824M): the value that closes the identity
    against revenue and GrossProfit wins; the rule applies to cost_of_revenue as to revenue."""
    res = one(run(conn, ["cost_of_revenue"], [INOGEN], [(2021, "FY")]))
    assert res.value == IN_COST_C1_FY21_V and res.fact_ids == [IN_COST_C1_FY21]
    assert res.resolution == "reconciles_with_gross_profit"


@pytest.mark.parametrize(
    ("period", "fid", "value"),
    [
        ((2020, "FY"), IN_REVT_FY20, IN_REVT_FY20_V),
        ((2022, "FY"), IN_REVT_FY22, IN_REVT_FY22_V),
        ((2021, "Q2"), IN_REVT_Q2_21, IN_REVT_Q2_21_V),
    ],
)
def test_identity_settles_other_inogen_periods(
    conn: Any, period: Period, fid: int, value: Decimal
) -> None:
    """The identity is evaluated per (company, period), annual and quarterly alike."""
    res = one(run(conn, ["revenue"], [INOGEN], [period]))
    assert (res.value, res.fact_ids, res.resolution) == (
        value,
        [fid],
        "reconciles_with_gross_profit",
    )


@pytest.mark.parametrize(
    ("period", "fid", "value"),
    [
        ((2022, "FY"), INS_REVT_FY22, INS_REVT_FY22_V),
        ((2021, "Q2"), INS_REVT_Q2_21, INS_REVT_Q2_21_V),
        ((2021, "Q3"), INS_REVT_Q3_21, INS_REVT_Q3_21_V),
        ((2022, "Q2"), INS_REVT_Q2_22, INS_REVT_Q2_22_V),
    ],
)
def test_insulet_conflicts_settled_by_identity(
    conn: Any, period: Period, fid: int, value: Decimal
) -> None:
    """Insulet's contract revenue (1,055.4M FY2022) and Revenues (1,305.3M) conflict; Revenues -
    COGS = GrossProfit (1,305.3 - 499.7 = 805.6M), so Revenues wins, against the chain order."""
    res = one(run(conn, ["revenue"], [INSULET], [period]))
    assert (res.value, res.fact_ids) == (value, [fid])
    assert res.resolution == "reconciles_with_gross_profit"


def test_a_derived_q4_with_the_total_below_its_component_abstains(conn: Any) -> None:
    """Insulet Q4 2021 (derived rows): `Revenues` is 307.7M, under contract revenue's original
    327.0M. The identity would close on `Revenues` (307.7 - 94.5 = 213.2 = GrossProfit), but
    ADR-043 abstains on a total below its component before any other rule, as the gold resolver
    does (`total_below_component`).

    CHANGED 2026-11-25 (ADR-047): this test first expected the identity to pick `Revenues`, which
    contradicted the ADR; the pre-commit review found the guard missing."""
    nf = only_not_found(run(conn, ["revenue"], [INSULET], [(2021, "Q4")]))
    assert nf.reason == "conflicting_candidates" and "below" in nf.detail


def test_conflict_with_no_cost_or_gross_profit_to_check_abstains(conn: Any) -> None:
    """Inogen FY2023 revenue: 251.6M vs 315.7M, and the fixture holds no cost or GrossProfit for
    FY2023, so the identity cannot be evaluated: NotFound(conflicting_candidates)."""
    nf = only_not_found(run(conn, ["revenue"], [INOGEN], [(2023, "FY")]))
    assert nf.reason == "conflicting_candidates"


def test_chain_with_one_populated_tag_is_a_single_candidate(conn: Any) -> None:
    """Aflac populates only `Revenues`: single_candidate, no conflict machinery needed."""
    res = one(run(conn, ["revenue"], [AFLAC], [(2020, "FY")]))
    assert res.resolution == "single_candidate" and res.value == AF_REV_FY20_V
    assert res.fact_ids == [AF_REV_FY20]


def test_insulet_agreeing_tags_cite_the_first_even_when_it_is_restated(conn: Any) -> None:
    """Insulet FY2021: the ORIGINAL contract revenue (1,098.8M) equals Revenues (1,098.8M), so the
    tags agree and the first tag is cited. Its restated latest value (1,040.6M, a different row)
    appears only as a restated note on that cited fact."""
    res = one(run(conn, ["revenue"], [INSULET], [(2021, "FY")]))
    assert res.resolution == "candidates_agree" and res.value == INS_REVC_FY21_ORIG_V
    assert res.fact_ids == [INS_REVC_FY21_ORIG]
    (n,) = note(res, "restated")
    assert (n.latest_value, n.latest_fact_id) == (INS_REVC_FY21_LATEST_V, INS_REVC_FY21_LATEST)
    assert INS_REVC_FY21_LATEST not in res.fact_ids


def test_identical_tags_on_both_sides_still_agree(conn: Any) -> None:
    """Insulet FY2020: both tags are exactly 904.4M: candidates_agree, first tag cited."""
    res = one(run(conn, ["revenue"], [INSULET], [(2020, "FY")]))
    assert res.resolution == "candidates_agree" and res.value == INS_REV_FY20_V
    assert res.fact_ids == [INS_REVC_FY20]


# ======================================================================
# 3. Formula metrics
# ======================================================================


def test_gross_margin_with_reported_gross_profit(conn: Any) -> None:
    """TI FY2022 gross_margin = GrossProfit / revenue = 13,771 / 20,028 as an exact Decimal
    fraction (not a percentage), unit 'ratio', kind derived, expr over the two leaf facts."""
    res = one(run(conn, ["gross_margin"], [TI], [(2022, "FY")]))
    assert res.value == D(13771000000) / D(20028000000)
    assert res.unit == "ratio" and res.kind == "derived"
    assert res.expr == f"f:{TI_GP_FY22} / f:{TI_REV_FY22}"
    assert set(res.fact_ids) == {TI_GP_FY22, TI_REV_FY22}


def test_operating_margin_uses_the_settled_revenue(conn: Any) -> None:
    """Insulet FY2022 operating_margin = 37.6M / 1,305.3M: the denominator is the revenue resolved
    by the identity (Revenues), not the first tag (1,055.4M)."""
    res = one(run(conn, ["operating_margin"], [INSULET], [(2022, "FY")]))
    assert res.value == D(37600000) / D(1305300000) != D(37600000) / INS_REVC_FY22_V
    assert res.expr == f"f:{INS_OPI_FY22} / f:{INS_REVT_FY22}"


def test_margin_is_negative_not_clamped(conn: Any) -> None:
    """Inogen FY2022 operating margin = -85.243M / 377.241M = -22.6%: a loss is a negative
    fraction, never clamped to zero (eval policy: margins are never clamped)."""
    res = one(run(conn, ["operating_margin"], [INOGEN], [(2022, "FY")]))
    assert res.value == IN_OPI_FY22_V / IN_REVT_FY22_V and res.value < 0


def test_formula_with_a_missing_input_is_missing_input(conn: Any) -> None:
    """Aflac reports no OperatingIncomeLoss at all, so its operating_margin is
    NotFound(missing_input) naming the operand; the compiler does not substitute net income."""
    nf = only_not_found(run(conn, ["operating_margin"], [AFLAC], [(2021, "FY")]))
    assert nf.reason == "missing_input" and nf.metric == "operating_margin"
    assert nf.missing_inputs == ["operating_income"]


def test_formula_whose_revenue_input_conflicts_is_not_computed(conn: Any) -> None:
    """ASSUMPTION (which reason): Dollar Tree FY2023 operating_margin needs revenue, whose tags
    conflict with no way to settle it. No value may come back; the NotFound is for the requested
    metric. The contract does not say whether it is `missing_input` (revenue named in
    missing_inputs) or `conflicting_candidates`; either is accepted here, a value is not."""
    nf = only_not_found(run(conn, ["operating_margin"], [DT], [(2023, "FY")]))
    assert nf.metric == "operating_margin" and nf.cik == DT
    assert nf.reason in ("missing_input", "conflicting_candidates")
    if nf.reason == "missing_input":
        assert nf.missing_inputs == ["revenue"]


def test_formula_division_by_zero_is_a_not_found(conn: Any) -> None:
    """A zero divisor gives NotFound(division_by_zero), never an exception or a float inf. Uses a
    custom catalog (the `catalog` argument) whose net_margin divides by revenue - revenue."""
    cat = dict(load_catalog())
    cat["net_margin"] = MetricDef(unit="ratio", formula="net_income / (revenue - revenue)")
    nf = only_not_found(run(conn, ["net_margin"], [TI], [(2022, "FY")], catalog=cat))
    assert nf.reason == "division_by_zero" and nf.metric == "net_margin"


def test_ratio_operation_on_a_ratio_metric_is_a_value(conn: Any) -> None:
    """`ratio` on a ratio-unit metric is treated as `value`: same number, operation 'value'."""
    as_ratio = one(run(conn, ["operating_margin"], [TI], [(2022, "FY")], "ratio"))
    as_value = one(run(conn, ["operating_margin"], [TI], [(2022, "FY")], "value"))
    assert as_ratio.operation == "value" and as_ratio.value == as_value.value
    assert as_ratio.value == D(10140000000) / D(20028000000)
    assert as_ratio.expr == as_value.expr


def test_nested_expression_cites_each_leaf_and_evaluates(conn: Any) -> None:
    """ASSUMPTION: the derived_fallback note propagates to a formula that consumes the fallback
    (the contract only says the fallback RESULT carries it). A formula over a fallback formula
    (Dollar Tree FY2021 gross_margin = (revenue - cost) / revenue) gets a nested expr that parses
    under the 4.3 grammar, evaluates to the Decimal value, and whose tokens are exactly
    {revenue fact, cost fact} (the negative cost id is one token)."""
    res = one(run(conn, ["gross_margin"], [DT], [(2021, "FY")]))
    assert set(res.fact_ids) == {DT_REVC_FY21, DT_COGS_FY21}
    assert res.value == (DT_REVC_FY21_V - DT_COGS_FY21_V) / DT_REVC_FY21_V
    assert res.kind == "derived" and "(" in res.expr
    assert len(note(res, "derived_fallback")) == 1


# ======================================================================
# 4. gross_profit: reported tag first, fallback formula only under only_if
# ======================================================================


def test_gross_profit_reported_is_a_reported_fact(conn: Any) -> None:
    """When GrossProfit is filed (Inogen FY2021) it is returned as a reported fact; the fallback
    formula is not used even though revenue - cost could be computed (fallback only applies when
    NO tag has a value)."""
    res = one(run(conn, ["gross_profit"], [INOGEN], [(2021, "FY")]))
    assert res.kind == "reported" and res.value == IN_GP_FY21_V
    assert res.fact_ids == [IN_GP_FY21] and res.resolution == "single_candidate"
    assert note(res, "derived_fallback") == []


@pytest.mark.parametrize(
    ("period", "rev_id", "cost_id", "rev", "cost"),
    [
        ((2021, "FY"), DT_REVC_FY21, DT_COGS_FY21, DT_REVC_FY21_V, DT_COGS_FY21_V),
        ((2022, "FY"), DT_REVC_FY22, DT_COGS_FY22, DT_REVC_FY22_V, DT_COGS_FY22_V),
    ],
)
def test_gross_profit_fallback_is_derived_and_labelled(
    conn: Any, period: Period, rev_id: int, cost_id: int, rev: Decimal, cost: Decimal
) -> None:
    """Dollar Tree reports no GrossProfit: revenue - cost_of_revenue is used under the catalog's
    only_if conditions, labelled kind 'derived' with a must_mention derived_fallback note, and
    cites both leaf facts (the cost id is negative: `f:A - f:-B` is a subtraction of two facts)."""
    res = one(run(conn, ["gross_profit"], [DT], [period]))
    assert res.value == rev - cost and res.kind == "derived" and res.unit == "USD"
    assert set(res.fact_ids) == {rev_id, cost_id}
    (n,) = note(res, "derived_fallback")
    assert n.must_mention is True and n.text


def test_gross_profit_fallback_refuses_when_an_input_conflicts(conn: Any) -> None:
    """ASSUMPTION (which reason): Dollar Tree FY2023 revenue conflicts, so the fallback's
    `inputs_verified` condition fails. No gross profit is invented; the NotFound is for
    gross_profit with one of the reasons a missing/unresolved input can take."""
    nf = only_not_found(run(conn, ["gross_profit"], [DT], [(2023, "FY")]))
    assert nf.metric == "gross_profit"
    assert nf.reason in ("missing_input", "conflicting_candidates", "fallback_not_allowed")


def test_gross_profit_fallback_with_a_missing_operand(conn: Any) -> None:
    """Insulet FY2020: no GrossProfit and no cost fact in the fixture (revenue exists). The
    fallback cannot run: NotFound, naming cost_of_revenue when the reason is missing_input
    (ASSUMPTION: `no_fact` is also acceptable, the contract leaves it open)."""
    nf = only_not_found(run(conn, ["gross_profit"], [INSULET], [(2020, "FY")]))
    assert nf.reason in ("missing_input", "no_fact")
    if nf.reason == "missing_input":
        assert nf.missing_inputs == ["cost_of_revenue"]


def test_fallback_without_only_if_is_never_used(conn: Any) -> None:
    """A catalog whose gross_profit has a fallback_formula but no `only_if` must not use it:
    Dollar Tree FY2021 is NotFound(fallback_not_allowed) (05 4.2: 'a fallback with no only_if is
    never used')."""
    cat = dict(load_catalog())
    cat["gross_profit"] = MetricDef(
        unit="USD",
        period_type="duration",
        tags=["GrossProfit"],
        fallback_formula="revenue - cost_of_revenue",
    )
    nf = only_not_found(run(conn, ["gross_profit"], [DT], [(2021, "FY")], catalog=cat))
    assert nf.reason == "fallback_not_allowed" and nf.metric == "gross_profit"


# ======================================================================
# 5. Restated facts: the original value is the answer; the latest is a note
# ======================================================================


def test_restated_revenue_returns_the_original_and_notes_the_latest(conn: Any) -> None:
    """Aflac FY2021 revenue was restated (22,106M -> 21,554M). The default path returns the
    as-originally-filed 22,106M (ADR-017), cites the ORIGINAL fact id, and carries a must_mention
    'restated' note with latest_value 21,554M and latest_fact_id of the restated row."""
    res = one(run(conn, ["revenue"], [AFLAC], [(2021, "FY")]))
    assert res.value == AF_REV_FY21_V != AF_REV_FY21_LATEST_V
    assert res.expr == f"f:{AF_REV_FY21}" and res.fact_ids == [AF_REV_FY21]
    (n,) = note(res, "restated")
    assert n.must_mention is True and n.text
    assert (n.latest_value, n.latest_fact_id) == (AF_REV_FY21_LATEST_V, AF_REV_FY21_LATEST)
    assert AF_REV_FY21_LATEST not in res.fact_ids


def test_restated_note_works_for_each_kind_of_fact(conn: Any) -> None:
    """Net income (duration), EPS (per share, uom USD) and total assets (instant) all return the
    original value and a restated note pointing at the latest row."""
    ni = one(run(conn, ["net_income"], [AFLAC], [(2021, "FY")]))
    eps = one(run(conn, ["eps_diluted"], [AFLAC], [(2021, "FY")]))
    ast_ = one(run(conn, ["total_assets"], [AFLAC], [(2022, "FY")]))
    for res, orig, latest_id, latest_v in (
        (ni, AF_NI_FY21_V, AF_NI_FY21_LATEST, AF_NI_FY21_LATEST_V),
        (eps, AF_EPS_FY21_V, AF_EPS_FY21_LATEST, AF_EPS_FY21_LATEST_V),
        (ast_, AF_AST_FY22_V, AF_AST_FY22_LATEST, AF_AST_FY22_LATEST_V),
    ):
        assert res.value == orig
        (n,) = note(res, "restated")
        assert (n.latest_value, n.latest_fact_id) == (latest_v, latest_id)


def test_unrestated_fact_has_no_notes(conn: Any) -> None:
    """Aflac FY2020 was never restated: no notes at all."""
    res = one(run(conn, ["net_income"], [AFLAC], [(2020, "FY")]))
    assert res.value == AF_NI_FY20_V and res.notes == []


def test_restated_instant_share_count_at_a_quarter_end(conn: Any) -> None:
    """Dollar Tree's Q2 2022 share count was restated (223.9M -> 223,932,187): the original
    (is_original) is returned, with the note."""
    res = one(run(conn, ["shares_outstanding"], [DT], [(2022, "Q2")]))
    assert res.value == DT_SHS_Q2_22_ORIG_V and res.fact_ids == [DT_SHS_Q2_22_ORIG]
    (n,) = note(res, "restated")
    assert (n.latest_value, n.latest_fact_id) == (DT_SHS_Q2_22_LATEST_V, DT_SHS_Q2_22_LATEST)


def test_derived_q4_that_is_also_restated_gets_both_notes(conn: Any) -> None:
    """Aflac Q4 2021 revenue is a derived Q4 whose FY parent was restated, so the row has an
    original (5,436M) and a latest (4,884M) version: both a q4_derived and a restated note."""
    res = one(run(conn, ["revenue"], [AFLAC], [(2021, "Q4")]))
    assert res.value == AF_REV_Q4_21_V and res.fact_ids == [AF_REV_Q4_21]
    assert len(note(res, "q4_derived")) == 1
    (n,) = note(res, "restated")
    assert (n.latest_value, n.latest_fact_id) == (AF_REV_Q4_21_LATEST_V, AF_REV_Q4_21_LATEST)


def test_formula_over_restated_inputs_notes_every_restated_leaf(conn: Any) -> None:
    """ASSUMPTION (contract is silent on formula results): Aflac FY2021 net_margin = original net
    income / original revenue, both restated. Each restated leaf yields its own restated note, so
    the composer can say both numbers were later revised."""
    res = one(run(conn, ["net_margin"], [AFLAC], [(2021, "FY")]))
    assert res.value == AF_NI_FY21_V / AF_REV_FY21_V
    latest = {n.latest_fact_id: n.latest_value for n in note(res, "restated")}
    assert latest == {
        AF_NI_FY21_LATEST: AF_NI_FY21_LATEST_V,
        AF_REV_FY21_LATEST: AF_REV_FY21_LATEST_V,
    }
    assert set(res.fact_ids) == {AF_NI_FY21, AF_REV_FY21}


def test_is_restated_row_without_a_distinct_latest_version_does_not_crash(conn: Any) -> None:
    """Real-data oddity: Dollar Tree FY2021 shares has is_restated = true but a SINGLE row (both
    is_latest and is_original). There is no separate latest fact to cite, so the compiler must
    return the value and may only emit a restated note whose latest_fact_id is a different,
    existing fact (a Note without a latest row cannot be valid). ASSUMPTION: no note is the
    expected behavior; this pins 'no crash, no self-referencing note'."""
    res = one(run(conn, ["shares_outstanding"], [DT], [(2021, "FY")]))
    assert res.value == DT_SHS_FY21_V and res.fact_ids == [DT_SHS_FY21]
    for n in note(res, "restated"):
        assert n.latest_fact_id != DT_SHS_FY21 and n.latest_fact_id in VALUE_OF


# ======================================================================
# 6. Operations: growth, compare, trend, rank, ratio
# ======================================================================


def test_growth_year_over_year_with_a_positive_base(conn: Any) -> None:
    """ASSUMPTION A1 (one period = year over year): growth for TI FY2022 revenue is measured
    against FY2021 (same fiscal period, one year earlier): (20,028 - 18,344) / 18,344, with
    expr exactly '(f:t - f:p) / f:p' (05 4.5), operation growth, unit ratio, kind derived."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")], "growth"))
    assert res.operation == "growth" and res.unit == "ratio" and res.kind == "derived"
    assert res.expr == f"(f:{TI_REV_FY22} - f:{TI_REV_FY21}) / f:{TI_REV_FY21}"
    assert set(res.fact_ids) == {TI_REV_FY22, TI_REV_FY21}
    assert res.value == (TI_REV_FY22_V - TI_REV_FY21_V) / TI_REV_FY21_V
    assert (res.fiscal_year, res.fiscal_period) == (2022, "FY")


def test_growth_can_be_negative_when_the_base_is_positive(conn: Any) -> None:
    """Insulet Q3 2022 net income (-5.2M) against Q3 2021 (+12.6M): base positive, so growth is
    computed and negative (-141.3%); a falling number is not a refusal."""
    res = one(run(conn, ["net_income"], [INSULET], [(2022, "Q3")], "growth"))
    assert res.value == (INS_NI_Q3_22_V - INS_NI_Q3_21_V) / INS_NI_Q3_21_V and res.value < -1
    assert res.expr == f"(f:{INS_NI_Q3_22} - f:{INS_NI_Q3_21}) / f:{INS_NI_Q3_21}"


@pytest.mark.parametrize(
    ("metric", "cik", "period"),
    [
        ("operating_income", INOGEN, (2021, "FY")),  # base FY2020 = -12.025M
        ("net_income", INSULET, (2022, "Q2")),  # base Q2 2021 = -25.0M
    ],
)
def test_growth_with_a_non_positive_base_is_refused(
    conn: Any, metric: str, cik: int, period: Period
) -> None:
    """A base <= 0 makes growth meaningless: NotFound(growth_base_nonpositive) for the requested
    (metric, cik, period), no number returned (M05: 'refuse when the base <= 0')."""
    nf = only_not_found(run(conn, [metric], [cik], [period], "growth"))
    assert nf.reason == "growth_base_nonpositive" and nf.metric == metric and nf.cik == cik
    assert (nf.fiscal_year, nf.fiscal_period) == period


@pytest.mark.parametrize(
    ("metric", "cik", "period"),
    [
        ("revenue", TI, (2021, "FY")),  # FY2020 is not in the data
        ("net_income", INSULET, (2022, "FY")),  # FY2021 net income is not in the data
    ],
)
def test_growth_with_a_missing_base_is_refused_not_estimated(
    conn: Any, metric: str, cik: int, period: Period
) -> None:
    """The prior period has no fact: NotFound(growth_base_missing). The base is never estimated
    from a neighbouring period or a different tag."""
    nf = only_not_found(run(conn, [metric], [cik], [period], "growth"))
    assert nf.reason == "growth_base_missing" and nf.metric == metric and nf.cik == cik


def test_growth_over_multi_tag_revenue_uses_each_period_resolution(conn: Any) -> None:
    """Insulet FY2022 vs FY2021 revenue: current = Revenues (settled by the identity, 1,305.3M),
    base = the first tag's ORIGINAL FY2021 value (1,098.8M, agrees with Revenues). The restated
    base shows up as a restated note on the growth result (latest 1,040.6M)."""
    res = one(run(conn, ["revenue"], [INSULET], [(2022, "FY")], "growth"))
    assert res.value == (INS_REVT_FY22_V - INS_REVC_FY21_ORIG_V) / INS_REVC_FY21_ORIG_V
    assert set(res.fact_ids) == {INS_REVT_FY22, INS_REVC_FY21_ORIG}
    assert INS_REVC_FY21_LATEST in {n.latest_fact_id for n in note(res, "restated")}


@pytest.mark.parametrize("order", [[(2021, "FY"), (2022, "FY")], [(2022, "FY"), (2021, "FY")]])
def test_growth_with_two_periods_is_later_over_earlier_in_either_order(
    conn: Any, order: list[Period]
) -> None:
    """ASSUMPTION A2: growth over two named periods is (later - earlier) / earlier whatever order
    the planner listed them in (the plan carries no 'from/to' marker). The contract only says
    growth = (v_t - v_{t-1}) / v_{t-1}; if Shekar decides the first period is the base, change
    this test."""
    res = one(run(conn, ["net_income"], [TI], order, "growth"))
    assert res.value == (TI_NI_FY22_V - TI_NI_FY21_V) / TI_NI_FY21_V
    assert (res.fiscal_year, res.fiscal_period) == (2022, "FY")
    assert res.expr == f"(f:{TI_NI_FY22} - f:{TI_NI_FY21}) / f:{TI_NI_FY21}"


def test_growth_uses_original_versions_and_notes_the_restated_one(conn: Any) -> None:
    """Aflac FY2021 net income growth vs FY2020: uses the as-filed 4,325M (not the restated
    4,231M) and notes the restatement of that input."""
    res = one(run(conn, ["net_income"], [AFLAC], [(2021, "FY")], "growth"))
    assert res.value == (AF_NI_FY21_V - AF_NI_FY20_V) / AF_NI_FY20_V
    assert AF_NI_FY21_LATEST in {n.latest_fact_id for n in note(res, "restated")}


def test_compare_two_companies_adds_one_difference_first_minus_second(conn: Any) -> None:
    """compare = one `value` result per company plus, for exactly two companies, one `difference`
    result = first minus second, compare_cik = the second company, expr over the two facts."""
    out = run(conn, ["net_income"], [TI, DT], [(2022, "FY")], "compare")
    assert len(out.results) == 3 and not out.not_found
    ti = pick(out, cik=TI)
    dt = pick(out, cik=DT)
    assert (ti.value, dt.value) == (TI_NI_FY22_V, DT_NI_FY22_V)
    diff = pick(out, op="difference", cik=TI)
    assert diff.compare_cik == DT and diff.value == TI_NI_FY22_V - DT_NI_FY22_V
    assert set(diff.fact_ids) == {TI_NI_FY22, DT_NI_FY22} and diff.kind == "derived"
    assert diff.unit == "USD"


def test_compare_difference_follows_company_order(conn: Any) -> None:
    """Swapping the two companies flips the sign: the difference is always plan-first minus
    plan-second."""
    a = pick(run(conn, ["net_income"], [TI, DT], [(2022, "FY")], "compare"), op="difference")
    b = pick(run(conn, ["net_income"], [DT, TI], [(2022, "FY")], "compare"), op="difference")
    assert a.value == -b.value and a.cik == TI and b.cik == DT


def test_compare_three_companies_has_no_difference(conn: Any) -> None:
    """A difference exists only for exactly two companies; three companies get three values."""
    out = run(conn, ["net_income"], [TI, DT, AFLAC], [(2022, "FY")], "compare")
    assert [r.operation for r in out.results] == ["value"] * 3
    assert {r.cik for r in out.results} == {TI, DT, AFLAC}


def test_compare_with_one_company_missing_has_no_difference(conn: Any) -> None:
    """Aflac reports no operating income: the TI value is returned, Aflac is a NotFound, and no
    difference is fabricated from a missing side."""
    out = run(conn, ["operating_income"], [TI, AFLAC], [(2022, "FY")], "compare")
    assert [r.operation for r in out.results] == ["value"] and out.results[0].cik == TI
    assert [(n.cik, n.reason) for n in out.not_found] == [(AFLAC, "no_fact")]


def test_trend_is_one_value_per_period_and_never_fills_the_gap(conn: Any) -> None:
    """trend over Q1, Q2 and Q4 of 2022 for TI revenue: Q2 and the derived Q4 are values, Q1 has no
    fact, so it is a NotFound rather than an interpolation."""
    out = run(conn, ["revenue"], [TI], [(2022, "Q1"), (2022, "Q2"), (2022, "Q4")], "trend")
    got = {(r.fiscal_year, r.fiscal_period): r.value for r in out.results}
    assert got == {(2022, "Q2"): TI_REV_Q2_22_V, (2022, "Q4"): TI_REV_Q4_22_V}
    assert [(n.fiscal_period, n.reason) for n in out.not_found] == [("Q1", "no_fact")]
    for r in out.results:
        check_provenance(r)


def test_trend_over_fiscal_years(conn: Any) -> None:
    """trend over FY2021 and FY2022 net income for TI: two value results, one per period."""
    out = run(conn, ["net_income"], [TI], [(2021, "FY"), (2022, "FY")], "trend")
    assert {(r.fiscal_year, r.value) for r in out.results} == {
        (2021, TI_NI_FY21_V),
        (2022, TI_NI_FY22_V),
    }


def test_rank_over_named_companies_is_ordered_best_first(conn: Any) -> None:
    """rank over the named companies, FY2022 net income: TI 8.749B (rank 1), Aflac 4.201B (the
    as-filed value, rank 2), Dollar Tree 1.615B, Insulet 4.6M; Inogen has no FY2022 net income in
    the data and is a NotFound. `rank` starts at 1 = highest value."""
    out = run(conn, ["net_income"], [INSULET, AFLAC, DT, TI, INOGEN], [(2022, "FY")], "rank")
    ordered = sorted(out.results, key=lambda r: r.rank or 0)
    assert [(r.rank, r.cik, r.value) for r in ordered] == [
        (1, TI, TI_NI_FY22_V),
        (2, AFLAC, AF_NI_FY22_V),
        (3, DT, DT_NI_FY22_V),
        (4, INSULET, D("4600000")),
    ]
    assert all(r.operation == "value" for r in ordered)
    assert [(n.cik, n.reason) for n in out.not_found] == [(INOGEN, "no_fact")]
    for r in ordered:
        check_provenance(r)


def test_rank_over_a_universe_when_no_company_is_named(conn: Any) -> None:
    """With no companies named, rank ranges over `universe` (the tracked CIKs): only the CIKs in
    it appear (Insulet, outside it, does not), best first."""
    plan = make_plan(["net_income"], [], [(2022, "FY")], "rank")
    out = compile_plan(plan, conn, universe={TI, AFLAC, DT})
    ordered = sorted(out.results, key=lambda r: r.rank or 0)
    assert [(r.rank, r.cik) for r in ordered] == [(1, TI), (2, AFLAC), (3, DT)]
    assert INSULET not in {r.cik for r in out.results}


def test_rank_is_limited_to_ten(conn: Any, pg_url: str) -> None:
    """A universe of 17 companies yields at most 10 ranked rows (LIMIT 10), the top ten by value,
    ranks 1..10 without gaps. Uses a separate schema holding the real fixture rows PLUS 12
    SYNTHETIC net-income rows (clearly fake CIKs 9000000+, values 1B..12B) so there are enough
    companies to hit the limit."""
    base = next(r for r in RAW if r["fact_id"] == TI_NI_FY22)
    synthetic = []
    for i in range(12):
        row = dict(base)
        row.update(fact_id=7_000_000_000 + i, fact_key=7_000_000_000 + i, cik=9_000_000 + i)
        row["value"] = str(D(i + 1) * D(10) ** 9)
        synthetic.append(row)
    with psycopg.connect(pg_url, autocommit=True, options="-c search_path=rank_limit") as c:
        c.execute("CREATE SCHEMA IF NOT EXISTS rank_limit")
        c.execute("DROP TABLE IF EXISTS rank_limit.facts_v1 CASCADE")
        load_into(c, RAW + synthetic)
        universe = {r["cik"] for r in synthetic} | set(CIKS)
        out = compile_plan(
            make_plan(["net_income"], [], [(2022, "FY")], "rank"), c, universe=universe
        )
    expected = sorted(
        [D(r["value"]) for r in synthetic] + [TI_NI_FY22_V, AF_NI_FY22_V, DT_NI_FY22_V],
        reverse=True,
    )[:10]
    ordered = sorted(out.results, key=lambda r: r.rank or 0)
    assert [r.rank for r in ordered] == list(range(1, 11))
    assert [r.value for r in ordered] == expected


def test_several_metrics_in_one_plan(conn: Any) -> None:
    """Two metrics for one company/period give one result each, each with its own provenance."""
    out = run(conn, ["revenue", "net_income"], [TI], [(2022, "FY")])
    assert {r.metric for r in out.results} == {"revenue", "net_income"} and not out.not_found
    assert pick(out, metric="revenue").value == TI_REV_FY22_V
    assert pick(out, metric="net_income").value == TI_NI_FY22_V


# ======================================================================
# 7. UnsupportedPlan: the plan cannot be compiled at all
# ======================================================================


def test_numeric_plan_without_an_operation_is_unsupported(conn: Any) -> None:
    """A numeric plan with operation None raises UnsupportedPlan (with a reason string); it is not
    defaulted to `value`."""
    with pytest.raises(UnsupportedPlan) as exc:
        run(conn, ["revenue"], [TI], [(2022, "FY")], None)
    assert isinstance(exc.value.reason, str) and exc.value.reason


def test_plan_without_a_metric_is_unsupported(conn: Any) -> None:
    """No metric: nothing to compile."""
    with pytest.raises(UnsupportedPlan):
        run(conn, [], [TI], [(2022, "FY")])


def test_company_without_a_cik_is_unsupported(conn: Any) -> None:
    """A CompanyRef whose cik is still None (resolution failed upstream) cannot be compiled; the
    compiler never looks a company up by name."""
    with pytest.raises(UnsupportedPlan):
        run(conn, ["revenue"], [None], [(2022, "FY")])
    with pytest.raises(UnsupportedPlan):
        run(conn, ["revenue"], [TI, None], [(2022, "FY")])


def test_plan_without_a_period_is_unsupported(conn: Any) -> None:
    """No period: the planner (periods.py) must fill the default; the compiler does not guess."""
    with pytest.raises(UnsupportedPlan):
        run(conn, ["revenue"], [TI], [])


def test_ratio_on_a_non_ratio_metric_is_unsupported(conn: Any) -> None:
    """`ratio` is only meaningful for a ratio-unit metric (05 4.5); on revenue (USD) it raises."""
    with pytest.raises(UnsupportedPlan):
        run(conn, ["revenue"], [TI], [(2022, "FY")], "ratio")


def test_unsupported_plan_raises_before_any_partial_outcome(conn: Any) -> None:
    """One bad company among good ones still raises: the compiler does not return a partial
    outcome for a plan that is malformed."""
    with pytest.raises(UnsupportedPlan):
        run(conn, ["revenue"], [TI, None, DT], [(2022, "FY")], "compare")


# ======================================================================
# 8. Safety: catalog argument, parameterized SQL, consolidated view only
# ======================================================================


def test_catalog_argument_is_honored(conn: Any) -> None:
    """compile_plan reads the `catalog` it is given, not only the shipped file: a catalog mapping
    `revenue` to the GrossProfit tag returns TI's gross profit."""
    cat = dict(load_catalog())
    cat["revenue"] = MetricDef(unit="USD", period_type="duration", tags=["GrossProfit"])
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")], catalog=cat))
    assert res.value == TI_GP_FY22_V and res.fact_ids == [TI_GP_FY22]


def test_tag_text_is_bound_as_a_parameter_not_spliced(conn: Any) -> None:
    """A tag containing SQL (quote, semicolon, DROP TABLE) is just a tag that matches no row:
    NotFound(no_fact), and the table is still there afterwards (parameterized SQL)."""
    evil = "Revenues'; DROP TABLE facts_v1; --"
    cat = dict(load_catalog())
    cat["revenue"] = MetricDef(unit="USD", period_type="duration", tags=[evil])
    nf = only_not_found(run(conn, ["revenue"], [TI], [(2022, "FY")], catalog=cat))
    assert nf.reason == "no_fact"
    assert conn.execute("SELECT count(*) FROM facts_v1").fetchone()[0] == len(RAW)


def test_segmented_rows_are_never_read(conn: Any) -> None:
    """`facts_dimensional` holds 7x twins of every row (poison). Results come from the
    consolidated `facts` view only, so TI revenue is the real 20,028M, not 140,196M."""
    res = one(run(conn, ["revenue"], [TI], [(2022, "FY")]))
    assert res.value == TI_REV_FY22_V and res.value != TI_REV_FY22_V * 7
    assert res.fact_ids == [TI_REV_FY22]


def test_compile_does_not_modify_the_data(conn: Any) -> None:
    """The compiler is read-only: the fact count and a checksum of values are unchanged after a
    broad plan."""
    before = conn.execute(
        "SELECT count(*), sum(value), sum(fact_id::numeric) FROM facts"
    ).fetchone()
    run(conn, ["revenue", "gross_margin", "net_income"], CIKS, [(2022, "FY"), (2021, "Q2")])
    after = conn.execute("SELECT count(*), sum(value), sum(fact_id::numeric) FROM facts").fetchone()
    assert before == after


# ======================================================================
# 9. Provenance: every returned number re-executes from its fact ids
# ======================================================================

SWEEP_METRICS = [
    "revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income", "eps_diluted",
    "total_assets", "shares_outstanding", "gross_margin", "operating_margin", "net_margin",
]  # fmt: skip
SWEEP_PERIODS: list[Period] = [(2021, "FY"), (2022, "FY"), (2021, "Q2"), (2022, "Q2"), (2022, "Q4")]


def test_every_value_in_a_broad_sweep_re_executes_from_its_expr(conn: Any) -> None:
    """Across 5 companies x 11 metrics x 5 periods, every returned number (a) has at least one
    fact id, (b) cites only real is_original fixture facts, (c) has fact_ids == the f: tokens of
    its expr, (d) re-evaluates in Decimal to exactly `value`; every other cell is a NotFound, and
    nothing is counted twice. This is the verifier's provenance_exec contract."""
    out = run(conn, SWEEP_METRICS, CIKS, SWEEP_PERIODS)
    assert out.results, "the sweep must find numbers"
    for res in out.results:
        check_provenance(res)
    cells = len(SWEEP_METRICS) * len(CIKS) * len(SWEEP_PERIODS)
    assert len(out.results) + len(out.not_found) == cells
    seen = {(r.metric, r.cik, r.fiscal_year, r.fiscal_period) for r in out.results}
    seen |= {(n.metric, n.cik, n.fiscal_year, n.fiscal_period) for n in out.not_found}
    assert len(seen) == cells


def test_signed_fact_ids_survive_into_expr_and_provenance(conn: Any) -> None:
    """Negative ids are one token (`f:-8138443950304199831`): a growth over two negative-id facts
    and a fallback `f:<positive> - f:<negative>` both parse, round-trip through `provenance()` and
    evaluate (05 4.3, widened by ADR-046)."""
    g = one(run(conn, ["revenue"], [TI], [(2022, "FY")], "growth"))
    assert (
        TI_REV_FY21 < 0 and f"f:{TI_REV_FY21}" in g.expr and g.provenance().fact_ids == g.fact_ids
    )
    fb = one(run(conn, ["gross_profit"], [DT], [(2021, "FY")]))
    assert DT_REVC_FY21 > 0 > DT_COGS_FY21
    assert tokens(fb.expr) == {DT_REVC_FY21, DT_COGS_FY21}


def test_nothing_is_returned_without_a_fact_for_every_failure_mode(conn: Any) -> None:
    """Across the failure modes (no fact, conflict, missing input, base missing/non-positive) no
    NumericResult appears and every NotFound names a metric, a cik and a period."""
    cases: list[tuple[str, int, Period, str]] = [
        ("revenue", TI, (2030, "FY"), "value"),
        ("revenue", DT, (2023, "FY"), "value"),
        ("operating_margin", AFLAC, (2021, "FY"), "value"),
        ("revenue", TI, (2021, "FY"), "growth"),
        ("operating_income", INOGEN, (2021, "FY"), "growth"),
    ]
    for metric, cik, period, op in cases:
        nf = only_not_found(run(conn, [metric], [cik], [period], op))
        assert (nf.metric, nf.cik, (nf.fiscal_year, nf.fiscal_period)) == (metric, cik, period)
        assert nf.reason


# ======================================================================
# 10. Property tests (Hypothesis) over the real fixture
# ======================================================================

PROP = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
ALL_METRICS = sorted(load_catalog())
PERIODS = [(y, p) for y in (2021, 2022, 2023) for p in ("FY", "Q2", "Q4")] + [(2030, "FY")]


def normalized(out: CompileOutcome) -> tuple[list[str], list[str]]:
    return (
        sorted(r.model_dump_json() for r in out.results),
        sorted(n.model_dump_json() for n in out.not_found),
    )


@given(
    metric=st.sampled_from(
        ["revenue", "net_income", "gross_profit", "total_assets", "gross_margin"]
    ),
    period=st.sampled_from(PERIODS),
    companies=st.lists(st.sampled_from(CIKS), min_size=1, max_size=5, unique=True),
    data=st.data(),
)
@PROP
def test_property_value_outcome_is_independent_of_company_order(
    conn: Any, metric: str, period: Period, companies: list[int], data: st.DataObject
) -> None:
    """Order independence (the signature of first-wins bugs): the set of results and NotFounds for
    `value` does not depend on the order the companies are listed in, and the same plan compiled
    twice gives the same outcome."""
    shuffled = data.draw(st.permutations(companies))
    a = run(conn, [metric], companies, [period])
    b = run(conn, [metric], shuffled, [period])
    assert normalized(a) == normalized(b)
    assert normalized(a) == normalized(run(conn, [metric], companies, [period]))


@given(
    metric=st.sampled_from(ALL_METRICS),
    cik=st.sampled_from(CIKS),
    period=st.sampled_from(PERIODS),
    op=st.sampled_from(["value", "growth"]),
)
@PROP
def test_property_one_request_one_outcome_and_expr_re_executes(
    conn: Any, metric: str, cik: int, period: Period, op: str
) -> None:
    """For any (metric, company, period): exactly one outcome (a result XOR a NotFound, never
    both, never none), it identifies the request, and a result's expr re-evaluates in independent
    Decimal arithmetic to its value over real original-version facts. `growth` is only drawn for
    non-ratio metrics (growth of a ratio is not specified)."""
    if op == "growth" and load_catalog()[metric].unit == "ratio":
        op = "value"
    out = run(conn, [metric], [cik], [period], op)
    assert len(out.results) + len(out.not_found) == 1
    if out.results:
        res = out.results[0]
        check_provenance(res)
        assert (res.metric, res.cik, (res.fiscal_year, res.fiscal_period)) == (metric, cik, period)
        assert res.operation == op
        assert res.unit == ("ratio" if op == "growth" else load_catalog()[metric].unit)
    else:
        nf = out.not_found[0]
        assert (nf.metric, nf.cik, (nf.fiscal_year, nf.fiscal_period)) == (metric, cik, period)


@given(
    metric=st.sampled_from(ALL_METRICS),
    cik=st.sampled_from(CIKS),
    year=st.integers(min_value=1990, max_value=2015) | st.integers(min_value=2026, max_value=2040),
    fp=st.sampled_from(["FY", "Q1", "Q2", "Q3", "Q4"]),
)
@PROP
def test_property_no_data_means_no_number(
    conn: Any, metric: str, cik: int, year: int, fp: str
) -> None:
    """A fiscal year with no rows in the data can never produce a value for any of the 25 catalog
    metrics (tag, formula or fallback): the compiler never fills a gap."""
    nf = only_not_found(run(conn, [metric], [cik], [(year, fp)]))
    assert nf.metric == metric and nf.cik == cik and nf.fiscal_year == year


@given(pair=st.sampled_from(list(combinations(CIKS, 2))), period=st.sampled_from(PERIODS))
@PROP
def test_property_difference_is_antisymmetric(
    conn: Any, pair: tuple[int, int], period: Period
) -> None:
    """When both sides exist, compare(a, b).difference == -compare(b, a).difference, and each
    equals the difference of the two value results (net_income)."""
    a, b = pair
    ab = run(conn, ["net_income"], [a, b], [period], "compare")
    ba = run(conn, ["net_income"], [b, a], [period], "compare")
    d_ab = [r for r in ab.results if r.operation == "difference"]
    d_ba = [r for r in ba.results if r.operation == "difference"]
    assert len(d_ab) == len(d_ba)
    if d_ab:
        values = {r.cik: r.value for r in ab.results if r.operation == "value"}
        assert d_ab[0].value == values[a] - values[b] == -d_ba[0].value
        check_provenance(d_ab[0])


# ======================================================================
# 11. Rules added with the implementation (ADR-047)
# ======================================================================


@pytest.mark.parametrize(
    ("metrics", "ciks", "periods", "op"),
    [
        (["net_income"], [TI], [(2022, "FY")], "rank"),  # a rank of one company means nothing
        (["gross_margin"], [TI], [(2022, "FY")], "growth"),  # growth of a ratio
        (["revenue"], [TI], [(2020, "FY"), (2021, "FY"), (2022, "FY")], "growth"),
        (["revenue"], [TI], [(2021, "FY"), (2022, "Q2")], "growth"),  # unlike periods
        (["revenue"], [], [(2022, "FY")], "value"),  # no company outside a rank
        (["revenue"], [], [(2022, "FY")], "rank"),  # no company and no universe
    ],
)
def test_plans_with_no_defensible_reading_are_unsupported(
    conn: Any, metrics: list[str], ciks: list[int], periods: list[Period], op: str
) -> None:
    with pytest.raises(UnsupportedPlan):
        run(conn, metrics, ciks, periods, op)


def test_a_catalog_formula_cycle_is_unsupported_not_a_recursion_error(conn: Any) -> None:
    cat = dict(load_catalog())
    cat["gross_margin"] = MetricDef(unit="ratio", formula="operating_margin + 1")
    cat["operating_margin"] = MetricDef(unit="ratio", formula="gross_margin + 1")
    with pytest.raises(UnsupportedPlan):
        run(conn, ["gross_margin"], [TI], [(2022, "FY")], catalog=cat)


def test_repeating_a_company_or_period_does_not_duplicate_results(conn: Any) -> None:
    out = run(conn, ["net_income"], [TI, TI], [(2022, "FY"), (2022, "FY")])
    assert len(out.results) == 1 and not out.not_found


def test_difference_of_ratios_re_executes(conn: Any) -> None:
    out = run(conn, ["operating_margin"], [TI, INSULET], [(2022, "FY")], "compare")
    diff = pick(out, op="difference")
    assert diff.unit == "ratio"
    assert diff.value == pick(out, cik=TI).value - pick(out, cik=INSULET).value


def test_growth_of_a_fallback_metric_re_executes_with_nested_parentheses(conn: Any) -> None:
    res = one(run(conn, ["gross_profit"], [DT], [(2022, "FY")], "growth"))
    assert res.value == ((DT_REVC_FY22_V - DT_COGS_FY22_V) - (DT_REVC_FY21_V - DT_COGS_FY21_V)) / (
        DT_REVC_FY21_V - DT_COGS_FY21_V
    )
    assert len(res.fact_ids) == 4 and len(note(res, "derived_fallback")) == 1


def _scratch(pg_url: str, extra: list[dict[str, Any]], schema: str) -> Any:
    """A private schema holding the real fixture rows plus synthetic ones."""
    c = psycopg.connect(pg_url, autocommit=True, options=f"-c search_path={schema}")
    c.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    c.execute(f"DROP TABLE IF EXISTS {schema}.facts_v1 CASCADE")
    load_into(c, RAW + extra)
    return c


def test_a_custom_taxonomy_tag_with_the_same_name_is_ignored(conn: Any, pg_url: str) -> None:
    """A company's own tag called Revenues is a different concept (ADR-044 scope)."""
    base = next(r for r in RAW if r["fact_id"] == TI_NI_FY22)
    custom = dict(base, fact_id=7_100_000_000, fact_key=7_100_000_000, taxonomy="custom")
    custom["value"] = "1"
    with _scratch(pg_url, [custom], "custom_tax") as c:
        assert one(run(c, ["net_income"], [TI], [(2022, "FY")])).value == TI_NI_FY22_V


def test_one_tag_with_two_period_ends_abstains(conn: Any, pg_url: str) -> None:
    """Two original facts for one (tag, fiscal period) cannot both be that period's number."""
    base = next(r for r in RAW if r["fact_id"] == TI_NI_FY22)
    other = dict(base, fact_id=7_200_000_000, fact_key=7_200_000_000, period_end="2022-12-30")
    with _scratch(pg_url, [other], "two_ends") as c:
        nf = only_not_found(run(c, ["net_income"], [TI], [(2022, "FY")]))
    assert nf.reason == "conflicting_candidates"


# ======================================================================
# 12. Regressions from the pre-commit review (2026-11-25)
# ======================================================================

FAKE_CIK = 9_999_999


def _fake(template_id: int, fact_id: int, **kw: Any) -> dict[str, Any]:
    """A synthetic row for a clearly fake company, shaped like a real fixture row."""
    row = dict(next(r for r in RAW if r["fact_id"] == template_id))
    row.update(fact_id=fact_id, fact_key=fact_id, cik=FAKE_CIK, is_restated=False)
    row.update(is_original=True, is_latest=True, derived_from=None)
    row.update(kw)
    return row


def test_a_trailing_twelve_month_figure_from_a_10q_is_not_the_fiscal_year(
    conn: Any, pg_url: str
) -> None:
    """A 10-Q's twelve-month figure is also labelled FY with qtrs = 4 in the lake (real case:
    Amazon FY2022). Served as the year, it would be a wrong number."""
    ttm = _fake(TI_NI_FY22, 7_300_000_001, form="10-Q", period_end="2022-06-30", value="116")
    with _scratch(pg_url, [ttm], "ttm_only") as c:
        nf = only_not_found(run(c, ["net_income"], [FAKE_CIK], [(2022, "FY")]))
    assert nf.reason == "no_fact"


def test_a_10q_twelve_month_figure_does_not_hide_the_annual_one(conn: Any, pg_url: str) -> None:
    ttm = _fake(TI_NI_FY22, 7_300_000_002, form="10-Q", period_end="2022-06-30", value="116")
    annual = _fake(TI_NI_FY22, 7_300_000_003, value="-27")
    with _scratch(pg_url, [ttm, annual], "ttm_and_annual") as c:
        res = compile_plan(make_plan(["net_income"], [FAKE_CIK], [(2022, "FY")]), c)
    assert [r.value for r in res.results] == [D("-27")] and not res.not_found


def test_a_total_below_its_component_abstains_even_when_the_identity_closes(
    conn: Any, pg_url: str
) -> None:
    """ADR-043: `Revenues` under contract revenue is inconsistent, so no later rule picks."""
    rows = [
        _fake(TI_REV_FY22, 7_400_000_001, tag="Revenues", value="100000000"),
        _fake(
            TI_REV_FY22,
            7_400_000_002,
            tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            value="150000000",
        ),
        _fake(TI_COST_C1_FY21, 7_400_000_003, fiscal_year=2022, value="40000000"),
        _fake(TI_GP_FY22, 7_400_000_004, value="60000000"),
    ]
    with _scratch(pg_url, rows, "below_component") as c:
        nf = only_not_found(run(c, ["revenue"], [FAKE_CIK], [(2022, "FY")]))
    assert nf.reason == "conflicting_candidates" and "below" in nf.detail


def test_growth_across_two_tags_that_never_agree_abstains(conn: Any, pg_url: str) -> None:
    """50M contract revenue one year and 900M `Revenues` the next is not 1,700% growth."""
    rows = [
        _fake(
            TI_REV_FY21,
            7_500_000_001,
            tag="RevenueFromContractWithCustomerExcludingAssessedTax",
            value="50000000",
        ),
        _fake(TI_REV_FY22, 7_500_000_002, tag="Revenues", value="900000000"),
    ]
    with _scratch(pg_url, rows, "growth_tags") as c:
        nf = only_not_found(run(c, ["revenue"], [FAKE_CIK], [(2022, "FY")], "growth"))
    assert nf.reason == "conflicting_candidates" and "different tags" in nf.detail


def test_restated_notes_on_a_ratio_name_the_restated_input(conn: Any) -> None:
    """The note must not say the margin was restated: net income and revenue were."""
    res = one(run(conn, ["net_margin"], [AFLAC], [(2021, "FY")]))
    texts = sorted(n.text for n in note(res, "restated"))
    assert [t.split(" for FY2021")[0] for t in texts] == ["Net income", "Revenue"]


def test_restated_notes_on_growth_name_the_period_of_each_input(conn: Any) -> None:
    res = one(run(conn, ["net_income"], [AFLAC], [(2021, "FY")], "growth"))
    (n,) = note(res, "restated")
    assert n.text.startswith("Net income for FY2021")


def test_a_fallback_note_names_the_periods_it_applies_to(conn: Any) -> None:
    res = one(run(conn, ["gross_profit"], [DT], [(2022, "FY")], "growth"))
    (n,) = note(res, "derived_fallback")
    assert "FY2022" in n.text and "FY2021" in n.text


def test_the_engine_period_anchors_run_on_postgres(conn: Any, pg_url: str) -> None:
    """The anchor SQL (annual and quarterly) against a real server, not a fake connection."""
    from app.engine.engine import EngineSystem

    rows = [
        _fake(TI_NI_FY22, 7_600_000_001, fiscal_year=2023, period_end="2023-12-31",
              filed="2024-02-01"),
        _fake(TI_NI_FY22, 7_600_000_002, fiscal_year=2024, fiscal_period="Q2", qtrs=1,
              form="10-Q", period_end="2024-06-30", filed="2024-07-30"),
        # a later-ending twelve-month figure in a 10-Q must not become "the latest year"
        _fake(TI_NI_FY22, 7_600_000_003, fiscal_year=2024, form="10-Q",
              period_end="2024-06-30", filed="2024-07-30"),
    ]  # fmt: skip
    with _scratch(pg_url, rows, "anchors") as c:
        eng = EngineSystem(connect=lambda dsn: c)
        plan = make_plan(["net_income"], [FAKE_CIK], [])
        (anchor,) = eng._anchors(plan, "postgres://unused")
    assert anchor.fiscal_year == 2023
    assert (anchor.latest.fiscal_year, anchor.latest.fiscal_period) == (2024, "Q2")


CONTRACT_TAG = "RevenueFromContractWithCustomerExcludingAssessedTax"


def _gp_history(*, reported_2022: bool) -> list[dict[str, Any]]:
    """FY2021: contract revenue 50, cost 20, no GrossProfit. FY2022: `Revenues` 900, cost 400."""
    rows = [
        _fake(TI_REV_FY21, 7_700_000_001, tag=CONTRACT_TAG, value="50000000"),
        _fake(TI_COST_C1_FY21, 7_700_000_002, value="20000000"),
        _fake(TI_REV_FY22, 7_700_000_003, tag="Revenues", value="900000000"),
        _fake(TI_COST_C1_FY21, 7_700_000_004, fiscal_year=2022, value="400000000",
              period_end="2022-12-31"),
    ]  # fmt: skip
    if reported_2022:
        rows.append(_fake(TI_GP_FY22, 7_700_000_005, value="500000000"))
    return rows


@pytest.mark.parametrize("reported_2022", [False, True])
def test_growth_through_a_fallback_cannot_mix_concepts(
    conn: Any, pg_url: str, reported_2022: bool
) -> None:
    """Computed vs computed from unrelated revenue tags, and reported vs computed, are both
    not comparisons of one quantity."""
    schema = f"gp_growth_{int(reported_2022)}"
    with _scratch(pg_url, _gp_history(reported_2022=reported_2022), schema) as c:
        nf = only_not_found(run(c, ["gross_profit"], [FAKE_CIK], [(2022, "FY")], "growth"))
    assert nf.reason == "conflicting_candidates"


def test_the_derived_quarter_note_names_the_metric(conn: Any) -> None:
    res = one(run(conn, ["revenue"], [TI], [(2022, "Q4")]))
    (n,) = note(res, "q4_derived")
    assert n.text.startswith("Revenue for Q4 FY2022")
