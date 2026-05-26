"""Gateway tests — every connector returns valid Pydantic objects in mock mode.

No real credentials or network calls are made. All connectors are tested with
mock=True, which returns the fixture data defined in each connector module.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.gateway.connectors.harvest import HarvestConnector
from src.gateway.connectors.hubspot import HubSpotConnector
from src.gateway.connectors.jira import JiraConnector
from src.gateway.connectors.pandadoc import PandaDocConnector
from src.gateway.connectors.xero import XeroConnector
from src.gateway.mcp_gateway import MCPGateway
from src.models.schemas import (
    CollectionRequest,
    DataSource,
    DateRange,
    DealStage,
    HarvestTimesheet,
    HubSpotDeal,
    HubSpotInvoice,
    HubSpotLead,
    HubSpotPricing,
    JiraFeature,
    JiraSprint,
    JiraStory,
    PandaDocContract,
    XeroContractor,
    XeroSupplierInvoice,
)


# ---------------------------------------------------------------------------
# Jira connector
# ---------------------------------------------------------------------------


class TestJiraConnector:
    @pytest.fixture
    def connector(self) -> JiraConnector:
        return JiraConnector(mock=True)

    async def test_fetch_stories_returns_valid_models(self, connector):
        stories = await connector.fetch_stories()
        assert len(stories) > 0
        for s in stories:
            assert isinstance(s, JiraStory)
            assert s.key
            assert s.summary
            assert s.status is not None

    async def test_fetch_stories_filter_by_project(self, connector):
        stories = await connector.fetch_stories(project_keys=["PROJ"])
        assert all(s.project_key == "PROJ" for s in stories)

    async def test_fetch_stories_filter_by_sprint(self, connector):
        stories = await connector.fetch_stories(sprint_id="sprint-42")
        assert all(s.sprint_id == "sprint-42" for s in stories)
        assert len(stories) > 0

    async def test_fetch_features_returns_valid_models(self, connector):
        features = await connector.fetch_features()
        assert len(features) > 0
        for f in features:
            assert isinstance(f, JiraFeature)
            assert f.key
            assert f.name

    async def test_fetch_sprints_returns_valid_models(self, connector):
        sprints = await connector.fetch_sprints()
        assert len(sprints) > 0
        for s in sprints:
            assert isinstance(s, JiraSprint)
            assert s.id
            assert s.name
            assert s.state in ("active", "closed", "future")

    async def test_fetch_sprints_filter_by_state(self, connector):
        active = await connector.fetch_sprints(state="active")
        assert all(s.state == "active" for s in active)

        closed = await connector.fetch_sprints(state="closed")
        assert all(s.state == "closed" for s in closed)

    async def test_story_points_are_numeric_or_none(self, connector):
        stories = await connector.fetch_stories()
        for s in stories:
            assert s.story_points is None or isinstance(s.story_points, float)


# ---------------------------------------------------------------------------
# HubSpot connector
# ---------------------------------------------------------------------------


class TestHubSpotConnector:
    @pytest.fixture
    def connector(self) -> HubSpotConnector:
        return HubSpotConnector(mock=True)

    async def test_fetch_deals_returns_valid_models(self, connector):
        deals = await connector.fetch_deals()
        assert len(deals) > 0
        for d in deals:
            assert isinstance(d, HubSpotDeal)
            assert d.id
            assert d.name
            assert d.stage in DealStage

    async def test_fetch_deals_filter_by_date_range(self, connector):
        date_range = DateRange(start=date(2026, 3, 1), end=date(2026, 3, 31))
        deals = await connector.fetch_deals(date_range=date_range)
        for d in deals:
            assert d.close_date is not None
            assert date_range.start <= d.close_date <= date_range.end

    async def test_fetch_leads_returns_valid_models(self, connector):
        leads = await connector.fetch_leads()
        assert len(leads) > 0
        for lead in leads:
            assert isinstance(lead, HubSpotLead)
            assert lead.id
            assert lead.first_name or lead.last_name

    async def test_fetch_invoices_returns_valid_models(self, connector):
        invoices = await connector.fetch_invoices()
        assert len(invoices) > 0
        for inv in invoices:
            assert isinstance(inv, HubSpotInvoice)
            assert inv.id
            assert inv.amount >= 0

    async def test_fetch_pricing_returns_valid_models(self, connector):
        pricing = await connector.fetch_pricing()
        assert len(pricing) > 0
        for p in pricing:
            assert isinstance(p, HubSpotPricing)
            assert p.product_id
            assert p.unit_price >= 0

    async def test_deal_amounts_are_decimal(self, connector):
        deals = await connector.fetch_deals()
        from decimal import Decimal
        for d in deals:
            if d.amount is not None:
                assert isinstance(d.amount, Decimal)


# ---------------------------------------------------------------------------
# Xero connector
# ---------------------------------------------------------------------------


class TestXeroConnector:
    @pytest.fixture
    def connector(self) -> XeroConnector:
        return XeroConnector(mock=True)

    async def test_fetch_contractors_returns_valid_models(self, connector):
        contractors = await connector.fetch_contractors()
        assert len(contractors) > 0
        for c in contractors:
            assert isinstance(c, XeroContractor)
            assert c.contact_id
            assert c.name
            assert c.is_supplier is True

    async def test_fetch_supplier_invoices_returns_valid_models(self, connector):
        invoices = await connector.fetch_supplier_invoices()
        assert len(invoices) > 0
        for inv in invoices:
            assert isinstance(inv, XeroSupplierInvoice)
            assert inv.invoice_id
            assert inv.invoice_number
            assert inv.contact_id
            assert inv.amount_due >= 0
            assert inv.amount_paid >= 0

    async def test_fetch_invoices_filter_by_date(self, connector):
        date_range = DateRange(start=date(2026, 3, 1), end=date(2026, 3, 31))
        invoices = await connector.fetch_supplier_invoices(date_range=date_range)
        for inv in invoices:
            assert inv.issue_date is not None
            assert date_range.start <= inv.issue_date <= date_range.end

    async def test_invoice_status_values_are_valid(self, connector):
        valid_statuses = {"DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"}
        invoices = await connector.fetch_supplier_invoices()
        for inv in invoices:
            assert inv.status in valid_statuses


# ---------------------------------------------------------------------------
# Harvest connector
# ---------------------------------------------------------------------------


class TestHarvestConnector:
    @pytest.fixture
    def connector(self) -> HarvestConnector:
        return HarvestConnector(mock=True)

    async def test_fetch_timesheets_returns_valid_models(self, connector):
        timesheets = await connector.fetch_timesheets()
        assert len(timesheets) > 0
        for ts in timesheets:
            assert isinstance(ts, HarvestTimesheet)
            assert ts.id
            assert ts.hours >= 0
            assert ts.user_name
            assert ts.project_name

    async def test_fetch_timesheets_filter_by_date(self, connector):
        date_range = DateRange(start=date(2026, 3, 30), end=date(2026, 4, 2))
        timesheets = await connector.fetch_timesheets(date_range=date_range)
        assert len(timesheets) > 0
        for ts in timesheets:
            assert date_range.start <= ts.date <= date_range.end

    async def test_jira_key_extraction(self, connector):
        timesheets = await connector.fetch_timesheets()
        # At least one timesheet should have a jira_ticket_key extracted from notes
        keys = [ts.jira_ticket_key for ts in timesheets if ts.jira_ticket_key]
        assert len(keys) > 0
        for key in keys:
            # Keys must match the PROJ-NNN pattern
            parts = key.split("-")
            assert len(parts) == 2
            assert parts[0].isupper()
            assert parts[1].isdigit()

    async def test_billable_field_is_bool(self, connector):
        timesheets = await connector.fetch_timesheets()
        for ts in timesheets:
            assert isinstance(ts.billable, bool)


# ---------------------------------------------------------------------------
# PandaDoc connector
# ---------------------------------------------------------------------------


class TestPandaDocConnector:
    @pytest.fixture
    def connector(self) -> PandaDocConnector:
        return PandaDocConnector(mock=True)

    async def test_fetch_contracts_returns_valid_models(self, connector):
        contracts = await connector.fetch_contracts()
        assert len(contracts) > 0
        for c in contracts:
            assert isinstance(c, PandaDocContract)
            assert c.document_id
            assert c.name
            assert c.status is not None

    async def test_fetch_contracts_filter_by_status(self, connector):
        from src.models.schemas import PandaDocStatus
        completed = await connector.fetch_contracts(status=PandaDocStatus.COMPLETED)
        assert len(completed) > 0
        assert all(c.status == PandaDocStatus.COMPLETED for c in completed)

    async def test_deal_id_is_string_or_none(self, connector):
        contracts = await connector.fetch_contracts()
        for c in contracts:
            assert c.deal_id is None or isinstance(c.deal_id, str)


# ---------------------------------------------------------------------------
# MCPGateway integration
# ---------------------------------------------------------------------------


class TestMCPGateway:
    @pytest.fixture
    def gateway(self) -> MCPGateway:
        return MCPGateway(mock=True)

    async def test_collect_single_source(self, gateway):
        req = CollectionRequest(sources=[DataSource.JIRA])
        result = await gateway.collect(req)
        assert len(result.stories) > 0
        assert result.errors == {}

    async def test_collect_multiple_sources(self, gateway):
        req = CollectionRequest(
            sources=[DataSource.JIRA, DataSource.HARVEST, DataSource.HUBSPOT]
        )
        result = await gateway.collect(req)
        assert len(result.stories) > 0
        assert len(result.timesheets) > 0
        assert len(result.deals) > 0

    async def test_collect_all_sources(self, gateway):
        req = CollectionRequest(sources=list(DataSource))
        result = await gateway.collect(req)
        assert len(result.stories) > 0
        assert len(result.timesheets) > 0
        assert len(result.deals) > 0
        assert len(result.contractors) > 0
        assert len(result.contracts) > 0
        assert result.errors == {}

    async def test_collect_with_date_range(self, gateway):
        req = CollectionRequest(
            sources=[DataSource.HARVEST],
            date_range=DateRange(start=date(2026, 3, 30), end=date(2026, 4, 2)),
        )
        result = await gateway.collect(req)
        for ts in result.timesheets:
            assert date(2026, 3, 30) <= ts.date <= date(2026, 4, 2)

    async def test_collect_with_project_filter(self, gateway):
        req = CollectionRequest(
            sources=[DataSource.JIRA],
            project_keys=["PROJ"],
        )
        result = await gateway.collect(req)
        assert all(s.project_key == "PROJ" for s in result.stories)

    async def test_errors_dict_populated_on_connector_failure(self, gateway):
        """A broken connector should populate errors without killing other results."""
        from unittest.mock import AsyncMock, patch

        async def boom(*_args, **_kwargs):
            raise RuntimeError("Simulated connector failure")

        with patch.object(gateway._jira, "fetch_stories", side_effect=boom), \
             patch.object(gateway._jira, "fetch_features", side_effect=boom), \
             patch.object(gateway._jira, "fetch_sprints", side_effect=boom):
            req = CollectionRequest(sources=[DataSource.JIRA, DataSource.HARVEST])
            result = await gateway.collect(req)

        assert DataSource.JIRA in result.errors
        # Harvest should still have returned data
        assert len(result.timesheets) > 0
