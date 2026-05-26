"""HubSpot connector — fetches deals, leads, invoices, and pricing.

Real API docs:
  Deals    : https://developers.hubspot.com/docs/api/crm/deals
  Contacts : https://developers.hubspot.com/docs/api/crm/contacts
  Invoices : https://developers.hubspot.com/docs/api/crm/invoices
  Products : https://developers.hubspot.com/docs/api/crm/products
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.models.schemas import (
    Currency,
    DateRange,
    DealStage,
    HubSpotDeal,
    HubSpotInvoice,
    HubSpotLead,
    HubSpotPricing,
)

_BASE = "https://api.hubapi.com"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_MOCK_DEALS: list[HubSpotDeal] = [
    HubSpotDeal(
        id="deal-001",
        name="Acme Corp — Platform Licence",
        stage=DealStage.CLOSED_WON,
        amount=Decimal("150000.00"),
        currency=Currency.ZAR,
        close_date=date(2026, 3, 31),
        owner="sales@nebula.co.za",
        pandadoc_contract_id="pdoc-AAA",
    ),
    HubSpotDeal(
        id="deal-002",
        name="BlueSky Ltd — Integration Project",
        stage=DealStage.CONTRACT_SENT,
        amount=Decimal("82500.00"),
        currency=Currency.ZAR,
        close_date=date(2026, 4, 15),
        owner="sales@nebula.co.za",
        pandadoc_contract_id=None,  # contract not yet sent
    ),
    HubSpotDeal(
        id="deal-003",
        name="Vertex SA — Annual Support",
        stage=DealStage.CLOSED_WON,
        amount=Decimal("48000.00"),
        currency=Currency.ZAR,
        close_date=date(2026, 3, 15),
        owner="sales@nebula.co.za",
        pandadoc_contract_id="pdoc-CCC",
    ),
    HubSpotDeal(
        id="deal-004",
        name="NovaTech — Discovery Phase",
        stage=DealStage.QUALIFIED,
        amount=Decimal("25000.00"),
        currency=Currency.ZAR,
        close_date=date(2026, 5, 30),
        owner="sales@nebula.co.za",
    ),
]

_MOCK_LEADS: list[HubSpotLead] = [
    HubSpotLead(
        id="contact-001",
        first_name="Thabo",
        last_name="Nkosi",
        email="thabo@acmecorp.co.za",
        company="Acme Corp",
        lifecycle_stage="customer",
        lead_status="converted",
    ),
    HubSpotLead(
        id="contact-002",
        first_name="Sarah",
        last_name="van der Berg",
        email="sarah@bluesky.co.za",
        company="BlueSky Ltd",
        lifecycle_stage="opportunity",
        lead_status="in_progress",
    ),
    HubSpotLead(
        id="contact-003",
        first_name="James",
        last_name="Osei",
        email="james@novatech.io",
        company="NovaTech",
        lifecycle_stage="lead",
        lead_status="new",
    ),
]

_MOCK_INVOICES: list[HubSpotInvoice] = [
    HubSpotInvoice(
        id="inv-001",
        deal_id="deal-001",
        amount=Decimal("75000.00"),
        currency=Currency.ZAR,
        status="paid",
        due_date=date(2026, 4, 7),
        issued_at=datetime(2026, 3, 31, 9, 0),
    ),
    HubSpotInvoice(
        id="inv-002",
        deal_id="deal-001",
        amount=Decimal("75000.00"),
        currency=Currency.ZAR,
        status="sent",
        due_date=date(2026, 5, 7),
        issued_at=datetime(2026, 4, 7, 9, 0),
    ),
    HubSpotInvoice(
        id="inv-003",
        deal_id="deal-003",
        amount=Decimal("48000.00"),
        currency=Currency.ZAR,
        status="paid",
        due_date=date(2026, 4, 1),
        issued_at=datetime(2026, 3, 15, 9, 0),
    ),
]

_MOCK_PRICING: list[HubSpotPricing] = [
    HubSpotPricing(
        product_id="prod-001",
        name="Platform Licence — Annual",
        unit_price=Decimal("150000.00"),
        currency=Currency.ZAR,
        description="Full platform access, 12-month term",
    ),
    HubSpotPricing(
        product_id="prod-002",
        name="Integration Package",
        unit_price=Decimal("82500.00"),
        currency=Currency.ZAR,
        description="Up to 5 custom integrations",
    ),
    HubSpotPricing(
        product_id="prod-003",
        name="Annual Support — Standard",
        unit_price=Decimal("48000.00"),
        currency=Currency.ZAR,
        description="Business-hours support, 12-month term",
    ),
]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class HubSpotConnector:
    """Thin async wrapper around the HubSpot CRM v3 API."""

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        self._mock = settings.mock_mode if mock is None else mock
        self._headers = {
            "Authorization": f"Bearer {settings.hubspot_token}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: dict) -> Any:
        """POST to HubSpot and return parsed JSON."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{_BASE}{path}", headers=self._headers, json=body)
            response.raise_for_status()
            return response.json()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE}{path}", headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Iterate HubSpot cursor-based pagination, collecting all results."""
        results: list[dict] = []
        after: str | None = None
        base_params = dict(params or {})
        base_params.setdefault("limit", 100)

        while True:
            if after:
                base_params["after"] = after
            data = await self._get(path, params=base_params)
            results.extend(data.get("results", []))
            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after:
                break
        return results

    def _parse_deal(self, raw: dict) -> HubSpotDeal:
        p = raw.get("properties", {})
        stage_raw = p.get("dealstage", "")
        try:
            stage = DealStage(stage_raw)
        except ValueError:
            stage = DealStage.QUALIFIED

        return HubSpotDeal(
            id=raw["id"],
            name=p.get("dealname", ""),
            stage=stage,
            amount=_parse_decimal(p.get("amount")),
            currency=Currency.ZAR,
            close_date=_parse_date(p.get("closedate")),
            owner=p.get("hubspot_owner_id"),
            created_at=_parse_datetime(p.get("createdate")),
            updated_at=_parse_datetime(p.get("hs_lastmodifieddate")),
            pandadoc_contract_id=p.get("pandadoc_document_id"),
        )

    def _parse_lead(self, raw: dict) -> HubSpotLead:
        p = raw.get("properties", {})
        return HubSpotLead(
            id=raw["id"],
            first_name=p.get("firstname", ""),
            last_name=p.get("lastname", ""),
            email=p.get("email"),
            company=p.get("company"),
            lifecycle_stage=p.get("lifecyclestage"),
            lead_status=p.get("hs_lead_status"),
            created_at=_parse_datetime(p.get("createdate")),
        )

    def _parse_invoice(self, raw: dict) -> HubSpotInvoice:
        p = raw.get("properties", {})
        return HubSpotInvoice(
            id=raw["id"],
            deal_id=p.get("hs_associated_deal_id"),
            amount=_parse_decimal(p.get("hs_amount_billed")) or Decimal("0"),
            currency=Currency.ZAR,
            status=p.get("hs_invoice_status", "draft"),
            due_date=_parse_date(p.get("hs_due_date")),
            issued_at=_parse_datetime(p.get("hs_invoice_date")),
        )

    def _parse_product(self, raw: dict) -> HubSpotPricing:
        p = raw.get("properties", {})
        return HubSpotPricing(
            product_id=raw["id"],
            name=p.get("name", ""),
            unit_price=_parse_decimal(p.get("price")) or Decimal("0"),
            currency=Currency.ZAR,
            description=p.get("description"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_deals(self, date_range: DateRange | None = None) -> list[HubSpotDeal]:
        """Fetch CRM deals, optionally filtered by close date range.

        TODO: HubSpot v3 search endpoint for date filtering:
        https://developers.hubspot.com/docs/api/crm/search
        """
        if self._mock:
            logger.debug("HubSpotConnector.fetch_deals → mock")
            if date_range:
                return [
                    d for d in _MOCK_DEALS
                    if d.close_date and date_range.start <= d.close_date <= date_range.end
                ]
            return _MOCK_DEALS

        if date_range:
            # Use the search endpoint for date filtering
            body = {
                "filterGroups": [{
                    "filters": [
                        {"propertyName": "closedate", "operator": "GTE",
                         "value": str(date_range.start)},
                        {"propertyName": "closedate", "operator": "LTE",
                         "value": str(date_range.end)},
                    ]
                }],
                "properties": [
                    "dealname", "dealstage", "amount", "closedate",
                    "hubspot_owner_id", "createdate", "hs_lastmodifieddate",
                    "pandadoc_document_id",
                ],
                "limit": 100,
            }
            data = await self._post("/crm/v3/objects/deals/search", body)
            return [self._parse_deal(r) for r in data.get("results", [])]

        raw_list = await self._paginate(
            "/crm/v3/objects/deals",
            params={"properties": "dealname,dealstage,amount,closedate,hubspot_owner_id,pandadoc_document_id"},
        )
        return [self._parse_deal(r) for r in raw_list]

    async def fetch_leads(self, date_range: DateRange | None = None) -> list[HubSpotLead]:
        """Fetch CRM contacts (used as leads).

        TODO: filter by lifecycle stage or lead status via search:
        https://developers.hubspot.com/docs/api/crm/contacts
        """
        if self._mock:
            logger.debug("HubSpotConnector.fetch_leads → mock")
            return _MOCK_LEADS

        raw_list = await self._paginate(
            "/crm/v3/objects/contacts",
            params={"properties": "firstname,lastname,email,company,lifecyclestage,hs_lead_status,createdate"},
        )
        return [self._parse_lead(r) for r in raw_list]

    async def fetch_invoices(self) -> list[HubSpotInvoice]:
        """Fetch HubSpot invoices.

        TODO: invoices object availability depends on HubSpot tier (Sales Hub Pro+):
        https://developers.hubspot.com/docs/api/crm/invoices
        """
        if self._mock:
            logger.debug("HubSpotConnector.fetch_invoices → mock")
            return _MOCK_INVOICES

        raw_list = await self._paginate(
            "/crm/v3/objects/invoices",
            params={"properties": "hs_amount_billed,hs_invoice_status,hs_due_date,hs_invoice_date,hs_associated_deal_id"},
        )
        return [self._parse_invoice(r) for r in raw_list]

    async def fetch_pricing(self) -> list[HubSpotPricing]:
        """Fetch the product/pricing catalogue.

        TODO: requires Products library (Sales Hub Starter+):
        https://developers.hubspot.com/docs/api/crm/products
        """
        if self._mock:
            logger.debug("HubSpotConnector.fetch_pricing → mock")
            return _MOCK_PRICING

        raw_list = await self._paginate(
            "/crm/v3/objects/products",
            params={"properties": "name,price,description"},
        )
        return [self._parse_product(r) for r in raw_list]
