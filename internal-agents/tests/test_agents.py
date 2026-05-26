"""Agent unit tests — each agent runs with mock connector data and a
mocked Anthropic client so no real API calls are made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.data_analyst import DataAnalystAgent
from src.agents.data_collector import DataCollectorAgent
from src.agents.data_processor import DataProcessorAgent
from src.agents.reporter import ReporterAgent
from src.gateway.mcp_gateway import MCPGateway
from src.models.schemas import (
    AnalysisRequest,
    AnalysisResult,
    CollectionRequest,
    CollectionResult,
    ContractorCost,
    Currency,
    DataSource,
    DateRange,
    DealStage,
    DealWithContract,
    HarvestTimesheet,
    HubSpotDeal,
    JiraStory,
    JiraStoryStatus,
    Metric,
    PandaDocContract,
    PandaDocStatus,
    ProcessedDataset,
    ReportAudience,
    ReportRequest,
    StoryHours,
    XeroContractor,
    XeroSupplierInvoice,
)


# ---------------------------------------------------------------------------
# Mock Anthropic response helpers
# ---------------------------------------------------------------------------


@dataclass
class _MockUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _MockTextBlock:
    text: str
    type: str = "text"


@dataclass
class _MockToolUseBlock:
    name: str
    input: dict
    id: str = "tool-call-001"
    type: str = "tool_use"


@dataclass
class _MockMessage:
    stop_reason: str
    content: list
    usage: _MockUsage = field(default_factory=_MockUsage)


def _text_msg(text: str) -> _MockMessage:
    return _MockMessage(stop_reason="end_turn", content=[_MockTextBlock(text=text)])


def _tool_msg(name: str, inp: dict) -> _MockMessage:
    return _MockMessage(
        stop_reason="tool_use",
        content=[_MockToolUseBlock(name=name, input=inp)],
    )


def _seq(*messages: _MockMessage):
    """Return an AsyncMock whose side_effect cycles through the given messages."""
    mock = AsyncMock(side_effect=list(messages))
    return mock


# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_collection_result() -> CollectionResult:
    """A minimal but realistic CollectionResult using mock connector data."""
    from src.gateway.connectors.jira import _MOCK_STORIES, _MOCK_FEATURES, _MOCK_SPRINTS
    from src.gateway.connectors.hubspot import _MOCK_DEALS, _MOCK_INVOICES
    from src.gateway.connectors.harvest import _MOCK_TIMESHEETS
    from src.gateway.connectors.xero import _MOCK_CONTRACTORS, _MOCK_INVOICES as _XERO_INV
    from src.gateway.connectors.pandadoc import _MOCK_CONTRACTS

    req = CollectionRequest(sources=list(DataSource))
    return CollectionResult(
        request=req,
        stories=list(_MOCK_STORIES),
        features=list(_MOCK_FEATURES),
        sprints=list(_MOCK_SPRINTS),
        deals=list(_MOCK_DEALS),
        hubspot_invoices=list(_MOCK_INVOICES),
        timesheets=list(_MOCK_TIMESHEETS),
        contractors=list(_MOCK_CONTRACTORS),
        supplier_invoices=list(_XERO_INV),
        contracts=list(_MOCK_CONTRACTS),
    )


@pytest.fixture
def mock_processed_dataset(mock_collection_result) -> ProcessedDataset:
    """Pre-built ProcessedDataset that mirrors what DataProcessor would produce."""
    from src.gateway.connectors.jira import _MOCK_STORIES
    from src.gateway.connectors.harvest import _MOCK_TIMESHEETS
    from src.gateway.connectors.hubspot import _MOCK_DEALS
    from src.gateway.connectors.pandadoc import _MOCK_CONTRACTS
    from src.gateway.connectors.xero import _MOCK_CONTRACTORS, _MOCK_INVOICES as _XERO_INV

    story_map = {s.key: StoryHours(story=s) for s in _MOCK_STORIES}
    for ts in _MOCK_TIMESHEETS:
        if ts.jira_ticket_key and ts.jira_ticket_key in story_map:
            sh = story_map[ts.jira_ticket_key]
            sh.timesheets.append(ts)
            sh.total_hours += ts.hours
            if ts.billable:
                sh.total_billable_hours += ts.hours

    contract_by_deal_id = {c.deal_id: c for c in _MOCK_CONTRACTS if c.deal_id}
    dwcs = [
        DealWithContract(
            deal=d,
            contract=contract_by_deal_id.get(d.id),
            has_signed_contract=(
                contract_by_deal_id.get(d.id) is not None
                and contract_by_deal_id[d.id].status == PandaDocStatus.COMPLETED
            ),
        )
        for d in _MOCK_DEALS
    ]

    contractor_map = {c.contact_id: ContractorCost(contractor=c) for c in _MOCK_CONTRACTORS}
    for inv in _XERO_INV:
        if inv.contact_id in contractor_map:
            cc = contractor_map[inv.contact_id]
            cc.invoices.append(inv)
            cc.total_invoiced += inv.amount_due
            cc.total_paid += inv.amount_paid

    return ProcessedDataset(
        source_result=mock_collection_result,
        story_hours=list(story_map.values()),
        deals_with_contracts=dwcs,
        contractor_costs=list(contractor_map.values()),
    )


# ---------------------------------------------------------------------------
# DataCollectorAgent
# ---------------------------------------------------------------------------


class TestDataCollectorAgent:
    async def test_collect_with_structured_request_bypasses_claude(self):
        """A pre-built CollectionRequest skips Claude and goes straight to the gateway."""
        gateway = MCPGateway(mock=True)
        agent = DataCollectorAgent(gateway=gateway)
        req = CollectionRequest(sources=[DataSource.JIRA, DataSource.HARVEST])
        result = await agent.collect(req)
        assert len(result.stories) > 0
        assert len(result.timesheets) > 0

    async def test_collect_nl_calls_claude_for_interpretation(self):
        """Natural-language input causes a Claude call to create_collection_request."""
        gateway = MCPGateway(mock=True)
        agent = DataCollectorAgent(gateway=gateway)

        tool_response = _tool_msg(
            "create_collection_request",
            {
                "sources": ["jira", "harvest"],
                "date_range_start": "2026-03-31",
                "date_range_end": "2026-04-11",
            },
        )
        end_response = _text_msg("Collection request created.")

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(tool_response, end_response)
            result = await agent.collect("Get last sprint's stories and hours")

        assert result is not None
        assert len(result.stories) > 0

    async def test_collect_fallback_when_claude_skips_tool(self):
        """If Claude doesn't call the tool, all sources are fetched as fallback."""
        gateway = MCPGateway(mock=True)
        agent = DataCollectorAgent(gateway=gateway)

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(_text_msg("Sure, fetching all data."))
            result = await agent.collect("Get everything")

        # Fallback fetches all sources
        assert result is not None


# ---------------------------------------------------------------------------
# DataProcessorAgent
# ---------------------------------------------------------------------------


class TestDataProcessorAgent:
    # The mock data contains one unmatched timesheet (ts-006, no Jira key) and
    # one unmatched contract (pdoc-DDD, no deal_id), which would trigger the
    # fuzzy Claude pass in production.  These tests cover the deterministic
    # Python joins only, so we stub _fuzzy_match out for the whole class.
    @pytest.fixture(autouse=True)
    def stub_fuzzy_match(self):
        with patch.object(
            DataProcessorAgent,
            "_fuzzy_match",
            new=AsyncMock(return_value=({}, {})),
        ):
            yield

    async def test_process_exact_story_timesheet_join(self, mock_collection_result):
        """Timesheets with a jira_ticket_key are joined to the matching story."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)

        assert len(dataset.story_hours) == len(mock_collection_result.stories)

        keyed_ts = [
            ts for ts in mock_collection_result.timesheets
            if ts.jira_ticket_key
        ]
        total_matched = sum(len(sh.timesheets) for sh in dataset.story_hours)
        assert total_matched == len(keyed_ts)

    async def test_process_deal_contract_join(self, mock_collection_result):
        """Deals are paired with their PandaDoc contracts where IDs match."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)

        assert len(dataset.deals_with_contracts) == len(mock_collection_result.deals)
        signed = [dwc for dwc in dataset.deals_with_contracts if dwc.has_signed_contract]
        assert len(signed) > 0

    async def test_process_contractor_cost_aggregation(self, mock_collection_result):
        """Supplier invoices are aggregated per contractor."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)

        assert len(dataset.contractor_costs) == len(mock_collection_result.contractors)
        for cc in dataset.contractor_costs:
            assert cc.total_invoiced >= 0
            assert cc.total_paid >= 0
            assert cc.total_invoiced >= cc.total_paid

    async def test_processing_notes_recorded(self, mock_collection_result):
        """Processing notes are populated after a successful run."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)
        assert len(dataset.processing_notes) > 0

    async def test_unmatched_timesheets_collected(self, mock_collection_result):
        """Timesheets with no jira_ticket_key land in unmatched_timesheets."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)

        unkeyed = [
            ts for ts in mock_collection_result.timesheets
            if not ts.jira_ticket_key
        ]
        assert len(dataset.unmatched_timesheets) == len(unkeyed)

    async def test_hour_totals_are_correct(self, mock_collection_result):
        """total_hours on StoryHours equals the sum of its timesheet hours."""
        processor = DataProcessorAgent()
        dataset = await processor.process(mock_collection_result)

        for sh in dataset.story_hours:
            expected = sum(ts.hours for ts in sh.timesheets)
            assert abs(sh.total_hours - expected) < 0.001


# ---------------------------------------------------------------------------
# DataAnalystAgent
# ---------------------------------------------------------------------------


class TestDataAnalystAgent:
    async def test_metrics_computed_without_claude(self, mock_processed_dataset):
        """Phase 1 metrics are computed in pure Python — Claude only writes narrative."""
        req = AnalysisRequest(question="What is the sprint velocity?")

        narrative = "Sprint completion rate is 60%. CONFIDENCE: medium"
        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(_text_msg(narrative))
            analyst = DataAnalystAgent()
            result = await analyst.analyse(mock_processed_dataset, req)

        assert result.question == req.question
        assert len(result.metrics) > 0

        metric_names = [m.name for m in result.metrics]
        assert "total_story_points" in metric_names
        assert "completed_story_points" in metric_names
        assert "total_logged_hours" in metric_names

    async def test_anomalies_detected(self, mock_processed_dataset):
        """Rule-based anomaly detection runs before Claude is called."""
        req = AnalysisRequest(question="Any issues?")

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(_text_msg("Some issues found. CONFIDENCE: high"))
            analyst = DataAnalystAgent()
            result = await analyst.analyse(mock_processed_dataset, req)

        # Mock data has a Closed-Won deal without a signed contract (deal-002)
        high_severity = [a for a in result.anomalies if a.severity == "high"]
        assert len(high_severity) > 0

    async def test_confidence_extracted_correctly(self, mock_processed_dataset):
        req = AnalysisRequest(question="Test question")

        for level in ("high", "medium", "low"):
            with patch("anthropic.AsyncAnthropic") as MockClient:
                instance = MockClient.return_value
                instance.messages.create = _seq(
                    _text_msg(f"Analysis complete.\n\nCONFIDENCE: {level}")
                )
                analyst = DataAnalystAgent()
                result = await analyst.analyse(mock_processed_dataset, req)
            assert result.confidence == level

    async def test_confidence_stripped_from_insights(self, mock_processed_dataset):
        req = AnalysisRequest(question="Test question")

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(
                _text_msg("Great insights here.\n\nCONFIDENCE: medium")
            )
            analyst = DataAnalystAgent()
            result = await analyst.analyse(mock_processed_dataset, req)

        assert "CONFIDENCE:" not in result.insights


# ---------------------------------------------------------------------------
# ReporterAgent
# ---------------------------------------------------------------------------


class TestReporterAgent:
    @pytest.fixture
    def mock_analysis(self) -> AnalysisResult:
        return AnalysisResult(
            question="Summarize sprint delivery",
            metrics=[
                Metric(name="completed_story_points", value=13.0, unit="points"),
                Metric(name="total_logged_hours", value=32.0, unit="hours"),
            ],
            anomalies=[],
            insights="The team completed 13 of 34 story points this sprint.",
            confidence="high",
        )

    async def test_report_has_required_fields(self, mock_analysis, mock_processed_dataset):
        reporter = ReporterAgent()
        req = ReportRequest(title="Sprint 42 Report", audience=ReportAudience.OPERATIONS)

        tool_response = _tool_msg(
            "create_report",
            {
                "tldr": "The sprint delivered 13 points out of 34.",
                "sections": [
                    {"heading": "Delivery Summary", "body": "13 points completed."},
                    {"heading": "Hours Logged", "body": "32 hours logged in total."},
                ],
            },
        )
        end_response = _text_msg("Report captured.")

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(tool_response, end_response)
            result = await reporter.report(mock_analysis, req, mock_processed_dataset)

        assert result.title == "Sprint 42 Report"
        assert result.audience == ReportAudience.OPERATIONS
        assert result.tldr
        assert len(result.sections) == 2
        assert result.markdown
        assert "Sprint 42 Report" in result.markdown

    async def test_markdown_contains_tldr(self, mock_analysis, mock_processed_dataset):
        reporter = ReporterAgent()
        req = ReportRequest(title="Test Report", audience=ReportAudience.EXECUTIVE)
        tldr_text = "Executive summary goes here."

        tool_response = _tool_msg(
            "create_report",
            {"tldr": tldr_text, "sections": [{"heading": "Findings", "body": "Details."}]},
        )
        end_response = _text_msg("Done.")

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(tool_response, end_response)
            result = await reporter.report(mock_analysis, req, mock_processed_dataset)

        assert tldr_text in result.markdown

    async def test_fallback_report_on_missing_tool_call(self, mock_analysis, mock_processed_dataset):
        """If Claude doesn't call create_report, a fallback report is returned."""
        reporter = ReporterAgent()
        req = ReportRequest(title="Fallback Test", audience=ReportAudience.OPERATIONS)

        with patch("anthropic.AsyncAnthropic") as MockClient:
            instance = MockClient.return_value
            instance.messages.create = _seq(_text_msg("Here is my report in prose."))
            result = await reporter.report(mock_analysis, req, mock_processed_dataset)

        assert result.title == "Fallback Test"
        assert result.markdown
        assert result.tldr
