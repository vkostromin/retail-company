"""Settings: config/settings.yaml, overridden by RA_* environment variables and .env."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(os.environ.get("RA_HOME", Path(__file__).resolve().parents[2]))


class Settings(BaseModel):
    gcp_project: str | None = None
    bq_dataset: str = "bigquery-public-data.thelook_ecommerce"
    bq_location: str = "US"

    agent_model: str = "gemini-3.8-flash"
    fallback_model: str | None = "gemini-2.5-flash"
    guard_model: str = "gemini-3.5-flash-lite"
    judge_model: str = "gemini-3.1-pro-preview"
    embedding_model: str = "gemini-embedding-001"
    vertex_location: str = "global"
    llm_timeout_s: int = 90
    judge_timeout_s: int = 180
    llm_requests_per_minute: int = 60

    max_bytes_billed: int = 1_000_000_000
    query_timeout_s: int = 60
    max_result_rows: int = 500
    preview_rows: int = 40
    max_sql_attempts: int = 4
    max_tool_calls: int = 14
    turn_token_budget: int = 250_000

    bulk_delete_threshold: int = 2
    undo_window_minutes: int = 30

    golden_top_k: int = 3
    golden_min_similarity: float = 0.60

    data_dir: Path = Path(".data")
    cache_dir: Path = Path(".data")  # content-addressed caches, safe to share across data dirs (e.g. evals)
    reports_export_dir: Path = Path("out/reports")

    # Not in YAML: derived from the environment.
    google_api_key: str | None = None
    use_vertex: bool = False
    fake_fail: frozenset[str] = frozenset()

    @property
    def app_db(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def traces_dir(self) -> Path:
        return self.data_dir / "traces"

    @property
    def users_file(self) -> Path:
        return PROJECT_ROOT / "config" / "users.yaml"

    @property
    def persona_seed_file(self) -> Path:
        return PROJECT_ROOT / "config" / "persona.yaml"

    @property
    def golden_dir(self) -> Path:
        return PROJECT_ROOT / "golden" / "trios"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.traces_dir, self.reports_export_dir):
            d.mkdir(parents=True, exist_ok=True)


def _coerce(value: str, current):
    if isinstance(current, bool):
        return value.lower() in {"1", "true", "yes"}
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    return value


def load_settings(settings_file: Path | None = None) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    path = settings_file or PROJECT_ROOT / "config" / "settings.yaml"
    raw: dict = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = {k: v for k, v in raw.items() if v is not None}

    defaults = Settings().model_dump()
    for key, current in defaults.items():
        env_val = os.environ.get(f"RA_{key.upper()}")
        if env_val is not None:
            raw[key] = _coerce(env_val, current)

    raw.setdefault("gcp_project", os.environ.get("GOOGLE_CLOUD_PROJECT"))
    raw["google_api_key"] = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    # Vertex AI (ADC) when explicitly requested or when no AI Studio key is present.
    use_vertex_env = os.environ.get("RA_USE_VERTEX")
    raw["use_vertex"] = (
        use_vertex_env.lower() in {"1", "true", "yes"} if use_vertex_env else not raw["google_api_key"]
    )
    raw["fake_fail"] = frozenset(
        f.strip() for f in os.environ.get("RA_FAKE_FAIL", "").split(",") if f.strip()
    )
    settings = Settings(**raw)
    for attr in ("data_dir", "cache_dir", "reports_export_dir"):
        p = getattr(settings, attr)
        if not p.is_absolute():
            setattr(settings, attr, PROJECT_ROOT / p)

    # ADC may carry a quota project from an unrelated gcloud setup; bill API quota to
    # the project we actually run in.
    if settings.gcp_project:
        os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", settings.gcp_project)
    return settings


@lru_cache
def get_settings() -> Settings:
    return load_settings()
