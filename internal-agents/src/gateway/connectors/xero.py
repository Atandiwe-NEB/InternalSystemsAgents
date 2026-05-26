"""Xero connector — fetches supplier contacts and ACCPAY invoices.

Real API docs:
  Contacts : https://developer.xero.com/documentation/api/accounting/contacts
  Invoices : https://developer.xero.com/documentation/api/accounting/invoices

Auth note: Xero uses OAuth2 with short-lived access tokens. This connector
expects a pre-obtained Bearer token in XERO_CLIENT_SECRET for simplicity.
TODO: wire up the full OAuth2 PKCE / client-credentials flow:
https://developer.xero.com/documentation/guides/oauth2/pkce-flow/
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
    XeroContractor,
    XeroSupplierInvoice,
)

_BASE = "https://api.xero.com/api.xro/2.0"


def _parse_date(value: str | None) -> date | None:
    """Parse Xero's /Date(ms+offset)/ format or ISO date strings."""
    if not value:
        return None
    if value.startswith("/Date("):
        try:
            ms = int(value[6:].split("+")[0].split("-")[0].split(")")[0])
            return datetime.utcfromtimestamp(ms / 1000).date()
        except (ValueError, IndexError):
            return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_MOCK_CONTRACTORS: list[XeroContractor] = [
    XeroContractor(
        contact_id="xero-c-001",
        name="Alice Dev (Pty) Ltd",
        email="alice@alicedev.co.za",
        tax_number="4012345678",
        is_supplier=True,
        currency=Currency.ZAR,
    ),
    XeroContractor(
        contact_id="xero-c-002",
        name="Bob Design Studio",
        email="bob@bobdesign.co.za",
        tax_number="4087654321",
        is_supplier=True,
        currency=Currency.ZAR,
    ),
    XeroContractor(
        contact_id="xero-c-003",
        name="Charlie Ops CC",
        email="charlie@charlieops.co.za",
        tax_number="4055566677",
        is_supplier=True,
        currency=Currency.ZAR,
    ),
]

_MOCK_INVOICES: list[XeroSupplierInvoice] = [
    XeroSupplierInvoice(
        invoice_id="xero-inv-001",
        invoice_number="ADL-0042",
        contact_id="xero-c-001",
        contact_name="Alice Dev (Pty) Ltd",
        status="AUTHORISED",
        amount_due=Decimal("28000.00"),
        amount_paid=Decimal("28000.00"),
        currency=Currency.ZAR,
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 3, 31),
        reference="PROJ Sprint 41",
    ),
    XeroSupplierInvoice(
        invoice_id="xero-inv-002",
        invoice_number="ADL-0043",
        contact_id="xero-c-001",
        contact_name="Alice Dev (Pty) Ltd",
        status="AUTHORISED",
        amount_due=Decimal("32000.00"),
        amount_paid=Decimal("0.00"),
        currency=Currency.ZAR,
        issue_date=date(2026, 4, 1),
        due_date=date(2026, 4, 30),
        reference="PROJ Sprint 42",
    ),
    XeroSupplierInvoice(
        invoice_id="xero-inv-003",
        invoice_number="BDS-0019",
        contact_id="xero-c-002",
        contact_name="Bob Design Studio",
        status="PAID",
        amount_due=Decimal("18500.00"),
        amount_paid=Decimal("18500.00"),
        currency=Currency.ZAR,
        issue_date=date(2026, 3, 15),
        due_date=date(2026, 4, 14),
        reference="Dashboard design Q1",
    ),
    XeroSupplierInvoice(
        invoice_id="xero-inv-004",
        invoice_number="COC-0008",
        contact_id="xero-c-003",
        contact_name="Charlie Ops CC",
        status="AUTHORISED",
        amount_due=Decimal("12000.00"),
        amount_paid=Decimal("12000.00"),
        currency=Currency.ZAR,
        issue_date=date(2026, 3, 1),
        due_date=date(2026, 3, 31),
        reference="DevOps support March",
    ),
]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class XeroConnector:
    """Thin async wrapper around the Xero Accounting API."""

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        self._mock = settings.mock_mode if mock is None else mock
        # TODO: replace with proper OAuth2 token refresh flow
        self._headers = {
            "Authorization": f"Bearer {settings.xero_client_secret}",
            "Xero-Tenant-Id": settings.xero_tenant_id,
            "Accept": "application/json",
        }

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE}{path}", headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()

    def _parse_contact(self, raw: dict) -> XeroContractor:
        return XeroContractor(
            contact_id=raw.get("ContactID", ""),
            name=raw.get("Name", ""),
            email=raw.get("EmailAddress"),
            phone=(raw.get("Phones") or [{}])[0].get("PhoneNumber"),
            tax_number=raw.get("TaxNumber"),
            account_number=raw.get("AccountNumber"),
            is_supplier=raw.get("IsSupplier", True),
            currency=Currency.ZAR,
        )

    def _parse_invoice(self, raw: dict) -> XeroSupplierInvoice:
        return XeroSupplierInvoice(
            invoice_id=raw.get("InvoiceID", ""),
            invoice_number=raw.get("InvoiceNumber", ""),
            contact_id=(raw.get("Contact") or {}).get("ContactID", ""),
            contact_name=(raw.get("Contact") or {}).get("Name"),
            status=raw.get("Status", "DRAFT"),
            amount_due=_decimal(raw.get("AmountDue", 0)),
            amount_paid=_decimal(raw.get("AmountPaid", 0)),
            currency=Currency.ZAR,
            issue_date=_parse_date(raw.get("Date")),
            due_date=_parse_date(raw.get("DueDate")),
            reference=raw.get("Reference"),
            line_items=raw.get("LineItems", []),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_contractors(self) -> list[XeroContractor]:
        """Fetch all supplier contacts from Xero."""
        if self._mock:
            logger.debug("XeroConnector.fetch_contractors → mock")
            return _MOCK_CONTRACTORS

        data = await self._get("/Contacts", params={"where": "IsSupplier==true"})
        return [self._parse_contact(c) for c in data.get("Contacts", [])]

    async def fetch_supplier_invoices(
        self, date_range: DateRange | None = None
    ) -> list[XeroSupplierInvoice]:
        """Fetch ACCPAY (supplier/purchase) invoices, optionally filtered by date.

        TODO: Xero returns max 100 invoices per call; add offset pagination:
        https://developer.xero.com/documentation/api/accounting/invoices
        """
        if self._mock:
            logger.debug("XeroConnector.fetch_supplier_invoices → mock")
            if date_range:
                return [
                    inv for inv in _MOCK_INVOICES
                    if inv.issue_date and date_range.start <= inv.issue_date <= date_range.end
                ]
            return _MOCK_INVOICES

        params: dict[str, Any] = {"Type": "ACCPAY", "summaryOnly": "false"}
        if date_range:
            params["where"] = (
                f'Date>=DateTime({date_range.start.year},{date_range.start.month},{date_range.start.day})'
                f'&&Date<=DateTime({date_range.end.year},{date_range.end.month},{date_range.end.day})'
            )

        data = await self._get("/Invoices", params=params)
        return [self._parse_invoice(i) for i in data.get("Invoices", [])]
