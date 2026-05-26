"""Reporter Agent — produces a polished, audience-aware markdown report from an
AnalysisResult.

Responsibilities:
  - Adapt tone, depth, and structure to the requested audience
    (executive / technical / finance / operations)
  - Force structured output via a tool call so sections are always typed
  - Assemble sections + TL;DR into a single coherent markdown document
  - Optionally include raw data tables and chart placeholders
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

from loguru import logger

from src.agents.base import BaseAgent, ToolSpec
from src.models.schemas import (
    AnalysisResult,
    Anomaly,
    ContractorCost,
    DealWithContract,
    Metric,
    ProcessedDataset,
    ReportAudience,
    ReportRequest,
    ReportResult,
    ReportSection,
    StoryHours,
)

# ---------------------------------------------------------------------------
# Audience-specific system prompt fragments
# ---------------------------------------------------------------------------

_AUDIENCE_GUIDANCE: dict[ReportAudience, str] = {
    ReportAudience.EXECUTIVE: """\
You are writing for C-suite executives. Be concise and outcome-focused.
Lead every section with the business implication, not the data.
Avoid technical jargon. Use bold for key numbers. Keep sections short (3–5 sentences or bullets).
Highlight risks prominently — executives need to act, not read.""",

    ReportAudience.TECHNICAL: """\
You are writing for engineers and technical leads. Be precise and data-rich.
Include story-level breakdowns, hours-per-ticket detail, and process observations.
Use markdown tables for structured data. Reference specific ticket keys and invoice numbers.
Surface inefficiencies and bottlenecks with specifics.""",

    ReportAudience.FINANCE: """\
You are writing for the finance team. Focus on costs, revenues, and cash flow.
Always include currency and totals. Flag unpaid invoices, overdue amounts,
contract values vs invoiced amounts, and deal closure without a signed contract.
Use tables for all monetary figures. Precision matters — no rounding without noting it.""",

    ReportAudience.OPERATIONS: """\
You are writing for operations managers. Balance delivery metrics with financial health.
Include sprint completion rates, contractor utilisation, and deal pipeline status.
Flag operational risks (blocked stories, missing contracts, overdue invoices).
Use a mix of prose and tables. Aim for a report that could be shared at a weekly ops review.""",
}

_BASE_SYSTEM_PROMPT = """\
You are the Reporter for an internal business intelligence system.
You receive pre-computed analysis results and produce a polished markdown report.

{audience_guidance}

Structure rules:
  - Always call the `create_report` tool with your output — never respond in prose.
  - The TL;DR must be one paragraph (3–5 sentences) answering the core question.
  - Each section body must be valid GitHub-flavoured markdown.
  - Tables must use GFM pipe syntax with a header row.
  - Never invent data not present in the inputs.
  - Anomalies with severity=high must appear in the report.
"""

# ---------------------------------------------------------------------------
# Tool definition — forces Claude to emit structured sections
# ---------------------------------------------------------------------------

_CREATE_REPORT_TOOL: dict[str, Any] = {
    "name": "create_report",
    "description": (
        "Emit the complete structured report. Call this tool exactly once with "
        "the TL;DR and all sections."
    ),
    "input_schema": {
        "type": "object",
        "required": ["tldr", "sections"],
        "properties": {
            "tldr": {
                "type": "string",
                "description": (
                    "One-paragraph executive summary (3–5 sentences). "
                    "Must directly answer the user's question."
                ),
            },
            "sections": {
                "type": "array",
                "description": "Ordered list of report sections.",
                "items": {
                    "type": "object",
                    "required": ["heading", "body"],
                    "properties": {
                        "heading": {
                            "type": "string",
                            "description": "Section heading (no leading #).",
                        },
                        "body": {
                            "type": "string",
                            "description": "GitHub-flavoured markdown body for this section.",
                        },
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class ReporterAgent:
    """Generates a polished, audience-aware markdown report from an AnalysisResult.

    Uses a forced tool call to guarantee the output is always structured into
    typed ReportSection objects, regardless of what Claude produces.
    """

    def __init__(self) -> None:
        # Agent is instantiated per-report so the system prompt can be audience-specific
        pass

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def report(
        self,
        analysis: AnalysisResult,
        request: ReportRequest,
        dataset: ProcessedDataset | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> ReportResult:
        """Generate a ReportResult from an AnalysisResult and a ReportRequest.

        `dataset` is optional — when provided, raw data tables are built from it
        if `request.include_raw_tables` is True.
        """
        logger.info(
            f"[reporter] generating report | "
            f"audience={request.audience.value} title={request.title!r}"
        )

        system_prompt = _BASE_SYSTEM_PROMPT.format(
            audience_guidance=_AUDIENCE_GUIDANCE[request.audience]
        )
        agent = BaseAgent(name="reporter", system_prompt=system_prompt)
        if progress:
            agent.with_progress(progress)

        captured: list[dict[str, Any]] = []

        async def handle_create_report(inp: dict[str, Any]) -> str:
            captured.append(inp)
            return "Report captured."

        spec = ToolSpec(definition=_CREATE_REPORT_TOOL, handler=handle_create_report)
        agent.with_tools([spec])

        prompt = self._build_prompt(analysis, request, dataset)
        await agent.run(prompt)

        if not captured:
            logger.error("[reporter] Claude did not call create_report — using fallback")
            return self._fallback_report(analysis, request)

        raw = captured[0]
        tldr: str = raw.get("tldr", "")
        raw_sections: list[dict] = raw.get("sections", [])

        sections = [
            ReportSection(
                heading=s.get("heading", ""),
                body=s.get("body", ""),
            )
            for s in raw_sections
        ]

        markdown = self._assemble_markdown(request.title, tldr, sections, request)

        logger.info(
            f"[reporter] done | sections={len(sections)} "
            f"markdown_chars={len(markdown)}"
        )

        return ReportResult(
            title=request.title,
            audience=request.audience,
            tldr=tldr,
            sections=sections,
            markdown=markdown,
            source_analysis=analysis,
        )

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        analysis: AnalysisResult,
        request: ReportRequest,
        dataset: ProcessedDataset | None,
    ) -> str:
        parts: list[str] = []

        parts.append(f"# Report Request\n")
        parts.append(f"**Title:** {request.title}")
        parts.append(f"**Audience:** {request.audience.value}")
        parts.append(f"**Original question:** {analysis.question}")
        if request.extra_instructions:
            parts.append(f"**Extra instructions:** {request.extra_instructions}")

        parts.append("\n## Analysis Insights\n")
        parts.append(analysis.insights)
        parts.append(f"\n*Analyst confidence: {analysis.confidence}*")

        parts.append("\n## Computed Metrics\n")
        parts.append(self._metrics_table(analysis.metrics))

        if analysis.anomalies:
            parts.append("\n## Anomalies Detected\n")
            parts.append(self._anomalies_table(analysis.anomalies))

        if dataset and request.include_raw_tables:
            raw_tables = self._build_raw_tables(dataset)
            if raw_tables:
                parts.append("\n## Raw Data (for reference)\n")
                parts.append(raw_tables)

        if request.include_charts_placeholder:
            parts.append(
                "\n> **Chart placeholders:** Include `<!-- CHART: <metric_name> -->` "
                "comments where a chart would enhance understanding."
            )

        parts.append(
            "\n---\nNow call `create_report` with the TL;DR and all sections."
        )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _metrics_table(metrics: list[Metric]) -> str:
        if not metrics:
            return "_No metrics computed._"
        rows = ["| Metric | Value | Unit | Context |",
                "|---|---|---|---|"]
        for m in metrics:
            rows.append(
                f"| {m.name} | {m.value} | {m.unit or '—'} | {m.context or '—'} |"
            )
        return "\n".join(rows)

    @staticmethod
    def _anomalies_table(anomalies: list[Anomaly]) -> str:
        if not anomalies:
            return "_No anomalies detected._"
        rows = ["| Severity | Description | Affected |",
                "|---|---|---|"]
        for a in anomalies:
            entities = ", ".join(a.affected_entities) if a.affected_entities else "—"
            severity_badge = {"high": "🔴 high", "medium": "🟡 medium", "low": "🟢 low"}.get(
                a.severity, a.severity
            )
            rows.append(f"| {severity_badge} | {a.description} | {entities} |")
        return "\n".join(rows)

    @staticmethod
    def _build_raw_tables(dataset: ProcessedDataset) -> str:
        sections: list[str] = []

        # Story hours table
        if dataset.story_hours:
            rows = ["| Story | Summary | Status | Points | Hours | Billable hrs |",
                    "|---|---|---|---|---|---|"]
            for sh in dataset.story_hours:
                s = sh.story
                rows.append(
                    f"| {s.key} | {s.summary[:50]} | {s.status.value} | "
                    f"{s.story_points or '—'} | {sh.total_hours:.1f} | "
                    f"{sh.total_billable_hours:.1f} |"
                )
            sections.append("### Stories & Hours\n" + "\n".join(rows))

        # Deals & contracts table
        if dataset.deals_with_contracts:
            rows = ["| Deal | Stage | Amount | Contract | Signed |",
                    "|---|---|---|---|---|"]
            for dwc in dataset.deals_with_contracts:
                d = dwc.deal
                contract_name = dwc.contract.name[:40] if dwc.contract else "—"
                signed = "✅" if dwc.has_signed_contract else "❌"
                rows.append(
                    f"| {d.name[:40]} | {d.stage.value} | "
                    f"{d.currency.value} {d.amount or '—'} | "
                    f"{contract_name} | {signed} |"
                )
            sections.append("### Deals & Contracts\n" + "\n".join(rows))

        # Contractor costs table
        if dataset.contractor_costs:
            rows = ["| Contractor | Invoices | Total Invoiced | Paid | Outstanding |",
                    "|---|---|---|---|---|"]
            for cc in dataset.contractor_costs:
                outstanding = cc.total_invoiced - cc.total_paid
                rows.append(
                    f"| {cc.contractor.name} | {len(cc.invoices)} | "
                    f"{cc.contractor.currency.value} {cc.total_invoiced} | "
                    f"{cc.total_paid} | {outstanding} |"
                )
            sections.append("### Contractor Costs\n" + "\n".join(rows))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Markdown assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _assemble_markdown(
        title: str,
        tldr: str,
        sections: list[ReportSection],
        request: ReportRequest,
    ) -> str:
        lines: list[str] = []
        lines.append(f"# {title}")
        lines.append(
            f"*Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"Audience: {request.audience.value}*"
        )
        lines.append("")
        lines.append("## TL;DR")
        lines.append("")
        lines.append(f"> {tldr}")
        lines.append("")
        lines.append("---")

        for section in sections:
            lines.append("")
            lines.append(f"## {section.heading}")
            lines.append("")
            lines.append(section.body)
            lines.append("")
            lines.append("---")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Fallback (Claude didn't call the tool)
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_report(
        analysis: AnalysisResult,
        request: ReportRequest,
    ) -> ReportResult:
        tldr = analysis.insights[:500] if analysis.insights else "No insights available."
        section = ReportSection(
            heading="Analysis",
            body=analysis.insights,
        )
        markdown = (
            f"# {request.title}\n\n"
            f"> {tldr}\n\n"
            f"## Analysis\n\n{analysis.insights}"
        )
        return ReportResult(
            title=request.title,
            audience=request.audience,
            tldr=tldr,
            sections=[section],
            markdown=markdown,
            source_analysis=analysis,
        )
