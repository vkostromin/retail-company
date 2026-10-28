"""SQL guard + scope rewrite: the hard enforcement layer for safety and PII.

The LLM writes SQL against four *logical* tables (orders, order_items, products, users).
Before anything reaches BigQuery we:

1. parse it (sqlglot, BigQuery dialect) and allow exactly one read-only query,
2. allow only the logical tables (and the query's own CTEs),
3. reject direct references to PII columns with an actionable message,
4. replace every logical table with a *scoped, PII-free subquery* bound to the
   principal's entitlements through query parameters (never string-spliced),
5. enforce a row LIMIT.

Even if the model is jailbroken, the rewritten query physically cannot see PII columns
or out-of-scope products. In production the same contract is implemented by BigQuery
authorized views + row-level access policies + policy tags (see docs/technical-design.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from google.cloud import bigquery
from sqlglot import exp

from .entitlements import Principal

LOGICAL_TABLES: dict[str, list[str]] = {
    "orders": [
        "order_id", "user_id", "status", "gender", "created_at", "returned_at",
        "shipped_at", "delivered_at", "num_of_item",
    ],
    "order_items": [
        "id", "order_id", "user_id", "product_id", "inventory_item_id", "status",
        "created_at", "shipped_at", "delivered_at", "returned_at", "sale_price",
    ],
    "products": [
        "id", "cost", "category", "name", "brand", "retail_price", "department", "sku",
        "distribution_center_id",
    ],
    # PII columns are intentionally absent: they do not exist for the agent.
    "users": ["id", "age", "gender", "city", "state", "country", "traffic_source", "created_at"],
}

PII_COLUMNS = {
    "first_name", "last_name", "email", "street_address", "postal_code", "latitude",
    "longitude", "user_geom",
}

_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
    exp.Command, exp.TruncateTable, exp.Into,
)
_FORBIDDEN_FUNCTIONS = {"EXTERNAL_QUERY", "SESSION_USER", "VECTOR_SEARCH", "AI.GENERATE"}
SCOPE_PARAMS = ("scope_brands", "scope_categories", "scope_departments")


class SQLGuardError(ValueError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"status": "rejected", "code": self.code, "error": self.message, "hint": self.hint}


@dataclass
class GuardedQuery:
    original_sql: str
    sql: str  # rewritten, executable
    params: list = field(default_factory=list)
    tables: set[str] = field(default_factory=set)


@dataclass
class StringFilter:
    table: str
    column: str
    value: str


def _parse_single(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="bigquery") if s is not None]
    except sqlglot.errors.ParseError as e:
        first = e.errors[0] if e.errors else {}
        raise SQLGuardError(
            "syntax_error",
            f"SQL syntax error: {first.get('description', str(e))[:200]}",
            "Fix the syntax. Use BigQuery Standard SQL.",
        ) from None
    if not statements:
        raise SQLGuardError("empty", "No SQL statement found.")
    if len(statements) > 1:
        raise SQLGuardError(
            "multiple_statements", "Only a single SELECT statement is allowed.", "Send one query."
        )
    return statements[0]


def _logical_name(table: exp.Table, dataset: str) -> str | None:
    """Return the logical table name for an allowed table reference, else None."""
    project, _, ds = dataset.partition(".")
    name = table.name.lower()
    catalog, db = table.catalog, table.db
    if not db and not catalog:
        return name if name in LOGICAL_TABLES else None
    if db == ds and catalog in ("", project) and name in LOGICAL_TABLES:
        return name
    return None


def _product_predicate(principal: Principal) -> str:
    if principal.scope.all_products:
        return "TRUE"
    return (
        "(brand IN UNNEST(@scope_brands) OR category IN UNNEST(@scope_categories)"
        " OR department IN UNNEST(@scope_departments))"
    )


def scoped_table_sql(table: str, principal: Principal, dataset: str) -> str:
    """The PII-free, entitlement-filtered source that replaces a logical table."""
    ds = f"`{dataset}"
    pred = _product_predicate(principal)
    in_scope_products = f"SELECT id FROM {ds}.products` WHERE {pred}"
    cols = ", ".join(LOGICAL_TABLES[table])
    if table == "products":
        return f"SELECT {cols} FROM {ds}.products` WHERE {pred}"
    if principal.scope.all_products:
        return f"SELECT {cols} FROM {ds}.{table}`"
    in_scope_items = f"SELECT order_id, user_id FROM {ds}.order_items` WHERE product_id IN ({in_scope_products})"
    if table == "order_items":
        return f"SELECT {cols} FROM {ds}.order_items` WHERE product_id IN ({in_scope_products})"
    if table == "orders":
        return f"SELECT {cols} FROM {ds}.orders` WHERE order_id IN (SELECT order_id FROM ({in_scope_items}))"
    if table == "users":
        return f"SELECT {cols} FROM {ds}.users` WHERE id IN (SELECT user_id FROM ({in_scope_items}))"
    raise KeyError(table)


def scope_params(principal: Principal) -> list:
    if principal.scope.all_products:
        return []
    s = principal.scope
    return [
        bigquery.ArrayQueryParameter("scope_brands", "STRING", s.brands),
        bigquery.ArrayQueryParameter("scope_categories", "STRING", s.categories),
        bigquery.ArrayQueryParameter("scope_departments", "STRING", s.departments),
    ]


def guard_sql(sql: str, principal: Principal, dataset: str, max_rows: int = 500) -> GuardedQuery:
    """Validate the model's SQL and rewrite it into a scoped, PII-free, bounded query."""
    sql = sql.strip().rstrip(";").strip()
    root = _parse_single(sql)

    if not isinstance(root, exp.Query) or isinstance(root, _FORBIDDEN_NODES):
        raise SQLGuardError(
            "not_read_only",
            "Only read-only SELECT queries are allowed (the database is read-only).",
            "Write a SELECT (optionally WITH ... SELECT).",
        )
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardError("not_read_only", "Only read-only SELECT queries are allowed.")
        if isinstance(node, exp.Anonymous) and node.name.upper() in _FORBIDDEN_FUNCTIONS:
            raise SQLGuardError("forbidden_function", f"Function {node.name} is not allowed.")

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    clash = cte_names & set(LOGICAL_TABLES)
    if clash:
        raise SQLGuardError(
            "cte_shadowing",
            f"CTE name(s) {sorted(clash)} shadow a source table.",
            "Rename the CTE (e.g. users_agg).",
        )

    for col in root.find_all(exp.Column):
        if col.name.lower() in PII_COLUMNS:
            raise SQLGuardError(
                "pii_column",
                f"Column '{col.name}' contains personal data (PII) and is not available.",
                "Use users.id as a pseudonymous customer identifier, and aggregate by "
                "city/state/country/age/gender instead of personal details.",
            )

    used: set[str] = set()
    replacements: list[tuple[exp.Table, exp.Expression]] = []
    for table in root.find_all(exp.Table):
        name_lower = table.name.lower()
        if not table.db and not table.catalog and name_lower in cte_names:
            continue
        if not table.name:
            raise SQLGuardError(
                "forbidden_source", "Table functions and external sources are not allowed."
            )
        logical = _logical_name(table, dataset)
        if logical is None:
            ref = ".".join(p for p in (table.catalog, table.db, table.name) if p)
            raise SQLGuardError(
                "unknown_table",
                f"Table '{ref}' is not available.",
                "Use only: orders, order_items, products, users (unqualified names).",
            )
        used.add(logical)
        subquery = sqlglot.parse_one(scoped_table_sql(logical, principal, dataset), read="bigquery")
        alias = table.alias or logical
        replacements.append((table, subquery.subquery(alias)))

    if not used:
        raise SQLGuardError(
            "no_tables", "The query does not read from any table.", "Query orders, order_items, products or users."
        )

    for table, replacement in replacements:
        table.replace(replacement)

    limit = root.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal) and limit.expression.is_int:
        current = int(limit.expression.this)
    if current is None or current > max_rows:
        root.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))

    return GuardedQuery(
        original_sql=sql,
        sql=root.sql(dialect="bigquery"),
        params=scope_params(principal),
        tables=used,
    )


def extract_string_filters(sql: str) -> list[StringFilter]:
    """Find `col = 'literal'` / `col IN ('a', ...)` / `col LIKE '...'` filters on logical tables.

    Used to diagnose empty results (e.g. 'Brazil' vs the stored 'Brasil').
    """
    try:
        root = _parse_single(sql)
    except SQLGuardError:
        return []
    alias_map: dict[str, str] = {}
    present: list[str] = []
    for t in root.find_all(exp.Table):
        name = t.name.lower()
        if name in LOGICAL_TABLES:
            alias_map[(t.alias or name).lower()] = name
            present.append(name)

    def resolve(col: exp.Column) -> str | None:
        if col.table:
            return alias_map.get(col.table.lower())
        for t in present:
            if col.name.lower() in LOGICAL_TABLES[t]:
                return t
        return None

    found: list[StringFilter] = []
    for node in root.find_all(exp.EQ, exp.In, exp.Like, exp.ILike):
        col = node.this if isinstance(node.this, exp.Column) else None
        if isinstance(node, exp.EQ) and col is None and isinstance(node.expression, exp.Column):
            col, lit = node.expression, node.this
            literals = [lit]
        elif isinstance(node, exp.In):
            literals = node.expressions
        else:
            literals = [node.expression]
        if col is None and isinstance(node.this, exp.Lower | exp.Upper) and isinstance(node.this.this, exp.Column):
            col = node.this.this
        if col is None:
            continue
        table = resolve(col)
        if table is None or col.name.lower() not in LOGICAL_TABLES[table]:
            continue
        for lit in literals:
            if isinstance(lit, exp.Literal) and lit.is_string:
                found.append(StringFilter(table, col.name.lower(), lit.this))
    # de-duplicate, keep order
    seen, unique = set(), []
    for f in found:
        key = (f.table, f.column, f.value)
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique
