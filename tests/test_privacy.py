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


@pytest.mark.parametrize("sql", [
    "SELECT age, gender, city FROM users WHERE id = 46830",
    "SELECT u.city FROM users u WHERE u.id IN (1, 2, 3)",
    "SELECT u.age FROM order_items oi JOIN users u ON u.id = oi.user_id WHERE oi.user_id = 46830",
])
def test_demographics_of_specific_customers_rejected(sql, alice):
    with pytest.raises(SQLGuardError) as e:
        guard_sql(sql, alice, DATASET)
    assert e.value.code == "individual_demographics"


@pytest.mark.parametrize("sql", [
    "SELECT SUM(sale_price) FROM order_items WHERE user_id = 46830",  # spend of one customer: fine
    "SELECT city, COUNT(*) AS customers FROM users GROUP BY city",     # group demographics: fine
])
def test_allowed_queries(sql, alice):
    guard_sql(sql, alice, DATASET)


def test_customer_rows_lose_demographics():
    df = pd.DataFrame({"user_id": [1, 2], "age": [30, 40], "city": ["A", "B"], "spend": [10.0, 20.0]})
    out, report = protect_small_groups(df)
    assert list(out.columns) == ["user_id", "spend"] and report["dropped_columns"] == ["age", "city"]


def test_small_groups_are_suppressed():
    df = pd.DataFrame({"city": ["Big", "Tiny"], "customers": [120, 2], "revenue": [9000.0, 150.0]})
    out, report = protect_small_groups(df)
    assert out["city"].tolist() == ["Big"] and report["suppressed_groups"] == 1


def test_aggregates_without_demographics_untouched():
    df = pd.DataFrame({"brand": ["A"], "customers": [1]})
    assert protect_small_groups(df)[0].equals(df)
