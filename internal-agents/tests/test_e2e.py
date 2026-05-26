"""End-to-end tests — three realistic user prompts run the full pipeline
(Data Collector → Data Processor → Data Analyst → Reporter) using mock
connector data and a mocked Anthropic client.

Claude API calls are mocked at the AsyncAnthropic level so no real
credentials are needed.  The mock responses are scripted to simulate
the orchestrator's tool-use planning loop and each downstream agent's
LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.orchestrator import OrchestratorAgent
from src.gateway.mcp_gateway import MCPGateway
from src.models.schemas import ReportResult


# ---------------------------------------------------------------------------
# Mock response helpers (duplicated from test_agents for self-containedness)
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 150
    output_tokens: int = 80


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _ToolUse:
    name: str
    input: dict
    id: str = "call-001"
    type: str = "tool_use"


@dataclass
class _Msg:
    stop_reason: str
    content: list
    usage: _Usage = field(default_factory=_Usage)


def _text(t: str) -> _Msg:
    return _Msg(stop_reason="end_turn", content=[_Text(text=t)])


def _tool(name: str, inp: dict) -> _Msg:
    return _Msg(stop_reason="tool_use", content=[_ToolUse(name=name, input=inp)])


def _make_mock_client(*messages: _Msg):
    """Return a patched AsyncAnthropic whose messages.create cycles through messages."""
    mock = AsyncMock(side_effect=list(messages))
    return mock


# ---------------------------------------------------------------------------
# Shared orchestrator fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def orchestrator() -> OrchestratorAgent:
    """Orchestrator wired to the mock gateway — no real HTTP calls."""
    return OrchestratorAgent(gateway=MCPGateway(mock=True))


# ---------------------------------------------------------------------------
# Scripted Claude response sequences
#
# Each e2e test scripts the full conversation:
#   orchestrator planning loop:   tool calls for each pipeline stage
#   data_collector interpreter:   create_collection_request tool call
#   data_processor fuzzy pass:    submit_fuzzy_matches (may not fire if all exact)
#   data_analyst narrative:       plain text response
#   reporter structured output:   create_report tool call
# ---------------------------------------------------------------------------


def _orchestrator_sequence_full(question: str, title: str, audience: str):
    """Return the Claude response sequence for a full 4-stage pipeline run."""
    return [
        # Orchestrator — plan: collect
        _tool("collect_data", {"fetch_description": question}),
        # DataCollector interpreter — parse NL into CollectionRequest
        _tool(
            "create_collection_request",
            {"sources": ["jira", "harvest", "hubspot", "pandadoc", "xero"]},
        ),
        _text("Collection request registered."),  # after tool result
        # Orchestrator — plan: process
        _tool("process_data", {}),
        # DataProcessor fuzzy pass (submit with empty matches — mock data fully exact)
        _tool(
            "submit_fuzzy_matches",
            {"timesheet_story_matches": [], "contract_deal_matches": []},
        ),
        _text("Fuzzy matches recorded."),
        # Orchestrator — plan: analyse
        _tool("analyse_data", {"question": question}),
        # DataAnalyst narrative
        _text(
            f"Based on the data, here are the findings for: {question}\n\n"
            "Key metrics look healthy. Some anomalies were detected.\n\n"
            "CONFIDENCE: medium"
        ),
        # Orchestrator — plan: generate report
        _tool("generate_report", {"title": title, "audience": audience}),
        # Reporter structured output
        _tool(
            "create_report",
            {
                "tldr": f"Summary for: {question}",
                "sections": [
                    {"heading": "Key Findings", "body": "Findings are here."},
                    {"heading": "Anomalies", "body": "Some anomalies were detected."},
                    {"heading": "Recommendations", "body": "Take action on the above."},
                ],
            },
        ),
        _text("Report captured."),
        # Orchestrator — end_turn after all tools done
        _text("Pipeline complete."),
    ]


# ---------------------------------------------------------------------------
# Test 1 — Sprint delivery vs hours logged
# ---------------------------------------------------------------------------


class TestSprintDeliveryVsHours:
    PROMPT = "Summarize last sprint's delivery vs hours logged"
    TITLE = "Sprint 42 Delivery Report"
    AUDIENCE = "operations"

    async def test_returns_report_result(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)

    async def test_report_has_tldr(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert len(result.tldr) > 0

    async def test_report_has_sections(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert len(result.sections) >= 2

    async def test_markdown_contains_heading(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert "## " in result.markdown  # at least one section heading

    async def test_progress_callback_receives_events(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        events: list[str] = []

        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            await orchestrator.run(self.PROMPT, progress_callback=events.append)

        assert len(events) > 0
        # First event should mention the prompt
        assert any("Received prompt" in e for e in events)
        # Should have a collecting event
        assert any("Collecting" in e or "Collected" in e for e in events)


# ---------------------------------------------------------------------------
# Test 2 — Deals without signed contracts
# ---------------------------------------------------------------------------


class TestDealsWithoutContracts:
    PROMPT = "Which deals closed last month don't have a signed contract yet?"
    TITLE = "Closed Deals Missing Contracts — April 2026"
    AUDIENCE = "finance"

    async def test_returns_report_result(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)

    async def test_analysis_contains_deal_anomaly(self, orchestrator):
        """The mock data has a Closed-Won deal (deal-002) without a contract."""
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert result.source_analysis is not None
        high_anomalies = [
            a for a in result.source_analysis.anomalies if a.severity == "high"
        ]
        assert len(high_anomalies) > 0

    async def test_report_audience_is_finance(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert result.audience.value == "finance"

    async def test_contract_coverage_metric_present(self, orchestrator):
        """signed_contract_rate metric must appear in the analysis."""
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        metric_names = [m.name for m in result.source_analysis.metrics]
        assert "signed_contract_rate" in metric_names


# ---------------------------------------------------------------------------
# Test 3 — Contractor cost per project for Q1
# ---------------------------------------------------------------------------


class TestContractorCostPerProject:
    PROMPT = "Show contractor cost per project for Q1"
    TITLE = "Q1 Contractor Cost Report"
    AUDIENCE = "finance"

    async def test_returns_report_result(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)

    async def test_contractor_cost_metrics_present(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        assert result.source_analysis is not None
        metric_names = [m.name for m in result.source_analysis.metrics]
        assert "total_contractor_invoiced" in metric_names
        assert "total_contractor_paid" in metric_names

    async def test_per_contractor_metrics_present(self, orchestrator):
        """A metric per contractor should appear in the analysis."""
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        metric_names = [m.name for m in result.source_analysis.metrics]
        contractor_metrics = [n for n in metric_names if n.startswith("contractor_cost_")]
        assert len(contractor_metrics) > 0

    async def test_generated_at_is_recent(self, orchestrator):
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            result = await orchestrator.run(self.PROMPT)

        assert isinstance(result, ReportResult)
        age_seconds = (datetime.now(UTC) - result.generated_at).total_seconds()
        assert age_seconds < 60  # report was generated within the last minute

    async def test_stream_yields_progress_then_report(self, orchestrator):
        """run_stream should emit progress frames before the final report frame."""
        sequence = _orchestrator_sequence_full(self.PROMPT, self.TITLE, self.AUDIENCE)
        progress_chunks: list[str] = []
        report_chunks: list[str] = []

        with patch("anthropic.AsyncAnthropic") as MockCls:
            MockCls.return_value.messages.create = _make_mock_client(*sequence)
            async for chunk in orchestrator.run_stream(self.PROMPT):
                if chunk.startswith("progress: "):
                    progress_chunks.append(chunk)
                elif chunk.startswith("report: "):
                    report_chunks.append(chunk)

        assert len(progress_chunks) > 0, "Expected progress events before the report"
        assert len(report_chunks) == 1, "Expected exactly one final report chunk"
        assert "## " in report_chunks[0]  # markdown content in the report chunk
