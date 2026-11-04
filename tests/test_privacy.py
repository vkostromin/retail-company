"""Scoped order sizes, demographic lookups of individuals, and small-group suppression."""

import pandas as pd
import pytest

from retail_agent.safety.privacy import protect_small_groups
from retail_agent.safety.sql_guard import SQLGuardError, guard_sql
from tests.conftest import DATASET


def test_scoped_orders_recount_items_in_scope(alice):
    sql = guard_sql("SELECT AVG(num_of_item) FROM orders", alice, DATASET).sql
    assert "COUNT(*) AS num_of_item" in sql and "@scope_departments" in sql
    assert "o.num_of_item" not in sql  # the raw, unscoped count is never exposed


def test_ceo_orders_keep_original_count(ceo):
    assert "num_of_item" in guard_sql("SELECT AVG(num_of_item) FROM orders", ceo, DATASET).sql


# Ways to tie age/gender/city to one customer. The first three are the original cases; the rest
# are bypasses of the earlier pattern-based check (literal id filters only) and other attempts.
@pytest.mark.parametrize("sql", [
    "SELECT age, gender, city FROM users WHERE id = 46830",
    "SELECT u.city FROM users u WHERE u.id IN (1, 2, 3)",
    "SELECT u.age FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE oi.user_id = 46830",
    "SELECT age, city FROM users WHERE id BETWEEN 123 AND 123",                     # range instead of =
    "SELECT age, city FROM users WHERE id = 122 + 1",                               # expression instead of literal
    "SELECT age, city FROM users WHERE id > 122 AND id < 124",
    "SELECT u.age, u.city FROM orders o JOIN users u ON u.id = o.user_id WHERE o.order_id = 555",  # via an order
    "SELECT id, age, city FROM users",                                              # every customer, row by row
    "SELECT id AS cid, age AS a, city AS c FROM users",                             # renamed columns
    "WITH x AS (SELECT id, city FROM users) SELECT x.id, x.city FROM x",            # hidden in a CTE
    "SELECT t.c FROM (SELECT city AS c FROM users WHERE id = 7) t",                 # hidden in a subquery
    "SELECT name, (SELECT age FROM users WHERE id = 7) AS a FROM products LIMIT 1",  # scalar subquery
    "SELECT * FROM users",
    "SELECT u.* FROM users u",
    "SELECT TO_JSON_STRING(u) FROM users u",                                        # whole row as a value
    "SELECT gender FROM orders WHERE order_id = 1",                                 # orders carry gender too
    "SELECT gender, MAX(age) AS oldest FROM users GROUP BY gender",                 # extremes are one person
    "SELECT gender, ANY_VALUE(city) FROM users GROUP BY gender",
    "SELECT gender, STRING_AGG(city) FROM users GROUP BY gender",
    "SELECT gender, AVG(IF(id = 123, age, NULL)) FROM users GROUP BY gender",       # singled out inside an aggregate
    "SELECT city, SUM(IF(id = 123, 1, 0)) AS hit FROM users GROUP BY city",         # membership test per city
    "SELECT id, AVG(age) OVER () FROM users",                                       # window, not a group
    "SELECT city, COUNT(*) OVER (PARTITION BY city) FROM users",
    "SELECT oi.user_id, u.age, SUM(oi.sale_price) FROM order_items oi JOIN users u ON u.id = oi.user_id "
    "GROUP BY oi.user_id, u.age",                                                   # one group per customer
    "SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM order_items oi WHERE oi.user_id = u.id AND u.age = 34)",
])
def test_demographics_of_individual_customers_rejected(sql, alice, ceo):
    for principal in (alice, ceo):
        with pytest.raises(SQLGuardError) as e:
            guard_sql(sql, principal, DATASET)
        assert e.value.code == "individual_demographics", sql


# Aggregates over demographics run, with a minimum group size added to the SELECT that reads them.
@pytest.mark.parametrize("sql", [
    "SELECT city, COUNT(*) AS customers FROM users GROUP BY city",
    "SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING n > 100",
    "SELECT gender, COUNT(*) FROM orders GROUP BY gender",
    "SELECT COUNT(*) FROM users WHERE id = 123 AND age = 34",                        # membership test: suppressed
    "SELECT AVG(age) FROM users WHERE id BETWEEN 1 AND 3",                           # 3 customers: suppressed
    "SELECT b.city, b.n FROM (SELECT city, COUNT(*) AS n FROM users GROUP BY city) b",
    "WITH spend AS (SELECT user_id, SUM(sale_price) AS s FROM order_items GROUP BY user_id) "
    "SELECT u.gender, AVG(spend.s) AS avg_spend FROM spend JOIN users u ON u.id = spend.user_id GROUP BY u.gender",
    "SELECT CASE WHEN u.age < 25 THEN '<25' ELSE '25+' END AS age_band, u.gender, COUNT(DISTINCT u.id) AS customers, "
    "SUM(IF(oi.status NOT IN ('Cancelled','Returned'), oi.sale_price, 0)) / COUNT(DISTINCT u.id) AS rpc "
    "FROM users u JOIN order_items oi ON oi.user_id = u.id GROUP BY age_band, u.gender",
])
def test_demographic_aggregates_get_minimum_group_size(sql, alice):
    g = guard_sql(sql, alice, DATASET)
    assert g.min_group_size == 5
    assert "HAVING" in g.sql and "COUNT(DISTINCT" in g.sql and ">= 5" in g.sql


def test_existing_having_is_kept(alice):
    sql = guard_sql("SELECT city, COUNT(*) AS n FROM users GROUP BY city HAVING n > 100", alice, DATASET).sql
    assert "n > 100 AND COUNT(DISTINCT users.id) >= 5" in sql


@pytest.mark.parametrize("sql", [
    "SELECT SUM(sale_price) FROM order_items WHERE user_id = 46830",  # spend of one customer: no demographics
    "SELECT id, country, traffic_source FROM users LIMIT 5",          # coarse attributes are not quasi-identifiers
    "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status ORDER BY orders",  # alias named like the table
    "SELECT oi.user_id, u.country, SUM(oi.sale_price) AS spend FROM order_items oi JOIN users u ON u.id = oi.user_id "
    "GROUP BY oi.user_id, u.country ORDER BY spend DESC LIMIT 10",
])
def test_queries_without_demographics_unchanged(sql, alice):
    assert guard_sql(sql, alice, DATASET).min_group_size is None


def test_customer_rows_lose_demographics():
    df = pd.DataFrame({"user_id": [1, 2], "age": [30, 40], "city": ["A", "B"], "spend": [10.0, 20.0]})
    out, report = protect_small_groups(df)
    assert list(out.columns) == ["user_id", "spend"] and report["dropped_columns"] == ["age", "city"]


def test_small_groups_are_suppressed():
    df = pd.DataFrame({"city": ["Big", "Tiny"], "customers": [120, 2], "revenue": [9000.0, 150.0]})
    out, report = protect_small_groups(df)
    assert out["city"].tolist() == ["Big"] and report["suppressed_groups"] == 1


def test_rows_keyed_by_plain_id_lose_demographics():
    out, report = protect_small_groups(pd.DataFrame({"id": [1], "age": [30], "spend": [10.0]}))
    assert list(out.columns) == ["id", "spend"] and report["dropped_columns"] == ["age"]


def test_aggregates_without_demographics_untouched():
    df = pd.DataFrame({"brand": ["A"], "customers": [1]})
    assert protect_small_groups(df)[0].equals(df)
