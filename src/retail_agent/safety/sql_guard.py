"""SQL guard + scope rewrite: the hard enforcement layer for safety and PII.

The LLM writes SQL against four *logical* tables (orders, order_items, products, users).
Before anything reaches BigQuery we:

1. parse it (sqlglot, BigQuery dialect) and allow exactly one read-only query,
2. allow only the logical tables (and the query's own CTEs),
3. reject direct references to PII columns with an actionable message,
   and allow demographics (age, gender, city) only in aggregates over >= 5 customers,
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

# Quasi-identifiers: harmless in aggregates, re-identifying when tied to one customer.
QUASI_IDENTIFIERS = {"age", "gender", "city"}
# Tables that carry them, with the column identifying the customer (the privacy unit).
PRIVACY_UNITS = {"users": "id", "orders": "user_id"}
QUASI_BY_TABLE = {"users": QUASI_IDENTIFIERS, "orders": {"gender"}}
MIN_GROUP_SIZE = 5
_SAFE_AGGREGATES = (exp.Count, exp.CountIf, exp.Sum, exp.Avg, exp.ApproxDistinct, exp.Stddev,
                    exp.StddevPop, exp.StddevSamp, exp.Variance, exp.VariancePop)

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
    min_group_size: int | None = None  # set when demographic groups below this size are filtered out


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
        # num_of_item is recomputed from in-scope items only, so it never reveals out-of-scope purchases.
        order_cols = ", ".join(f"o.{c}" for c in LOGICAL_TABLES["orders"] if c != "num_of_item")
        return (
            f"SELECT {order_cols}, s.num_of_item FROM {ds}.orders` AS o JOIN ("
            f"SELECT order_id, COUNT(*) AS num_of_item FROM {ds}.order_items` "
            f"WHERE product_id IN ({in_scope_products}) GROUP BY order_id) AS s ON s.order_id = o.order_id"
        )
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

    thresholded = _enforce_min_group_size(root, dataset)

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
        min_group_size=MIN_GROUP_SIZE if thresholded else None,
    )


def _demographics_error(message: str, hint: str) -> SQLGuardError:
    return SQLGuardError("individual_demographics", message, hint)


def _demographic_sources(select: exp.Select, dataset: str) -> dict[str, str]:
    """alias -> logical table, for tables with demographics read directly in this SELECT's FROM/JOINs."""
    out: dict[str, str] = {}
    for src in [select.args.get("from_"), *(select.args.get("joins") or [])]:
        table = src.this if src is not None else None
        if isinstance(table, exp.Table):
            logical = _logical_name(table, dataset)
            if logical in PRIVACY_UNITS:
                out[(table.alias or table.name).lower()] = logical
    return out


def _outer_selects(select: exp.Select):
    """Enclosing SELECTs a correlated reference could reach (a CTE body cannot see the outer query)."""
    node = select.parent
    while node is not None and not isinstance(node, exp.CTE):
        if isinstance(node, exp.Select):
            yield node
        node = node.parent


def _own(node: exp.Expression, select: exp.Select) -> bool:
    return node.find_ancestor(exp.Select) is select


def _is_aggregating(select: exp.Select) -> bool:
    return bool(select.args.get("group")) or any(
        _own(agg, select) and not isinstance(agg.parent, exp.Window)
        for e in select.expressions for agg in e.find_all(exp.AggFunc)
    )


def _enforce_min_group_size(root: exp.Expression, dataset: str) -> bool:
    """Demographics only in aggregates over at least MIN_GROUP_SIZE customers.

    Age, gender and city enter a query only where a SELECT reads them from `users` or `orders`
    (star and whole-row references are rejected, so every read is a named column). Each such
    SELECT must aggregate, may only use them in group keys, filters and COUNT/SUM/AVG-style
    aggregates, and gets `HAVING COUNT(DISTINCT customer) >= 5` added. Whatever leaves that
    SELECT therefore describes groups of at least 5 customers, however the rest of the query
    renames, filters or nests it. Returns True if any threshold was added.
    """
    selects = list(root.find_all(exp.Select))
    sources = {id(s): _demographic_sources(s, dataset) for s in selects}
    sites: dict[int, exp.Select] = {}

    for select in selects:
        if sources[id(select)] and any(
            isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
            for e in select.expressions
        ):
            raise _demographics_error("SELECT * is not allowed on users or orders.",
                                      "List the columns you need.")

    for col in root.find_all(exp.Column):
        block = col.find_ancestor(exp.Select)
        if block is None or isinstance(col.this, exp.Star):
            continue
        name, qualifier = col.name.lower(), (col.table or "").lower()
        own = sources[id(block)]
        outer = [sources[id(s)] for s in _outer_selects(block)]
        output_aliases = {e.alias.lower() for e in block.expressions if isinstance(e, exp.Alias)}
        if not qualifier and name not in output_aliases and any(name in src for src in (own, *outer)):
            raise _demographics_error("Whole-row references to users or orders are not allowed.",
                                      "Select individual columns.")
        if qualifier:
            table = own.get(qualifier)
            if table is None:
                if any(name in QUASI_BY_TABLE[src[qualifier]] for src in outer if qualifier in src):
                    raise _demographics_error(
                        "Age, gender and city must be used in the same SELECT that reads users/orders.",
                        "Move the condition on age/gender/city into the outer query.")
                continue
            if name not in QUASI_BY_TABLE[table]:
                continue
        else:
            if not any(name in QUASI_BY_TABLE[t] for t in own.values()):
                if any(name in QUASI_BY_TABLE[t] for src in outer for t in src.values()):
                    raise _demographics_error(
                        f"Ambiguous reference to '{name}' inside a subquery.",
                        f"Qualify it with its table alias (e.g. u.{name}).")
                continue
        if not _is_aggregating(block):
            raise _demographics_error(
                "Age, gender and city are only available in aggregates over groups of customers.",
                "GROUP BY age band, gender or city and aggregate the measures (COUNT, SUM, AVG). "
                f"Groups with fewer than {MIN_GROUP_SIZE} customers are removed automatically.")
        window = col.find_ancestor(exp.Window, exp.Select)
        if isinstance(window, exp.Window):
            raise _demographics_error("Window functions over age, gender or city are not allowed.",
                                      "Aggregate with GROUP BY instead.")
        agg = col.find_ancestor(exp.AggFunc, exp.Select)
        if isinstance(agg, exp.AggFunc) and not isinstance(agg, _SAFE_AGGREGATES):
            raise _demographics_error(
                f"{type(agg).__name__.upper()} of age, gender or city can reveal one customer's value.",
                "Use COUNT, SUM or AVG over the group.")
        sites[id(block)] = block

    for block in sites.values():
        own = sources[id(block)]
        for col in block.find_all(exp.Column):
            if not _own(col, block):
                continue
            name = col.name.lower()
            is_id = name == "id" or name.endswith("_id")
            if not is_id:
                continue
            agg = col.find_ancestor(exp.AggFunc, exp.Select)
            if isinstance(agg, exp.AggFunc):
                arg = agg.this.expressions[0] if isinstance(agg.this, exp.Distinct) and len(
                    agg.this.expressions) == 1 else agg.this
                if not (isinstance(agg, exp.Count | exp.ApproxDistinct) and arg is col):
                    raise _demographics_error(
                        "In queries that use age, gender or city, ids can only be counted.",
                        "Use COUNT(DISTINCT ...) of ids; filter rows in WHERE instead of inside aggregates.")
        group = block.args.get("group")
        if group is not None:
            for col in group.find_all(exp.Column):
                table = own.get((col.table or "").lower()) if col.table else None
                if col.name.lower() == "user_id" or (col.name.lower() == "id" and table == "users"):
                    raise _demographics_error(
                        "Demographics cannot be grouped per customer.",
                        "Compute per-customer metrics in a CTE without demographics, then join users and "
                        "aggregate by age band, gender or city.")
        alias, table = next(((a, t) for a, t in own.items() if t == "users"), next(iter(own.items())))
        unit = exp.column(PRIVACY_UNITS[table], table=alias)
        block.having(
            exp.GTE(this=exp.Count(this=exp.Distinct(expressions=[unit])), expression=exp.Literal.number(MIN_GROUP_SIZE)),
            copy=False,
        )
    return bool(sites)


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
