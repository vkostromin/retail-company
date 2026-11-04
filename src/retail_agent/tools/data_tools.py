"""Data tools: dataset description, guarded SQL execution, analyst playbook search."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from google.api_core.exceptions import GoogleAPICallError
from google.cloud import bigquery
from pydantic import BaseModel, Field

from ..resilience.errors import DependencyUnavailable
from ..safety.pii import sanitize_dataframe
from ..safety.privacy import protect_small_groups
from ..safety.sql_guard import LOGICAL_TABLES, SQLGuardError, extract_string_filters, guard_sql
from .base import ToolContext, ToolSpec

TABLE_NOTES = {
    "orders": "One row per customer order: status, timestamps for created/shipped/delivered/returned, "
              "number of items. Join to order_items on order_id, to users on user_id.",
    "order_items": "One row per item sold - the source of revenue (sale_price). Each item has its own "
                   "status and timestamps. Join to products on product_id, orders on order_id, users on user_id.",
    "products": "Product catalog: name, brand, category, department (Men/Women), retail_price and cost "
                "(for margin), distribution center.",
    "users": "Customers (non-identifying attributes only): age, gender, city, state, country, acquisition "
             "channel (traffic_source) and signup date. Personal data (names, emails, addresses, exact "
             "locations) is not available to this assistant.",
}
GLOSSARY = {
    "net revenue": "SUM(order_items.sale_price) where status NOT IN ('Cancelled','Returned')",
    "AOV": "net revenue / distinct orders with at least one non-cancelled, non-returned item",
    "gross margin": "(sale_price - products.cost) / sale_price",
    "return rate": "COUNTIF(status='Returned') / COUNTIF(status != 'Cancelled') on order_items",
    "churn": "no non-cancelled order for 90 days; counted in the month the 90 days elapse; churn rate = churned / customers ordering in that month",
    "status values": "Complete, Shipped, Processing, Cancelled, Returned",
    "coverage": "Orders from January 2019 to today, refreshed daily",
}
_STRING_COLUMNS = {
    "status", "gender", "category", "name", "brand", "department", "sku", "city", "state", "country",
    "traffic_source",
}


# --------------------------------------------------------------------------- describe
class DescribeArgs(BaseModel):
    table: Literal["orders", "order_items", "products", "users"] | None = Field(
        None, description="Optional: one table to describe in detail. Omit for an overview of all tables."
    )


def describe_dataset(ctx: ToolContext, args: DescribeArgs) -> dict:
    services = ctx.services
    tables = [args.table] if args.table else list(LOGICAL_TABLES)
    out = {}
    for t in tables:
        columns = services.table_columns(t)
        out[t] = {"description": TABLE_NOTES[t], "columns": columns}
    return {
        "tables": out,
        "glossary": GLOSSARY,
        "your_access": f"You can analyse {ctx.principal.scope.describe()}. Queries are automatically limited to this.",
    }


# --------------------------------------------------------------------------- run_sql
class RunSQLArgs(BaseModel):
    sql: str = Field(description="ONE BigQuery Standard SQL SELECT over the unqualified tables orders, "
                                 "order_items, products, users. Aggregate in SQL; keep results compact.")
    purpose: str = Field(description="What this query answers, in a few words (for logs).")


def _clean_bq_error(exc: GoogleAPICallError, dataset: str) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    try:
        msg = exc.errors[0]["message"]
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    msg = re.sub(r"\s+at \[\d+:\d+\]", "", msg)
    return msg.replace(f"{dataset}.", "")[:400]


def _records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso", double_precision=4))


def _diagnose_empty(ctx: ToolContext, sql: str) -> list[dict]:
    """For string filters in an empty-result query, look up the closest stored values."""
    hints = []
    for f in [f for f in extract_string_filters(sql) if f.column in _STRING_COLUMNS][:3]:
        probe = (
            f"SELECT {f.column} AS value, COUNT(*) AS n FROM {f.table} GROUP BY 1 "
            f"ORDER BY IF(LOWER({f.column}) LIKE @probe, 0, 1), n DESC LIMIT 12"
        )
        try:
            g = guard_sql(probe, ctx.principal, ctx.settings.bq_dataset, 12)
            params = [*g.params, bigquery.ScalarQueryParameter("probe", "STRING", f"%{f.value.lower()[:3]}%")]
            res = ctx.services.bq.run(g.sql, params, max_rows=12, labels={"purpose": "empty-diagnosis"})
            values = [v for v in res.df["value"].tolist() if v is not None]
            if f.value not in values:
                hints.append({"filter": f"{f.table}.{f.column} = '{f.value}'",
                              "closest_valid_values": values})
        except Exception:  # noqa: BLE001 - diagnosis is best effort
            continue
    return hints


def run_sql(ctx: ToolContext, args: RunSQLArgs) -> dict:
    s = ctx.settings
    queries, errors = ctx.current("sql_queries"), ctx.current("sql_errors")
    if queries >= s.max_sql_queries or errors >= s.max_sql_errors:
        limit = "queries" if queries >= s.max_sql_queries else "failed attempts"
        ctx.turn.emit("sql_budget_exhausted", queries=queries, errors=errors, limit=limit)
        return {
            "status": "budget_exhausted",
            "error": f"The query budget for this question is used up ({queries} queries, {errors} failed).",
            "hint": "Do not run more queries. Answer with what you have, or explain what could not be "
                    "computed and suggest how the user could rephrase.",
        }
    attempt = ctx.bump("sql_queries")
    ctx.turn.progress(f"Querying data: {args.purpose[:60]}")
    base = {"attempt": attempt, "purpose": args.purpose, "sql": args.sql}

    try:
        guarded = guard_sql(args.sql, ctx.principal, s.bq_dataset, s.max_result_rows)
    except SQLGuardError as e:
        ctx.bump("sql_errors")
        ctx.turn.emit("sql_attempt", status="rejected", code=e.code, error=e.message, **base)
        if e.code in {"pii_column", "individual_demographics", "not_read_only", "forbidden_function", "unknown_table", "multiple_statements"}:
            ctx.turn.emit("guardrail_block", layer="sql_guard", category=e.code)
        return e.to_dict()

    base["executed_sql"] = guarded.sql  # what BigQuery actually ran (scoped rewrite)
    cache_key = hashlib.sha256(
        (guarded.sql + json.dumps([(p.name, getattr(p, "values", None)) for p in guarded.params])).encode()
    ).hexdigest()
    result = ctx.services.cache.get(cache_key)
    try:
        if result is None:
            est = ctx.services.bq.dry_run(guarded.sql, guarded.params)  # free: syntax + cost check
            if est > s.max_bytes_billed:
                ctx.bump("sql_errors")
                ctx.turn.emit("sql_attempt", status="too_expensive", bytes_estimate=est, **base)
                return {"status": "rejected", "error": f"Query would scan {est / 1e9:.1f} GB (limit "
                        f"{s.max_bytes_billed / 1e9:.1f} GB).", "hint": "Filter by date or aggregate more."}
            result = ctx.services.bq.run(
                guarded.sql, guarded.params, max_rows=s.max_result_rows,
                labels={"app": "retail-agent", "user": ctx.principal.user_id, "session": ctx.session_id[:30]},
                on_retry=lambda n, exc: ctx.turn.emit("retry", dependency="bigquery", attempt=n,
                                                      error_type=type(exc).__name__),
            )
            ctx.services.cache.set(cache_key, result)
    except DependencyUnavailable as e:
        ctx.turn.emit("sql_attempt", status="unavailable", error=str(e)[:300], **base)
        ctx.turn.emit("dependency_down", dependency="bigquery", error=str(e)[:300])
        ctx.turn.flags.add("degraded")
        return {"status": "unavailable", "error": "The data warehouse is temporarily unavailable.",
                "hint": "Do not retry now. Tell the user the data source is temporarily unavailable and "
                        "to try again in a few minutes."}
    except GoogleAPICallError as e:
        msg = _clean_bq_error(e, s.bq_dataset)
        ctx.bump("sql_errors")
        ctx.turn.emit("sql_attempt", status="error", error=msg, **base)
        return {"status": "error", "error": msg,
                "hint": "Fix the SQL and try again. Use only the documented columns; table names unqualified."}

    df, redactions = sanitize_dataframe(result.df)
    if redactions:
        ctx.turn.emit("pii_redaction", layer="query_result", counts=dict(redactions))
    df, privacy = protect_small_groups(df)
    if privacy:
        ctx.turn.emit("privacy_suppression", **privacy)
    ctx.turn.sql_executed.append(args.sql)
    ctx.turn.emit("sql_attempt", status="ok", rows=len(df), bytes_processed=result.bytes_processed,
                  cache_hit=result.cache_hit, duration_ms=result.duration_ms,
                  result_preview=json.dumps(_records(df.head(5)), ensure_ascii=False)[:1500], **base)

    if df.empty:
        ctx.bump("sql_errors")  # an empty result usually means a wrong filter: it counts as a correction
        hints = _diagnose_empty(ctx, args.sql)
        ctx.turn.emit("empty_result", attempt=attempt, hints=hints)
        return {
            "status": "ok", "row_count": 0, "rows": [],
            "hint": "The query returned no rows. Check filter values (exact spelling/case as stored) and the "
                    "date range before concluding there is no data."
                    + (f" Groups with fewer than {guarded.min_group_size} customers are removed from demographic "
                       "queries, so a narrow segment can return nothing." if guarded.min_group_size else ""),
            "valid_values": hints,
        }
    preview = df.head(s.preview_rows)
    out = {"status": "ok", "row_count": len(df), "columns": list(df.columns), "rows": _records(preview)}
    if guarded.min_group_size:
        out["privacy_note"] = (f"Demographic groups with fewer than {guarded.min_group_size} customers are excluded, "
                               "so group totals can be below overall totals.")
    if privacy.get("dropped_columns"):
        out["privacy_note"] = (f"Removed {privacy['dropped_columns']} from customer-level rows: demographics are "
                               "only available for groups of customers.")
    if privacy.get("suppressed_groups"):
        out["privacy_note"] = (f"{privacy['suppressed_groups']} group(s) with fewer than 5 customers were "
                               "suppressed. Mention this; do not estimate them.")
    if len(df) > s.preview_rows:
        out["note"] = f"Showing the first {s.preview_rows} of {len(df)} rows; aggregate further if needed."
    return out


# --------------------------------------------------------------------------- playbook
class PlaybookArgs(BaseModel):
    question: str = Field(description="A (sub-)question to find similar analyst-approved analyses for.")


def search_playbook(ctx: ToolContext, args: PlaybookArgs) -> dict:
    hits, mode = ctx.services.golden.search(args.question, exclude=ctx.turn.golden_exclude)
    ctx.turn.emit("golden_search", mode=mode, hits=[(t.id, round(s, 3)) for t, s in hits])
    if not hits:
        return {"examples": [], "note": "No sufficiently similar analyst examples."}
    return {"retrieval": mode, "examples": [
        {"id": t.id, "question": t.question, "interpretation": t.interpretation, "sql": t.sql,
         "conclusion_method": t.report, "similarity": round(s, 3)} for t, s in hits
    ]}


DATA_TOOLS = [
    ToolSpec("describe_dataset", "Describe the available tables, columns, business definitions and the "
             "user's data access. Use for questions about what data exists.", DescribeArgs, describe_dataset,
             progress="Reading the data catalog…"),
    ToolSpec("run_sql", "Run one read-only BigQuery SQL query and get the result rows. Queries are "
             "automatically limited to the user's data access and to non-personal columns.", RunSQLArgs, run_sql,
             progress="Querying data…"),
    ToolSpec("search_analyst_playbook", "Find analyst-approved past analyses (question, interpretation, SQL, "
             "conclusion) similar to a question. Use for sub-questions in multi-step analyses.", PlaybookArgs,
             search_playbook, progress="Checking the analyst playbook…"),
]
