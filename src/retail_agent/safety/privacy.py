"""Small-group protection on query results (k-anonymity style, k = 5).

The SQL guard already rejects demographic lookups for specific customer ids. This layer
covers what reaches the model:
  A. individual-level rows (a customer id column) never carry age/gender/city;
  B. demographic breakdowns drop groups smaller than K customers when the result has a
     customer-count column.
Production: BigQuery aggregation threshold analysis rules on the users view
(privacy_unit_column = user_id, threshold = 5) enforce B in the database.
"""

from __future__ import annotations

import re

import pandas as pd

K = 5
_IDENTIFIER = re.compile(r"^(user_id|customer_id|user|customer|users_id)$", re.I)
_QUASI = re.compile(r"^(age|gender|city|.*_age|.*_city|age_.*|.*_gender)$", re.I)
_GROUP_SIZE = re.compile(r"(customers|users|buyers|shoppers|people|customer_count|user_count)$", re.I)


def protect_small_groups(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    report: dict = {}
    quasi = [c for c in df.columns if _QUASI.match(str(c))]
    if not quasi:
        return df, report
    if any(_IDENTIFIER.match(str(c)) for c in df.columns):
        report["dropped_columns"] = quasi
        return df.drop(columns=quasi), report
    size_cols = [c for c in df.columns if _GROUP_SIZE.search(str(c)) and pd.api.types.is_numeric_dtype(df[c])]
    if size_cols:
        small = df[size_cols[0]] < K
        if small.any():
            report["suppressed_groups"] = int(small.sum())
            df = df[~small]
    return df, report
