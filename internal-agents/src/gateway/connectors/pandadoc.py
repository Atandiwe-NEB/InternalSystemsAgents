"""PandaDoc connector — fetches document/contract status.

Real API docs:
  List documents : https://developers.pandadoc.com/reference/list-documents
  Document detail: https://developers.pandadoc.com/reference/get-document-details

Auth: API key via Authorization: API-Key header.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.models.schemas import Currency, PandaDocContract, PandaDocStatus

_BASE = "https://api.pandadoc.com/public/v1"


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_MOCK_CONTRACTS: list[PandaDocContract] = [
    PandaDocContract(
        document_id="pdoc-AAA",
        name="Acme Corp — Platform Licence Agreement",
        status=PandaDocStatus.COMPLETED,
        deal_id="deal-001",
        recipient_name="Thabo Nkosi",
        recipient_email="thabo@acmecorp.co.za",
        total_value=Decimal("150000.00"),
        currency=Currency.ZAR,
        created_at=datetime(2026, 3, 25, 10, 0),
        sent_at=datetime(2026, 3, 26, 9, 0),
        completed_at=datetime(2026, 3, 28, 14, 30),
    ),
    PandaDocContract(
        document_id="pdoc-BBB",
        name="BlueSky Ltd — Integration Project SOW",
        status=PandaDocStatus.SENT,
        deal_id="deal-002",
        recipient_name="Sarah van der Berg",
        recipient_email="sarah@bluesky.co.za",
        total_value=Decimal("82500.00"),
        currency=Currency.ZAR,
        created_at=datetime(2026, 4, 10, 11, 0),
        sent_at=datetime(2026, 4, 11, 9, 0),
        expiry_date=date(2026, 4, 25),
    ),
    PandaDocContract(
        document_id="pdoc-CCC",
        name="Vertex SA — Annual Support Agreement",
        status=PandaDocStatus.COMPLETED,
        deal_id="deal-003",
        recipient_name="Vertex Finance",
        recipient_email="finance@vertexsa.co.za",
        total_value=Decimal("48000.00"),
        currency=Currency.ZAR,
        created_at=datetime(2026, 3, 10, 9, 0),
        sent_at=datetime(2026, 3, 11, 9, 0),
        completed_at=datetime(2026, 3, 13, 16, 0),
    ),
    PandaDocContract(
        document_id="pdoc-DDD",
        name="Expired Draft — Old Prospect",
        status=PandaDocStatus.EXPIRED,
        deal_id=None,
        total_value=Decimal("15000.00"),
        currency=Currency.ZAR,
        created_at=datetime(2026, 1, 5, 9, 0),
        expiry_date=date(2026, 2, 5),
    ),
]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class PandaDocConnector:
    """Thin async wrapper around the PandaDoc Public API v1."""

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        self._mock = settings.mock_mode if mock is None else mock
        self._headers = {
            "Authorization": f"API-Key {settings.pandadoc_api_key}",
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE}{path}", headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Iterate PandaDoc cursor-based pagination.

        TODO: PandaDoc uses page + count params. Max count is 100:
        https://developers.pandadoc.com/reference/list-documents
        """
        results: list[dict] = []
        base_params = dict(params or {})
        base_params["count"] = 100
        page = 1
        while True:
            base_params["page"] = page
            data = await self._get(path, params=base_params)
            batch = data.get("results", [])
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    def _parse_document(self, raw: dict) -> PandaDocContract:
        status_raw = raw.get("status", "document.draft")
        try:
            status = PandaDocStatus(status_raw)
        except ValueError:
            status = PandaDocStatus.DRAFT

        # deal_id lives in metadata — only present on detail responses (not list summaries)
        metadata = raw.get("metadata", {}) or {}
        deal_id = metadata.get("hubspot_deal_id") or metadata.get("deal_id")

        recipients = raw.get("recipients", []) or []
        first_recipient = recipients[0] if recipients else {}
        first = (first_recipient.get("first_name") or "").strip()
        last = (first_recipient.get("last_name") or "").strip()
        recipient_name = " ".join(filter(None, [first, last])) or None

        return PandaDocContract(
            document_id=raw.get("id", ""),
            name=raw.get("name", ""),
            status=status,
            deal_id=deal_id,
            recipient_name=recipient_name,
            recipient_email=first_recipient.get("email"),
            total_value=_decimal((raw.get("grand_total") or {}).get("amount")),
            currency=Currency.ZAR,
            created_at=_parse_datetime(raw.get("date_created")),
            sent_at=_parse_datetime(raw.get("date_sent")),
            completed_at=_parse_datetime(raw.get("date_completed")),
            expiry_date=_parse_date(raw.get("expiration_date")),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def _fetch_document_detail(self, doc_id: str) -> dict:
        """Fetch a single document's full detail, including metadata."""
        return await self._get(f"/documents/{doc_id}/details")

    async def fetch_contracts(
        self,
        status: PandaDocStatus | None = None,
    ) -> list[PandaDocContract]:
        """Fetch all documents from PandaDoc, optionally filtered by status.

        The list endpoint returns summaries without metadata (deal_id lives there),
        so each document's detail is fetched in parallel to populate deal_id.
        """
        if self._mock:
            logger.debug("PandaDocConnector.fetch_contracts → mock")
            if status:
                return [c for c in _MOCK_CONTRACTS if c.status == status]
            return _MOCK_CONTRACTS

        params: dict[str, Any] = {}
        if status:
            params["status"] = status.value

        summaries = await self._paginate("/documents", params=params)
        if not summaries:
            return []

        # Fetch full detail for each doc in parallel to get metadata.hubspot_deal_id
        details = await asyncio.gather(
            *[self._fetch_document_detail(s["id"]) for s in summaries],
            return_exceptions=True,
        )
        contracts = []
        for summary, detail in zip(summaries, details):
            if isinstance(detail, BaseException):
                logger.warning(f"[pandadoc] detail fetch failed for {summary.get('id')}: {detail}")
                contracts.append(self._parse_document(summary))
            else:
                contracts.append(self._parse_document(detail))
        return contracts
