import pytest
import sqlglot

from retail_agent.safety.sql_guard import SQLGuardError, extract_string_filters, guard_sql
from tests.conftest import DATASET


def g(sql, principal, **kw):
    return guard_sql(sql, principal, DATASET, **kw)


@pytest.mark.parametrize(
    "sql,code",
    [
        ("DELETE FROM orders WHERE TRUE", "not_read_only"),
        ("DROP TABLE orders", "not_read_only"),
        ("INSERT INTO orders (order_id) VALUES (1)", "not_read_only"),
        ("UPDATE orders SET status='x' WHERE TRUE", "not_read_only"),
        ("SELECT 1; DROP TABLE orders", "multiple_statements"),
        ("EXPORT DATA OPTIONS(uri='gs://x/*', format='CSV') AS SELECT * FROM orders", "not_read_only"),
        ("SELECT email FROM users", "pii_column"),
        ("SELECT u.first_name, u.last_name FROM users u", "pii_column"),
        ("SELECT id FROM users WHERE email LIKE '%@gmail.com'", "pii_column"),
        ("SELECT ST_ASTEXT(user_geom) FROM users", "pii_column"),
        ("SELECT * FROM `bigquery-public-data.thelook_ecommerce.events`", "unknown_table"),
        ("SELECT * FROM other_project.secret.salaries", "unknown_table"),
        ("SELECT * FROM thelook_ecommerce.INFORMATION_SCHEMA.TABLES", "unknown_table"),
        ("SELECT * FROM EXTERNAL_QUERY('conn', 'select 1')", "forbidden_function"),
        ("WITH users AS (SELECT 1 AS id) SELECT * FROM users", "cte_shadowing"),
        ("SELECT 1", "no_tables"),
        ("SELEC status FROM orders", "syntax_error"),
    ],
)
def test_rejects(sql, code, ceo):
    with pytest.raises(SQLGuardError) as e:
        g(sql, ceo)
    assert e.value.code == code


def test_pii_rejection_has_actionable_hint(ceo):
    with pytest.raises(SQLGuardError) as e:
        g("SELECT email FROM users", ceo)
    assert "users.id" in e.value.hint


def test_ceo_rewrite_strips_pii_and_keeps_aliases(ceo):
    q = g("SELECT u.state, COUNT(*) n FROM users u GROUP BY 1", ceo)
    assert "`bigquery-public-data.thelook_ecommerce.users`" in q.sql
    assert "email" not in q.sql and "first_name" not in q.sql
    assert ") AS u" in q.sql
    assert q.params == []  # all products => no scope parameters
    sqlglot.parse_one(q.sql, read="bigquery")  # still valid SQL


def test_users_source_only_exposes_safe_columns(ceo):
    q = g("SELECT id, country FROM users", ceo)
    inner = q.sql.split("FROM (", 1)[1]
    assert "street_address" not in inner and "latitude" not in inner


def test_select_star_on_users_rejected(ceo):
    # A star would return age, gender and city per customer.
    with pytest.raises(SQLGuardError) as e:
        g("SELECT * FROM users", ceo)
    assert e.value.code == "individual_demographics"


def test_scoped_principal_gets_parameterized_filters_on_every_table(alice):
    q = g(
        "SELECT p.brand, SUM(oi.sale_price) FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "JOIN orders o ON o.order_id = oi.order_id JOIN users u ON u.id = oi.user_id GROUP BY 1",
        alice,
    )
    assert q.tables == {"order_items", "products", "orders", "users"}
    # every source carries the entitlement predicate
    assert q.sql.count("UNNEST(@scope_departments)") >= 4
    names = {p.name: p.values for p in q.params}
    assert names["scope_departments"] == ["Women"]
    assert "Women" not in q.sql  # values are bound as parameters, never spliced


def test_fully_qualified_logical_tables_are_scoped_too(alice):
    q = g("SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.order_items`", alice)
    assert "@scope_departments" in q.sql


def test_cte_references_are_not_rewritten_but_their_sources_are(alice):
    q = g("WITH agg AS (SELECT user_id, SUM(sale_price) s FROM order_items GROUP BY 1) SELECT * FROM agg", alice)
    assert "@scope_departments" in q.sql
    assert "FROM agg" in q.sql


def test_limit_enforced(ceo):
    assert "LIMIT 500" in g("SELECT * FROM products", ceo).sql
    assert "LIMIT 10" in g("SELECT * FROM products LIMIT 10", ceo).sql
    assert "LIMIT 500" in g("SELECT * FROM products LIMIT 100000", ceo).sql
    assert "LIMIT 500" in g("SELECT status FROM orders UNION ALL SELECT status FROM order_items", ceo).sql


def test_extract_string_filters():
    f = extract_string_filters(
        "SELECT COUNT(*) FROM users u JOIN orders o ON o.user_id=u.id "
        "WHERE u.country = 'Brazil' AND o.status IN ('complete', 'Shipped')"
    )
    assert [(x.table, x.column, x.value) for x in f] == [
        ("users", "country", "Brazil"),
        ("orders", "status", "complete"),
        ("orders", "status", "Shipped"),
    ]
