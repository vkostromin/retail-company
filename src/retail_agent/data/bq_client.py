import logging
import re
import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Sequence

import pandas as pd
from google.cloud import bigquery

from ..resilience.breaker import CircuitBreaker
from ..resilience.errors import DependencyUnavailable
from ..resilience.faults import FaultInjector
from ..resilience.retry import call_with_retry, is_transient


@dataclass
class QueryResult:
    df: pd.DataFrame
    bytes_processed: int
    cache_hit: bool
    job_id: str
    duration_ms: int


def _label(value: str) -> str:
    """BigQuery label values: lowercase letters, digits, '-', '_', max 63 chars."""
    return re.sub(r"[^a-z0-9_-]", "-", value.lower())[:63]


class BigQueryRunner:
    """A lean BigQuery client for executing SQL queries and returning DataFrame results.

    Extended for the agent with dry-runs (free syntax/cost check), query parameters,
    a hard `maximum_bytes_billed` cap, job timeouts, job labels for cost attribution,
    retries on transient errors and a circuit breaker.
    """

    def __init__(
        self,
        project_id: Optional[str] = None,
        dataset_id: Optional[str] = "bigquery-public-data.thelook_ecommerce",
        location: str = "US",
        max_bytes_billed: Optional[int] = None,
        timeout_s: int = 60,
        faults: Optional[FaultInjector] = None,
    ) -> None:
        """Initialize BigQuery client.

        Args:
            project_id: Google Cloud project ID. If None, uses default credentials.
            dataset_id: BigQuery dataset ID. If None, uses default dataset.
        """
        logging.info("Initializing BigQuery client")
        try:
            self.client = bigquery.Client(project=project_id, location=location)
            self.dataset_id = dataset_id
            self.max_bytes_billed = max_bytes_billed
            self.timeout_s = timeout_s
            self.faults = faults or FaultInjector()
            self.breaker = CircuitBreaker("bigquery")
            logging.info(f"BigQuery client initialized for dataset: {self.dataset_id}")
        except Exception as e:
            logging.error(f"Failed to initialize BigQuery client: {str(e)}")
            raise

    def execute_query(self, sql_query: str) -> pd.DataFrame:
        """Execute a SQL query and return results as a DataFrame.

        Args:
            sql_query: The SQL query to execute.

        Returns:
            DataFrame containing the query results.

        Raises:
            Exception: If query execution fails.
        """
        return self.run(sql_query).df

    def dry_run(self, sql_query: str, params: Sequence[bigquery.ScalarQueryParameter] = ()) -> int:
        """Validate a query without running it. Free; returns the bytes it would scan.

        Raises google.api_core.exceptions.BadRequest on syntax/semantic errors.
        """
        self.faults.check("bigquery")
        config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=list(params))

        def _call() -> int:
            job = self.client.query(sql_query, job_config=config)
            return int(job.total_bytes_processed or 0)

        return self._guarded(_call)

    def run(
        self,
        sql_query: str,
        params: Sequence[bigquery.ScalarQueryParameter] = (),
        max_rows: Optional[int] = None,
        labels: Optional[Dict[str, str]] = None,
        on_retry=None,
    ) -> QueryResult:
        """Execute with cost cap, timeout, retries on transient errors and a circuit breaker."""
        config = bigquery.QueryJobConfig(
            query_parameters=list(params),
            labels={_label(k): _label(v) for k, v in (labels or {}).items()},
        )
        if self.max_bytes_billed:
            config.maximum_bytes_billed = self.max_bytes_billed

        def _call() -> QueryResult:
            self.faults.check("bigquery")
            started = time.monotonic()
            logging.info("Executing BigQuery query")
            job = self.client.query(sql_query, job_config=config, timeout=self.timeout_s)
            rows = job.result(timeout=self.timeout_s, max_results=max_rows)
            df = rows.to_dataframe(create_bqstorage_client=False)
            logging.info(f"Query completed successfully, returned {len(df)} rows")
            return QueryResult(
                df=df,
                bytes_processed=int(job.total_bytes_processed or 0),
                cache_hit=bool(job.cache_hit),
                job_id=job.job_id,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return self._guarded(_call, on_retry=on_retry)

    def _guarded(self, fn, on_retry=None):
        try:
            return self.breaker.call(
                lambda: call_with_retry(fn, attempts=3, initial=0.5, max_wait=8, on_retry=on_retry),
                counts_as_failure=is_transient,
            )
        except Exception as e:
            if is_transient(e):
                logging.error(f"BigQuery execution failed: {str(e)}")
                raise DependencyUnavailable("bigquery", str(e)) from e
            raise

    def get_table_schema(self, table_name: str) -> List[Dict[str, Any]]:
        """Get schema information for a specific table.

        Args:
            table_name: Name of the table (orders, order_items, products, users).

        Returns:
            List of dictionaries containing column information.
        """
        try:
            table_ref = f"{self.dataset_id}.{table_name}"
            table = self._guarded(lambda: self.client.get_table(table_ref))
            schema_info = []
            for field in table.schema:
                schema_info.append({
                    "name": field.name,
                    "type": field.field_type,
                    "mode": field.mode,
                    "description": field.description or ""
                })
            logging.info(f"Retrieved schema for table {table_name}")
            return schema_info
        except Exception as e:
            logging.error(f"Failed to get schema for table {table_name}: {str(e)}")
            raise
