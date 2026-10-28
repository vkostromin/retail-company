"""Dependency container. Tests inject fakes for `bq` and `llm`."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from .agent.llm import LLMGateway
from .config import Settings
from .data.bq_client import BigQueryRunner
from .data.sqlite import Database
from .golden import GoldenStore
from .memory.preferences import PreferenceStore
from .observability.tracer import Tracer
from .persona.store import PersonaStore
from .reports.store import ReportStore
from .resilience.faults import FaultInjector
from .safety.entitlements import Directory
from .safety.sql_guard import LOGICAL_TABLES, PII_COLUMNS


class TTLCache:
    """Small in-process LRU+TTL cache for query results (prod: Memorystore/Redis)."""

    def __init__(self, maxsize: int = 256, ttl_s: float = 900):
        self.maxsize, self.ttl_s = maxsize, ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item or time.monotonic() - item[0] > self.ttl_s:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return item[1]

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


@dataclass
class Services:
    settings: Settings
    db: Database
    bq: BigQueryRunner
    llm: LLMGateway
    golden: GoldenStore
    reports: ReportStore
    prefs: PreferenceStore
    personas: PersonaStore
    tracer: Tracer
    directory: Directory
    faults: FaultInjector
    cache: TTLCache = field(default_factory=TTLCache)
    _columns: dict[str, list[dict]] = field(default_factory=dict)

    def table_columns(self, table: str) -> list[dict]:
        """Live schema (via the provided BigQueryRunner.get_table_schema), PII removed."""
        if table not in self._columns:
            allowed = set(LOGICAL_TABLES[table])
            try:
                cols = [
                    {"name": c["name"], "type": c["type"]}
                    for c in self.bq.get_table_schema(table)
                    if c["name"] in allowed and c["name"] not in PII_COLUMNS
                ]
            except Exception:  # noqa: BLE001 - static fallback keeps the agent useful
                cols = [{"name": c, "type": "?"} for c in LOGICAL_TABLES[table]]
            self._columns[table] = cols
        return self._columns[table]


def build_services(settings: Settings, *, bq: BigQueryRunner | None = None, llm: LLMGateway | None = None,
                   golden: GoldenStore | None = None) -> Services:
    settings.ensure_dirs()
    faults = FaultInjector(settings.fake_fail)
    db = Database(settings.app_db)
    return Services(
        settings=settings,
        db=db,
        bq=bq or BigQueryRunner(
            project_id=settings.gcp_project, dataset_id=settings.bq_dataset, location=settings.bq_location,
            max_bytes_billed=settings.max_bytes_billed, timeout_s=settings.query_timeout_s, faults=faults,
        ),
        llm=llm or LLMGateway(settings, faults),
        golden=golden or GoldenStore(settings, faults),
        reports=ReportStore(db, settings.reports_export_dir),
        prefs=PreferenceStore(db),
        personas=PersonaStore(db, settings.persona_seed_file),
        tracer=Tracer(settings.traces_dir),
        directory=Directory(settings.users_file),
        faults=faults,
    )
