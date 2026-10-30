"""Number grounding: figures in an answer must come from the query results.

A figure counts as grounded when it equals a number in the evidence (allowing rounding, and
fractions shown as percentages) or a simple derivation of two evidence numbers: difference,
sum, ratio, percent change, or percentage-point change. Anything else is flagged, so the agent
can remove or support it before the user sees it. Shared with the offline eval.
"""

from __future__ import annotations

import re

import numpy as np

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")
MAX_EVIDENCE_NUMBERS = 400


def numbers_in(text: str, every: bool = False) -> list[tuple[str, float]]:
    """Numbers in text. By default only those worth checking: skips years and small integers
    (counts like 'top 5'). `every=True` keeps all numbers (for evidence)."""
    out = []
    for raw in (r.rstrip(",") for r in _NUM.findall(text or "")):
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if every:
            out.append((raw, v))
            continue
        if "." not in raw and "," not in raw and 1900 <= v <= 2100:
            continue
        if abs(v) >= 10 or "." in raw:
            out.append((raw, v))
    return out


def _tolerance(v: float) -> float:
    return max(0.51, abs(v) * 0.006)


def _derived(evidence: np.ndarray) -> np.ndarray:
    e = evidence[:, None]
    f = evidence[None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        parts = [
            evidence, evidence * 100,               # values, fractions shown as %
            (e - f).ravel(), (e + f).ravel(),       # differences, sums
            (e / f).ravel(), (e / f * 100).ravel(),  # ratios, shares
            ((e - f) / f * 100).ravel(),            # percent change
            ((e - f) * 100).ravel(),                # percentage-point change of fractions
        ]
    out = np.concatenate(parts)
    return out[np.isfinite(out)]


def ungrounded_numbers(answer: str, evidence_texts: list[str], allowed_texts: list[str] = ()) -> list[str]:
    """Figures in `answer` not found in, or simply derivable from, the evidence."""
    candidates = numbers_in(answer)
    if not candidates:
        return []
    allowed = {v for t in allowed_texts for _, v in numbers_in(t, every=True)}
    values = list(dict.fromkeys([0.0, *(v for t in evidence_texts for _, v in numbers_in(t, every=True))]))
    # Direct matches use every number; pairwise derivations are capped to keep the check fast.
    pool = np.concatenate([np.array(values, dtype=float) * k for k in (1, 100)]
                          + [_derived(np.array(values[:MAX_EVIDENCE_NUMBERS], dtype=float))])
    missing = []
    for raw, v in candidates:
        if v in allowed:
            continue
        if not np.any(np.abs(pool - v) <= _tolerance(v)) and not np.any(np.abs(pool + v) <= _tolerance(v)):
            missing.append(raw)
    return list(dict.fromkeys(missing))
