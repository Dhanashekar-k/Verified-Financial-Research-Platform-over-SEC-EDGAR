"""Guarded text-to-SQL fallback: validate a model-written SELECT before it can run.

Shekar-owned module. Implemented by Claude at Shekar's request (2026-11-25, ADR-047) against
`tests/unit/engine/test_sql_guard.py`; Shekar reviews it with /interview-me.

M05 section 3.5: exactly one SELECT; tables limited to ``{facts, companies}``; functions limited
to an allow-list; no comments, no chained statements, no writing CTEs; LIMIT injected or clamped
to 200. The fallback is off by default (``ENGINE_SQL_FALLBACK``) and never on the main path
(CLAUDE.md rule 6).

How it holds:

* **Allow-lists, not a block-list.** Every node of the parse tree must be a known type; every
  table, function call, qualified column name and cast target must be named. A qualified name
  matters because Postgres calls ``rel.name`` as a function when ``rel`` has no such column.
* **The text is screened before it is parsed:** printable ASCII only, no backslash, no ``$``, no
  comment, one statement, bounded length and nesting. This removes the places where the parser
  and Postgres could read the same bytes differently.
* **What runs is what was checked:** the returned SQL is regenerated from the checked tree and
  then checked again; both passes must give the same text.

Execution (role ``edgarqa_ro``, ``SET LOCAL statement_timeout = '2s'``) is a separate step: this
module only decides whether a string may be executed at all. That step is NOT built yet, and
nothing in the engine calls this module. Wiring it without the read-only role and the timeout
would leave this check as the only layer; it does not bound CPU time (a cross join is accepted).
"""

from __future__ import annotations

from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

MAX_LIMIT = 200
MAX_SQL_CHARS = 10_000
MAX_PAREN_DEPTH = 32
ALLOWED_TABLES: frozenset[str] = frozenset({"facts", "companies"})
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {"sum", "avg", "min", "max", "count", "round", "abs", "coalesce"}
)
# The columns of the served `facts` and `companies` views (05 section 3). A qualified name must
# be one of these: Postgres reads ``rel.name`` as ``name(rel)`` when ``rel`` has no such column,
# which would call any function that takes a row (``facts.json_agg``).
ALLOWED_COLUMNS: frozenset[str] = frozenset(
    {
        # facts
        "fact_id",
        "fact_key",
        "cik",
        "tag",
        "taxonomy",
        "uom",
        "qtrs",
        "period_start",
        "period_end",
        "fiscal_year",
        "fiscal_period",
        "calendar_frame",
        "value",
        "accession",
        "form",
        "filed",
        "is_latest",
        "is_original",
        "is_restated",
        "anomaly_score",
        "derived_from",
        # companies (cik is shared)
        "name",
        "ticker",
        "sic",
        "fye_mmdd",
        "aliases",
    }
)
SYSTEM_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "pg_toast"})

RejectionCode = Literal[
    "empty",
    "parse_error",
    "not_single_statement",
    "not_select",
    "table_not_allowed",
    "function_not_allowed",
    "comment",
    "writing_cte",
    "system_catalog",
    "other",
]


class SafeSQL(BaseModel):
    model_config = ConfigDict(frozen=True)

    sql: str  # the normalized statement, with LIMIT <= MAX_LIMIT


class Rejection(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: RejectionCode
    detail: str


class _Reject(Exception):
    def __init__(self, code: RejectionCode, detail: str) -> None:
        super().__init__(detail)
        self.rejection = Rejection(code=code, detail=detail)


# --- the text screen ----------------------------------------------------------------------------


def _screen_text(sql: str) -> str:
    """The statement without its one optional trailing ``;``. Strings ('...') and quoted
    identifiers ("...") are skipped over; with backslashes and ``$`` refused, they are the only
    quoting Postgres has left, so this scan and the server agree on what is code."""
    if len(sql) > MAX_SQL_CHARS:
        raise _Reject("other", f"statement is longer than {MAX_SQL_CHARS} characters")
    if any(ch not in "\t\n\r" and not " " <= ch <= "~" for ch in sql):
        raise _Reject("other", "only printable ASCII is accepted")
    if "\\" in sql:
        raise _Reject("other", "backslashes are not accepted")
    quote = ""
    depth = 0
    semicolon = -1
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                if sql[i + 1 : i + 2] == quote:  # a doubled quote is an escaped quote
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if sql[i : i + 2] in ("--", "/*"):
            raise _Reject("comment", "comments are not accepted")
        if semicolon >= 0 and not ch.isspace():
            raise _Reject("not_single_statement", "only one statement is accepted")
        if ch in ("'", '"'):
            quote = ch
        elif ch == "$":
            raise _Reject("other", "'$' (dollar quoting, parameters) is not accepted")
        elif ch == "(":
            depth += 1
            if depth > MAX_PAREN_DEPTH:
                raise _Reject("other", f"parentheses nest deeper than {MAX_PAREN_DEPTH}")
        elif ch == ")":
            depth -= 1
        elif ch == ";":
            semicolon = i
        i += 1
    if quote:
        raise _Reject("parse_error", "unterminated quoted text")
    body = (sql if semicolon < 0 else sql[:semicolon]).strip()
    if not body:
        raise _Reject("empty", "there is no statement")
    return body


# --- the tree -----------------------------------------------------------------------------------

_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Command,
    exp.Set,
    exp.Grant,
)
# sqlglot models these operators and keywords as functions; they call nothing.
_NOT_CALLS = (exp.Case, exp.If, exp.And, exp.Or, exp.Exists, exp.Cast)
_ALLOWED_CALLS = (
    exp.Sum,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Count,
    exp.Round,
    exp.Abs,
    exp.Coalesce,
)
_ALLOWED_NODES: tuple[type[exp.Expression], ...] = (
    *_NOT_CALLS,
    *_ALLOWED_CALLS,
    # query shape
    exp.Select,
    exp.From,
    exp.Join,
    exp.Table,
    exp.TableAlias,
    exp.Subquery,
    exp.With,
    exp.CTE,
    exp.Where,
    exp.Group,
    exp.Having,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Offset,
    exp.Distinct,
    # values and names
    exp.Identifier,
    exp.Column,
    exp.Star,
    exp.Alias,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Var,  # LIMIT ALL only; replaced before the tree is regenerated
    exp.Paren,
    exp.DataType,  # no length or precision: CHAR(n) pads a value to any size
    # operators
    exp.Add,
    exp.Sub,
    exp.Mul,
    exp.Div,
    exp.Mod,
    exp.Neg,
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.Not,
    exp.Is,
    exp.In,
    exp.Between,
    exp.Like,
    exp.ILike,
    exp.Any,
    exp.All,
)
# Plain value types only: 'x'::regclass and friends read the system catalog.
_ALLOWED_CASTS = frozenset(
    {
        "SMALLINT",
        "INT",
        "BIGINT",
        "DECIMAL",
        "FLOAT",
        "DOUBLE",
        "TEXT",
        "VARCHAR",
        "CHAR",
        "DATE",
        "TIMESTAMP",
        "BOOLEAN",
    }
)
# Bare words Postgres evaluates as functions although sqlglot reads some of them as columns.
_SESSION_WORDS = frozenset(
    {
        "user",
        "session_user",
        "current_user",
        "current_role",
        "current_schema",
        "current_catalog",
        "system_user",
    }
)


def _pg_name(identifier: object) -> str:
    """The name Postgres would look up: quoted identifiers keep their case, others fold down."""
    if not isinstance(identifier, exp.Identifier):
        raise _Reject("other", "a table or CTE needs a plain name")
    name = str(identifier.this)
    return name if identifier.quoted else name.lower()


def _visible_ctes(node: exp.Expr) -> set[str]:
    """CTE names ``node`` can see. Inside the i-th CTE of a WITH only the earlier ones exist:
    ``WITH api_keys AS (SELECT * FROM api_keys)`` reads the real table inside its own body."""
    visible: set[str] = set()
    child: exp.Expr = node
    parent = node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            for cte in parent.expressions:
                if cte is child:
                    break
                visible.add(_pg_name(cte.args["alias"].this))
        else:
            with_ = parent.args.get("with_") or parent.args.get("with")
            if isinstance(with_, exp.With) and with_ is not child:
                visible.update(_pg_name(cte.args["alias"].this) for cte in with_.expressions)
        child, parent = parent, parent.parent
    return visible


def _check_table(table: exp.Table) -> None:
    schema, catalog = table.args.get("db"), table.args.get("catalog")
    if not isinstance(table.this, exp.Identifier):
        raise _Reject("table_not_allowed", f"not a plain table: {table.sql()[:80]}")
    name = _pg_name(table.this)
    if schema is not None or catalog is not None:
        qualifier = str(getattr(schema, "this", schema)).lower()
        code: RejectionCode = (
            "system_catalog" if qualifier in SYSTEM_SCHEMAS else "table_not_allowed"
        )
        raise _Reject(code, f"schema-qualified names are not accepted: {table.sql()[:80]}")
    if name.lower().startswith("pg_"):
        raise _Reject("system_catalog", f"system relation {name!r}")
    if name not in ALLOWED_TABLES and name not in _visible_ctes(table):
        raise _Reject("table_not_allowed", f"table {name!r} is not allowed")


def _check_column(column: exp.Column) -> None:
    name = column.this
    if column.args.get("db") or column.args.get("catalog"):
        raise _Reject("table_not_allowed", "schema-qualified column names are not accepted")
    if isinstance(name, exp.Star):
        return
    if column.table:
        if _pg_name(name) not in ALLOWED_COLUMNS:
            raise _Reject(
                "function_not_allowed",
                f"'{column.table}.{name.this}' is not a column of facts or companies; Postgres "
                "would call it as a function (name computed columns without a prefix)",
            )
    elif (
        isinstance(name, exp.Identifier)
        and not name.quoted
        and str(name.this).lower() in _SESSION_WORDS
    ):
        raise _Reject("function_not_allowed", f"session value {name.this!r}")


def _check_tree(root: exp.Expr) -> None:
    if type(root) is not exp.Select:
        raise _Reject("not_select", f"only SELECT is accepted, not {type(root).__name__}")
    for node in root.walk():
        if isinstance(node, _WRITES):
            if node.find_ancestor(exp.CTE) is not None:
                raise _Reject("writing_cte", "a CTE may only read")
            raise _Reject("not_select", f"{type(node).__name__} is not a read")
        if isinstance(node, exp.Into):
            raise _Reject("not_select", "SELECT INTO creates a table")
        if isinstance(node, exp.Lock):
            raise _Reject("not_select", "row locks are not accepted")
        if isinstance(node, exp.SetOperation):
            raise _Reject("not_select", "UNION, INTERSECT and EXCEPT are not accepted")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise _Reject("other", "recursive CTEs are not accepted")
    for node in root.walk():
        if isinstance(node, exp.Func) and not isinstance(node, _NOT_CALLS + _ALLOWED_CALLS):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            raise _Reject("function_not_allowed", f"function {name.lower()!r} is not allowed")
        if isinstance(node, exp.Dot):
            raise _Reject("function_not_allowed", "qualified calls and field access")
        if isinstance(node, exp.Column):
            _check_column(node)
        if isinstance(node, exp.In) and (node.args.get("field") or node.args.get("unnest")):
            raise _Reject("table_not_allowed", "IN takes a list or a subquery")
    # A bare relation name is the whole row, which nests: SELECT a AS x, a AS y FROM (...) a.
    relations = {_pg_name(t.this) for t in root.find_all(exp.TableAlias) if t.this is not None}
    relations |= {
        _pg_name(t.this) for t in root.find_all(exp.Table) if isinstance(t.this, exp.Identifier)
    }
    for column in root.find_all(exp.Column):
        if not column.table and isinstance(column.this, exp.Identifier):
            if _pg_name(column.this) in relations:
                raise _Reject("other", f"{column.this.name!r} would select whole rows")
    for table in root.find_all(exp.Table):
        _check_table(table)
    for source in root.find_all(exp.From, exp.Join):
        if not isinstance(source.this, exp.Table | exp.Subquery):
            raise _Reject("table_not_allowed", "FROM takes a table or a subquery")
    for node in root.walk():
        if not isinstance(node, _ALLOWED_NODES):
            raise _Reject("other", f"{type(node).__name__} is not accepted")
        if isinstance(node, exp.Cast):
            kind = node.to.this  # a DataType.Type; a string for names sqlglot does not know
            target = str(getattr(kind, "name", kind)).upper()
            if target not in _ALLOWED_CASTS:
                raise _Reject("other", f"cast to {target} is not accepted")
        if isinstance(node, exp.Var):
            limit = node.parent
            if not (isinstance(limit, exp.Limit) and str(node.this).upper() == "ALL"):
                raise _Reject("other", f"unexpected word {node.this!r}")


def _plain_int(node: object) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string and str(node.this).isdigit():
        return int(node.this)
    return None


def _bound_rows(root: exp.Expr) -> None:
    """The outer statement gets LIMIT <= MAX_LIMIT; a literal OFFSET is kept."""
    offset = root.args.get("offset")
    if offset is not None and (
        not isinstance(offset, exp.Offset) or _plain_int(offset.expression) is None
    ):
        raise _Reject("other", "OFFSET must be a plain integer")
    limit = root.args.get("limit")
    rows = MAX_LIMIT
    if limit is not None:
        if not isinstance(limit, exp.Limit) or limit.args.get("expressions"):
            raise _Reject("other", "only LIMIT n bounds a result")
        value = limit.expression
        is_all = isinstance(value, exp.Var | exp.Column) and value.name.upper() == "ALL"
        if not is_all:
            number = _plain_int(value)
            if number is None:
                raise _Reject("other", "LIMIT must be a plain integer")
            rows = min(number, MAX_LIMIT)
    root.set("limit", exp.Limit(expression=exp.Literal.number(rows)))


def _normalize(sql: str) -> str:
    """One pass: screen the text, parse it, check the tree, bound it, regenerate it."""
    body = _screen_text(sql)
    statements = [s for s in sqlglot.parse(body, dialect="postgres") if s is not None]
    if len(statements) != 1:
        code: RejectionCode = "parse_error" if not statements else "not_single_statement"
        raise _Reject(code, f"expected one statement, found {len(statements)}")
    root = statements[0]
    # "LIMIT ALL" means no limit: replace it before the node check sees a bare word.
    limit = root.args.get("limit")
    if isinstance(limit, exp.Limit) and isinstance(limit.expression, exp.Var | exp.Column):
        if limit.expression.name.upper() == "ALL":
            limit.set("expression", exp.Literal.number(MAX_LIMIT))
    _check_tree(root)
    _bound_rows(root)
    return str(root.sql(dialect="postgres", comments=False))


def validate(sql: str) -> SafeSQL | Rejection:
    """``SafeSQL`` when ``sql`` is one allowed SELECT, else a ``Rejection`` (never raises on
    hostile input)."""
    try:
        normalized = _normalize(sql)
        if _normalize(normalized) != normalized:
            return Rejection(code="other", detail="the statement does not normalize stably")
    except _Reject as err:
        return err.rejection
    except Exception as err:  # noqa: BLE001 - total over hostile text, RecursionError included
        return Rejection(code="parse_error", detail=f"could not parse: {type(err).__name__}")
    return SafeSQL(sql=normalized)
