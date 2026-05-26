"""Orchestrator Agent — the single entry point for all user requests.

Responsibilities:
  1. Receive a natural-language user prompt
  2. Plan which pipeline stages are needed (using Claude tool-use)
  3. Drive Data Collector → Data Processor → Data Analyst → Reporter
     in dependency order, skipping stages that aren't needed
  4. Surface clarification questions when the request is ambiguous
  5. Emit progress events so callers can stream updates to the user

Per-request state is isolated in a _RunContext dataclass so concurrent
FastAPI requests never share pipeline data.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Coroutine

from loguru import logger

from src.agents.base import BaseAgent, ToolSpec
from src.agents.data_analyst import DataAnalystAgent
from src.agents.data_collector import DataCollectorAgent
from src.agents.data_processor import DataProcessorAgent
from src.agents.reporter import ReporterAgent
from src.cache.pipeline_cache import PipelineCache
from src.config import get_settings
from src.gateway.mcp_gateway import MCPGateway
from src.models.schemas import (
    AnalysisRequest,
    AnalysisResult,
    CollectionResult,
    ProcessedDataset,
    ReportAudience,
    ReportRequest,
    ReportResult,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Orchestrator of an internal business intelligence system.
Your job is to plan and execute the right pipeline for each user request.

## Available tools (call them in dependency order)

1. `collect_data`    — fetch raw data from SaaS sources (always first)
2. `process_data`    — clean and join the collected data (requires collect_data)
3. `analyse_data`    — compute metrics and insights (requires process_data)
4. `generate_report` — produce a formatted report (requires analyse_data)
5. `ask_clarification` — ask the user one focused question if truly ambiguous

## Dependency rules
- You MUST call collect_data before process_data.
- You MUST call process_data before analyse_data.
- You MUST call analyse_data before generate_report.

## Planning principles
- Always run at least: collect_data → process_data → analyse_data.
  The analyse_data step produces the text answer — skipping it means no answer is returned.
- Add generate_report when the user wants a formatted document or asks for a "report".
- Only call ask_clarification if you cannot make a reasonable assumption.
- Infer audience from context: "board", "CEO" → executive; "engineering" → technical;
  "finance team" → finance; default → operations.
- After each tool call you will receive a summary; use it to decide next steps.
"""

# ---------------------------------------------------------------------------
# Per-request context (isolates state from concurrent requests)
# ---------------------------------------------------------------------------


@dataclass
class _RunContext:
    collection_result: CollectionResult | None = None
    processed_dataset: ProcessedDataset | None = None
    analysis_result: AnalysisResult | None = None
    report_result: ReportResult | None = None
    clarification_question: str | None = None
    progress: list[str] = field(default_factory=list)

    def emit(self, message: str, callback: Callable[[str], None] | None = None) -> None:
        """Record a progress message and optionally push it to a callback."""
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        stamped = f"[{ts}] {message}"
        self.progress.append(stamped)
        logger.info(f"[orchestrator] {message}")
        if callback:
            callback(stamped)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_COLLECT_TOOL: dict[str, Any] = {
    "name": "collect_data",
    "description": (
        "Fetch raw data from one or more SaaS sources. "
        "Describe what you need in plain English; the Data Collector will interpret it."
    ),
    "input_schema": {
        "type": "object",
        "required": ["fetch_description"],
        "properties": {
            "fetch_description": {
                "type": "string",
                "description": (
                    "Natural language description of what to fetch, e.g. "
                    "'Get last sprint's Jira stories and matching Harvest hours'."
                ),
            },
        },
    },
}

_PROCESS_TOOL: dict[str, Any] = {
    "name": "process_data",
    "description": (
        "Clean, normalise, and join the previously collected data across sources. "
        "Must be called after collect_data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

_ANALYSE_TOOL: dict[str, Any] = {
    "name": "analyse_data",
    "description": (
        "Compute metrics, detect anomalies, and generate analytical insights. "
        "Must be called after process_data."
    ),
    "input_schema": {
        "type": "object",
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "description": "The specific analytical question to answer, in one sentence.",
            },
            "focus_sources": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["jira", "hubspot", "xero", "harvest", "pandadoc"],
                },
                "description": "Optional: narrow analysis to specific data sources.",
            },
        },
    },
}

_REPORT_TOOL: dict[str, Any] = {
    "name": "generate_report",
    "description": (
        "Produce a polished markdown report from the analysis. "
        "Must be called after analyse_data."
    ),
    "input_schema": {
        "type": "object",
        "required": ["title", "audience"],
        "properties": {
            "title": {
                "type": "string",
                "description": "Report title.",
            },
            "audience": {
                "type": "string",
                "enum": ["executive", "technical", "finance", "operations"],
                "description": "Target audience — determines tone and depth.",
            },
            "include_raw_tables": {
                "type": "boolean",
                "description": "Include raw data tables in the report. Default true.",
            },
            "extra_instructions": {
                "type": "string",
                "description": "Any additional formatting or content instructions.",
            },
        },
    },
}

_CLARIFY_TOOL: dict[str, Any] = {
    "name": "ask_clarification",
    "description": (
        "Ask the user one focused clarifying question when you genuinely cannot proceed. "
        "Use sparingly — prefer reasonable assumptions."
    ),
    "input_schema": {
        "type": "object",
        "required": ["question"],
        "properties": {
            "question": {
                "type": "string",
                "description": "The single clarifying question to ask the user.",
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class OrchestratorAgent:
    """Plans and drives the full data pipeline for a user prompt.

    Each call to `run()` or `run_stream()` creates an isolated _RunContext
    so concurrent requests never share pipeline state.
    """

    def __init__(self, gateway: MCPGateway | None = None) -> None:
        gw = gateway or MCPGateway()
        self._collector = DataCollectorAgent(gateway=gw)
        self._processor = DataProcessorAgent()
        self._analyst = DataAnalystAgent()
        self._reporter = ReporterAgent()
        self._cache = PipelineCache(ttl_seconds=get_settings().cache_ttl_seconds)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(
        self,
        user_prompt: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> ReportResult | str:
        """Execute the full pipeline for a user prompt.

        Returns a ReportResult when the pipeline completes, or a plain string
        when the orchestrator asks a clarification question.
        """
        ctx = _RunContext()
        agent = self._build_agent(ctx, progress_callback)

        ctx.emit(f"Received prompt: {user_prompt[:80]!r}", progress_callback)
        await agent.run(user_prompt)

        if ctx.clarification_question:
            return ctx.clarification_question

        if ctx.report_result:
            return ctx.report_result

        # Orchestrator ran the pipeline but stopped before generating a report
        if ctx.analysis_result:
            ctx.emit("Pipeline completed without a report — returning insights.", progress_callback)
            return ctx.analysis_result.insights

        # Safety net: orchestrator only called collect_data — format raw collection as text
        if ctx.collection_result:
            ctx.emit("Pipeline stopped after collection — summarising raw data.", progress_callback)
            cr = ctx.collection_result
            lines = ["**Collected data summary:**"]
            if cr.projects:
                lines.append(f"\n**Jira Projects ({len(cr.projects)}):**")
                for p in cr.projects:
                    lines.append(f"- {p.key}: {p.name} ({p.project_type or 'unknown type'})")
            if cr.boards:
                lines.append(f"\n**Jira Boards ({len(cr.boards)}):**")
                for b in cr.boards:
                    proj = f" — {b.project_name}" if b.project_name else ""
                    lines.append(f"- {b.name} (ID: {b.id}, type: {b.type}{proj})")
            if cr.sprints:
                lines.append(f"\n**Sprints ({len(cr.sprints)}):**")
                for s in cr.sprints:
                    lines.append(f"- {s.name} [{s.state}] on board {s.board_id or '?'}")
            if cr.stories:
                lines.append(f"\n**Stories:** {len(cr.stories)} fetched")
            if cr.deals:
                lines.append(f"\n**HubSpot Deals:** {len(cr.deals)} fetched")
            if cr.errors:
                lines.append(f"\n**Errors:** {', '.join(f'{k.value}: {v[:80]}' for k, v in cr.errors.items())}")
            return "\n".join(lines)

        return "Pipeline did not produce a result. Check logs for details."

    async def run_stream(
        self,
        user_prompt: str,
    ) -> AsyncGenerator[str, None]:
        """Stream progress messages as the pipeline executes, then yield the final markdown.

        Usage:
            async for chunk in orchestrator.run_stream(prompt):
                print(chunk)
        """
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _progress(msg: str) -> None:
            queue.put_nowait(f"progress: {msg}")

        async def _run() -> None:
            try:
                result = await self.run(user_prompt, progress_callback=_progress)
                if isinstance(result, ReportResult):
                    queue.put_nowait(f"report: {result.markdown}")
                else:
                    queue.put_nowait(f"result: {result}")
            except Exception as exc:
                queue.put_nowait(f"error: {exc}")
            finally:
                queue.put_nowait(None)  # always unblock the generator

        task = asyncio.create_task(_run())

        while True:
            item = await queue.get()
            if item is None:
                break
            yield item

        await task  # re-raise if task stored an exception (it won't now, but keeps semantics)

    # ------------------------------------------------------------------
    # Agent construction — tools are closures over the per-request context
    # ------------------------------------------------------------------

    def _build_agent(
        self,
        ctx: _RunContext,
        progress_callback: Callable[[str], None] | None,
    ) -> BaseAgent:
        """Build a BaseAgent with all pipeline tools wired to the given context."""

        def _emit(msg: str) -> None:
            ctx.emit(msg, progress_callback)

        def _sub_emit(agent_label: str) -> Callable[[str], None]:
            """Return a progress callback that prefixes messages with the agent label."""
            def _cb(msg: str) -> None:
                _emit(f"{agent_label}: {msg}")
            return _cb

        # ---- collect_data ----
        async def handle_collect(inp: dict[str, Any]) -> str:
            description = inp["fetch_description"]
            _emit(f"Collecting data: {description!r}")
            ctx.collection_result = await self._collector.collect(
                description,
                progress=_sub_emit("Data Collector"),
                cache=self._cache,
            )
            cr = ctx.collection_result
            summary = (
                f"Collected: {len(cr.projects)} projects, {len(cr.boards)} boards, "
                f"{len(cr.stories)} stories, {len(cr.features)} features, "
                f"{len(cr.sprints)} sprints, {len(cr.deals)} deals, "
                f"{len(cr.timesheets)} timesheets, {len(cr.contracts)} contracts, "
                f"{len(cr.contractors)} contractors."
            )
            if cr.errors:
                error_details = " | ".join(
                    f"{k.value}: {v[:120]}" for k, v in cr.errors.items()
                )
                summary += f" Errors: {error_details}"
            _emit(summary)
            return summary

        # ---- process_data ----
        async def handle_process(_inp: dict[str, Any]) -> str:
            if ctx.collection_result is None:
                return "ERROR: collect_data must be called before process_data."

            col_key = self._cache.collection_key(ctx.collection_result.request)
            cached_ds = self._cache.get_dataset(col_key)
            if cached_ds is not None:
                ctx.processed_dataset = cached_ds
                _emit("Cache HIT — reusing previously processed dataset.")
            else:
                _emit("Processing and joining collected data …")
                ctx.processed_dataset = await self._processor.process(
                    ctx.collection_result, progress=_sub_emit("Data Processor")
                )
                self._cache.set_dataset(col_key, ctx.processed_dataset)

            ds = ctx.processed_dataset
            summary = (
                f"Processed: {len(ds.story_hours)} story-hour records, "
                f"{len(ds.deals_with_contracts)} deal-contract pairs, "
                f"{len(ds.contractor_costs)} contractor cost summaries. "
                f"Unmatched timesheets: {len(ds.unmatched_timesheets)}, "
                f"unmatched contracts: {len(ds.unmatched_contracts)}."
            )
            _emit(summary)
            return summary

        # ---- analyse_data ----
        async def handle_analyse(inp: dict[str, Any]) -> str:
            if ctx.processed_dataset is None:
                return "ERROR: process_data must be called before analyse_data."
            question = inp["question"]
            focus = inp.get("focus_sources", [])
            _emit(f"Analysing: {question!r}")
            from src.models.schemas import DataSource
            focus_sources = []
            for s in focus:
                try:
                    focus_sources.append(DataSource(s))
                except ValueError:
                    pass

            col_key = self._cache.collection_key(ctx.processed_dataset.source_result.request)
            cached_ar = self._cache.get_analysis(col_key, question, focus)
            if cached_ar is not None:
                ctx.analysis_result = cached_ar
                _emit("Cache HIT — reusing previously computed analysis.")
            else:
                req = AnalysisRequest(question=question, focus_sources=focus_sources)
                ctx.analysis_result = await self._analyst.analyse(
                    ctx.processed_dataset, req, progress=_sub_emit("Data Analyst")
                )
                self._cache.set_analysis(col_key, question, focus, ctx.analysis_result)

            ar = ctx.analysis_result
            summary = (
                f"Analysis complete: {len(ar.metrics)} metrics computed, "
                f"{len(ar.anomalies)} anomalies found. "
                f"Confidence: {ar.confidence}."
            )
            _emit(summary)
            return summary

        # ---- generate_report ----
        async def handle_report(inp: dict[str, Any]) -> str:
            if ctx.analysis_result is None:
                return "ERROR: analyse_data must be called before generate_report."
            title = inp["title"]
            audience_raw = inp.get("audience", "operations")
            include_tables = inp.get("include_raw_tables", True)
            extra = inp.get("extra_instructions")

            try:
                audience = ReportAudience(audience_raw)
            except ValueError:
                audience = ReportAudience.OPERATIONS

            _emit(f"Generating {audience.value} report: {title!r}")
            req = ReportRequest(
                title=title,
                audience=audience,
                include_raw_tables=include_tables,
                extra_instructions=extra,
            )
            ctx.report_result = await self._reporter.report(
                analysis=ctx.analysis_result,
                request=req,
                dataset=ctx.processed_dataset,
                progress=_sub_emit("Reporter"),
            )
            summary = (
                f"Report generated: {len(ctx.report_result.sections)} sections, "
                f"{len(ctx.report_result.markdown)} characters."
            )
            _emit(summary)
            return summary

        # ---- ask_clarification ----
        async def handle_clarify(inp: dict[str, Any]) -> str:
            ctx.clarification_question = inp["question"]
            _emit(f"Clarification needed: {inp['question']!r}")
            return "Clarification question recorded."

        specs = [
            ToolSpec(_COLLECT_TOOL, handle_collect),
            ToolSpec(_PROCESS_TOOL, handle_process),
            ToolSpec(_ANALYSE_TOOL, handle_analyse),
            ToolSpec(_REPORT_TOOL, handle_report),
            ToolSpec(_CLARIFY_TOOL, handle_clarify),
        ]

        return (
            BaseAgent(
                name="orchestrator",
                system_prompt=_SYSTEM_PROMPT,
                model=get_settings().orchestrator_model,
                max_tokens=4096,
            )
            .with_tools(specs)
            .with_forced_tool_use()
            .with_progress(lambda msg: _emit(f"Orchestrator: {msg}"))
        )
