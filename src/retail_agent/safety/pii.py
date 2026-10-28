"""PII detection/redaction for query results and the final answer (defense in depth).

The SQL guard already makes PII columns unreachable; this layer catches anything that
slips through (aliases, free-text fields, model hallucinations) before it reaches the
LLM or the user. Production: Cloud DLP / Sensitive Data Protection with the same hooks.
"""

from __future__ import annotations

import re
from collections import Counter

import pandas as pd

_PII_COLUMN_RE = re.compile(
    r"(e[-_]?mail|first[-_]?name|last[-_]?name|full[-_]?name|street|address|postal|zip[-_]?code|"
    r"latitude|longitude|\blat\b|\blng\b|\blon\b|geom|phone|ssn|passport)",
    re.IGNORECASE,
)

_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    # Requires separators so plain numbers (revenue, IDs) are never matched.
    "phone": re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w])"),
    "street_address": re.compile(
        r"\b\d{1,6}\s+(?:[A-Z][A-Za-z]+\s+){1,4}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace)\b\.?"
    ),
    "geo_point": re.compile(r"POINT\s*\(\s*-?\d+\.\d+\s+-?\d+\.\d+\s*\)", re.IGNORECASE),
    "lat_long": re.compile(r"(?<![\d.])-?\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}(?![\d.])"),
}


def is_pii_column(name: str) -> bool:
    return bool(_PII_COLUMN_RE.search(name))


def redact_text(text: str) -> tuple[str, Counter]:
    counts: Counter = Counter()
    if not text:
        return text, counts
    for kind, pattern in _PATTERNS.items():
        text, n = pattern.subn(f"[REDACTED {kind.upper()}]", text)
        if n:
            counts[kind] += n
    return text, counts


def sanitize_dataframe(df: pd.DataFrame) -> tuple[pd.DataFrame, Counter]:
    """Drop PII-looking columns and redact PII patterns inside string cells."""
    counts: Counter = Counter()
    pii_cols = [c for c in df.columns if is_pii_column(str(c))]
    if pii_cols:
        counts["pii_column"] += len(pii_cols)
        df = df.drop(columns=pii_cols)
    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
            def _redact(v):
                if isinstance(v, str):
                    new, c = redact_text(v)
                    counts.update(c)
                    return new
                return v

            df[col] = df[col].map(_redact)
    return df, counts
