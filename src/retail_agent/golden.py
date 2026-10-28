"""Golden Knowledge: analyst-authored Question -> SQL -> Report trios (Req 1).

Local prototype of the production pipeline (GCS bucket -> ingestion -> vector index):
trios are YAML files; questions are embedded with Gemini embeddings (cached on disk by
content hash); retrieval is cosine top-k with a similarity floor. If the embedding API is
down we degrade to BM25 keyword retrieval instead of failing the turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml
from rank_bm25 import BM25Okapi

from .config import Settings
from .resilience.faults import FaultInjector
from .resilience.retry import call_with_retry

log = logging.getLogger(__name__)


@dataclass
class Trio:
    id: str
    question: str
    interpretation: str
    sql: str
    report: str
    tags: list[str] = field(default_factory=list)

    def as_prompt(self) -> str:
        return (
            f"### Example `{self.id}`\nQ: {self.question}\n"
            f"How the analyst interpreted it: {self.interpretation.strip()}\n"
            f"SQL:\n```sql\n{self.sql.strip()}\n```\nAnalyst conclusion: {self.report.strip()}"
        )


def load_trios(directory: Path) -> list[Trio]:
    trios = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        trios.append(Trio(**data))
    return trios


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class GoldenStore:
    def __init__(self, settings: Settings, faults: FaultInjector | None = None):
        self.settings = settings
        self.faults = faults or FaultInjector()
        self.trios = load_trios(settings.golden_dir)
        self._bm25 = BM25Okapi([_tokens(t.question + " " + " ".join(t.tags)) for t in self.trios]) if self.trios else None
        self._matrix: np.ndarray | None = None
        self._client = None
        self.mode = "uninitialized"

    # -- embeddings ---------------------------------------------------------------------
    def _genai(self):
        if self._client is None:
            from google import genai

            if self.settings.use_vertex:
                self._client = genai.Client(vertexai=True, project=self.settings.gcp_project,
                                            location=self.settings.vertex_location)
            else:
                self._client = genai.Client(api_key=self.settings.google_api_key)
        return self._client

    def _embed(self, texts: list[str], task: str) -> np.ndarray:
        from google.genai import types

        def call():
            self.faults.check("embeddings")
            resp = self._genai().models.embed_content(
                model=self.settings.embedding_model, contents=texts,
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=768),
            )
            return np.array([e.values for e in resp.embeddings], dtype=np.float32)

        vecs = call_with_retry(call, attempts=2, initial=0.5, max_wait=4)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    def _index(self) -> np.ndarray:
        if self._matrix is not None:
            return self._matrix
        digest = hashlib.sha256(
            (self.settings.embedding_model + "".join(t.id + t.question for t in self.trios)).encode()
        ).hexdigest()[:16]
        cache = self.settings.data_dir / f"golden_index_{digest}.json"
        if cache.exists():
            self._matrix = np.array(json.loads(cache.read_text()), dtype=np.float32)
        else:
            self._matrix = self._embed([t.question for t in self.trios], "RETRIEVAL_DOCUMENT")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(self._matrix.tolist()))
        return self._matrix

    # -- retrieval ----------------------------------------------------------------------
    def search(self, question: str, k: int | None = None) -> tuple[list[tuple[Trio, float]], str]:
        """Return [(trio, score)] and the retrieval mode used ('semantic' | 'keyword')."""
        if not self.trios:
            return [], "empty"
        k = k or self.settings.golden_top_k
        try:
            matrix = self._index()
            q = self._embed([question], "RETRIEVAL_QUERY")[0]
            scores = matrix @ q
            order = np.argsort(-scores)[:k]
            hits = [(self.trios[i], float(scores[i])) for i in order
                    if scores[i] >= self.settings.golden_min_similarity]
            self.mode = "semantic"
            return hits, "semantic"
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the turn
            log.warning("embedding retrieval unavailable, falling back to BM25: %s", exc)
            scores = self._bm25.get_scores(_tokens(question))
            order = np.argsort(-scores)[:k]
            top = float(scores[order[0]]) if len(order) else 0.0
            hits = [(self.trios[i], float(scores[i] / top) if top else 0.0) for i in order if scores[i] > 1.0]
            self.mode = "keyword"
            return hits, "keyword"
