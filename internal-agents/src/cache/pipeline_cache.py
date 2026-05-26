"""In-memory TTL cache for pipeline stage results.

Shared across requests on a single OrchestratorAgent instance so that
follow-up questions about the same data reuse already-fetched results
without repeating expensive API calls or LLM stages.

Cache hierarchy
---------------
  Level 1 — CollectionResult   : keyed on canonical CollectionRequest
  Level 2 — ProcessedDataset   : keyed on the same collection key
                                 (processing is deterministic from collection)
  Level 3 — AnalysisResult     : keyed on collection key + question + focus sources
                                 (analysis depends on the question asked)

For multi-process or distributed deployments swap the dicts for a
Redis-backed store; the public interface is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from loguru import logger

from src.models.schemas import (
    AnalysisResult,
    CollectionRequest,
    CollectionResult,
    ProcessedDataset,
)

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float


class PipelineCache:
    """TTL-keyed in-memory cache for all three pipeline stages."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._collections: dict[str, _Entry[CollectionResult]] = {}
        self._datasets: dict[str, _Entry[ProcessedDataset]] = {}
        self._analyses: dict[str, _Entry[AnalysisResult]] = {}
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # CollectionResult
    # ------------------------------------------------------------------

    def get_collection(self, request: CollectionRequest) -> CollectionResult | None:
        return self._get(self._collections, self.collection_key(request), "collection")

    def set_collection(self, result: CollectionResult) -> None:
        key = self.collection_key(result.request)
        self._set(self._collections, key, result)
        logger.debug(f"[cache] stored collection key={key[:8]}")

    # ------------------------------------------------------------------
    # ProcessedDataset — keyed identically to its source collection
    # ------------------------------------------------------------------

    def get_dataset(self, collection_key: str) -> ProcessedDataset | None:
        return self._get(self._datasets, collection_key, "dataset")

    def set_dataset(self, collection_key: str, dataset: ProcessedDataset) -> None:
        self._set(self._datasets, collection_key, dataset)
        logger.debug(f"[cache] stored dataset key={collection_key[:8]}")

    # ------------------------------------------------------------------
    # AnalysisResult — keyed on collection + question + focus
    # ------------------------------------------------------------------

    def get_analysis(
        self, collection_key: str, question: str, focus: list[str]
    ) -> AnalysisResult | None:
        key = self._analysis_key(collection_key, question, focus)
        return self._get(self._analyses, key, "analysis")

    def set_analysis(
        self,
        collection_key: str,
        question: str,
        focus: list[str],
        result: AnalysisResult,
    ) -> None:
        key = self._analysis_key(collection_key, question, focus)
        self._set(self._analyses, key, result)
        logger.debug(f"[cache] stored analysis key={key[:8]}")

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def collection_key(self, request: CollectionRequest) -> str:
        """Stable 16-char hex key for a CollectionRequest."""
        payload = {
            "sources": sorted(s.value for s in request.sources),
            "date_range": (
                {
                    "start": request.date_range.start.isoformat(),
                    "end": request.date_range.end.isoformat(),
                }
                if request.date_range
                else None
            ),
            "project_keys": sorted(request.project_keys),
            "sprint_id": request.sprint_id,
            "extra_filters": request.extra_filters,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]

    def stats(self) -> dict[str, int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "cached_collections": len(self._collections),
            "cached_datasets": len(self._datasets),
            "cached_analyses": len(self._analyses),
            "ttl_seconds": self._ttl,
        }

    def invalidate(self) -> None:
        """Flush all cached entries."""
        self._collections.clear()
        self._datasets.clear()
        self._analyses.clear()
        logger.info("[cache] invalidated all entries")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, store: dict[str, _Entry[T]], key: str, label: str) -> T | None:
        entry = store.get(key)
        if entry is None or time.monotonic() > entry.expires_at:
            if entry is not None:
                del store[key]
            self._misses += 1
            return None
        self._hits += 1
        logger.info(f"[cache] HIT {label} key={key[:8]} — skipping pipeline stage")
        return entry.value

    def _set(self, store: dict[str, _Entry], key: str, value: object) -> None:
        store[key] = _Entry(value=value, expires_at=time.monotonic() + self._ttl)

    @staticmethod
    def _analysis_key(collection_key: str, question: str, focus: list[str]) -> str:
        payload = {
            "collection": collection_key,
            "question": question.lower().strip(),
            "focus": sorted(focus),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()[:16]
