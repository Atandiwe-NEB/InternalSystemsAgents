"""Data Collector Agent — translates natural language or structured requests into
a CollectionResult by calling the MCP Gateway.

Responsibilities:
  - Accept either a plain-English fetch description OR a ready-made CollectionRequest
  - When given natural language, use Claude to infer which sources and date ranges
    are needed (via a forced tool call)
  - Delegate the actual HTTP fetching entirely to MCPGateway
  - Return a raw CollectionResult — no analysis, no transformation
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Callable

from loguru import logger

from src.agents.base import BaseAgent, ToolSpec
from src.cache.pipeline_cache import PipelineCache
from src.gateway.mcp_gateway import MCPGateway
from src.models.schemas import (
    CollectionRequest,
    CollectionResult,
    DataSource,
    DateRange,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Data Collector for an internal business intelligence system.
Your only job is to decide WHAT data to fetch and from WHERE — you never
analyse, summarise, or transform data.

Available data sources:
  - jira      → stories, features (epics), sprints
  - hubspot   → deals, leads, invoices, pricing
  - xero      → contractor contacts, supplier invoices
  - harvest   → timesheets / time entries
  - pandadoc  → contracts / documents

When asked to collect data, you MUST call the `create_collection_request` tool
with your decision. Do not answer in prose — always use the tool.

Date-range guidelines:
  - "last sprint"      → the 2-week period ending on the most recent Friday
  - "this sprint"      → the 2-week period starting on the most recent Monday
  - "last month"       → first to last day of the previous calendar month
  - "Q1"               → Jan 1 – Mar 31 of the current year
  - "Q2"               → Apr 1 – Jun 30 of the current year
  - "last week"        → Mon–Sun of the previous ISO week
  - When a range is ambiguous or not mentioned, omit date_range_start/end

Source selection guidelines:
  - "stories", "tickets", "sprint", "features"     → jira
  - "deals", "leads", "pipeline", "revenue"        → hubspot
  - "contractors", "suppliers", "invoices (cost)"  → xero
  - "hours", "time", "timesheets", "logged"        → harvest
  - "contracts", "signed", "documents"             → pandadoc
  - Cross-source requests include all relevant sources
"""

# ---------------------------------------------------------------------------
# Tool definition — forces Claude to emit a structured CollectionRequest
# ---------------------------------------------------------------------------

_COLLECTION_REQUEST_TOOL: dict[str, Any] = {
    "name": "create_collection_request",
    "description": (
        "Specify exactly which data sources to query and any filters to apply. "
        "Call this tool once with all parameters decided."
    ),
    "input_schema": {
        "type": "object",
        "required": ["sources"],
        "properties": {
            "sources": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["jira", "hubspot", "xero", "harvest", "pandadoc"],
                },
                "description": "One or more data sources to activate.",
            },
            "date_range_start": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD — start of the date filter (inclusive).",
            },
            "date_range_end": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD — end of the date filter (inclusive).",
            },
            "project_keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Jira project keys to scope story/sprint queries, e.g. ['PROJ'].",
            },
            "sprint_id": {
                "type": "string",
                "description": "Exact Jira sprint ID if the request names a specific sprint.",
            },
            "extra_filters": {
                "type": "object",
                "description": (
                    "Freeform connector-specific filters, e.g. "
                    '{"jira_sprint_state": "closed", "jira_board_id": "1"}'
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DataCollectorAgent:
    """Fetches raw data from SaaS sources via the MCP Gateway.

    Accepts either a natural-language string or a ready-made CollectionRequest.
    Returns a CollectionResult without performing any analysis.
    """

    def __init__(self, gateway: MCPGateway | None = None) -> None:
        self._gateway = gateway or MCPGateway()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def collect(
        self,
        request: str | CollectionRequest,
        progress: Callable[[str], None] | None = None,
        cache: PipelineCache | None = None,
    ) -> CollectionResult:
        """Fetch data based on a natural-language description or a CollectionRequest.

        If `request` is a string, Claude interprets it into a CollectionRequest
        first, then the gateway is called. If it is already a CollectionRequest,
        the gateway is called directly.

        When `cache` is provided the gateway call is skipped on a cache hit.
        The interpreted CollectionRequest is always resolved before the cache
        lookup so the key is canonical regardless of how the prompt is worded.
        """
        if isinstance(request, CollectionRequest):
            collection_request = request
            logger.info(
                f"[data-collector] structured request received "
                f"sources={[s.value for s in collection_request.sources]}"
            )
        else:
            logger.info(f"[data-collector] interpreting natural-language request: {request!r}")
            collection_request = await self._interpret(request, progress=progress)
            logger.info(
                f"[data-collector] interpreted → sources={[s.value for s in collection_request.sources]} "
                f"date_range={collection_request.date_range}"
            )

        if cache:
            cached = cache.get_collection(collection_request)
            if cached is not None:
                if progress:
                    progress("Cache HIT — reusing previously fetched data (skipping API calls).")
                return cached

        result = await self._gateway.collect(collection_request)

        if cache:
            cache.set_collection(result)

        return result

    # ------------------------------------------------------------------
    # Natural-language interpretation
    # ------------------------------------------------------------------

    async def _interpret(
        self,
        natural_language: str,
        progress: Callable[[str], None] | None = None,
    ) -> CollectionRequest:
        """Ask Claude to map a natural-language request to a CollectionRequest.

        Uses a captured tool-call pattern: the tool handler stores the result in
        a closure variable instead of doing real work, so the BaseAgent loop
        terminates after one tool call.
        """
        captured: list[CollectionRequest] = []  # list so the closure can mutate it

        async def handle_create_collection_request(inp: dict[str, Any]) -> str:
            date_range: DateRange | None = None
            start_raw = inp.get("date_range_start")
            end_raw = inp.get("date_range_end")
            if start_raw and end_raw:
                try:
                    date_range = DateRange(
                        start=date.fromisoformat(start_raw),
                        end=date.fromisoformat(end_raw),
                    )
                except ValueError as exc:
                    logger.warning(f"[data-collector] bad date from Claude: {exc}")

            raw_sources: list[str] = inp.get("sources", [])
            sources: list[DataSource] = []
            for s in raw_sources:
                try:
                    sources.append(DataSource(s))
                except ValueError:
                    logger.warning(f"[data-collector] unknown source '{s}' — skipping")

            if not sources:
                sources = [DataSource.JIRA]  # safe fallback

            cr = CollectionRequest(
                sources=sources,
                date_range=date_range,
                project_keys=inp.get("project_keys", []),
                sprint_id=inp.get("sprint_id"),
                extra_filters=inp.get("extra_filters", {}),
            )
            captured.append(cr)
            return "Collection request registered."

        spec = ToolSpec(
            definition=_COLLECTION_REQUEST_TOOL,
            handler=handle_create_collection_request,
        )

        agent = (
            BaseAgent(
                name="data-collector-interpreter",
                system_prompt=_SYSTEM_PROMPT,
            )
            .with_tools([spec])
            .with_forced_tool_use()
        )
        if progress:
            agent.with_progress(progress)

        today = datetime.now(UTC).date().isoformat()
        prompt = f"Today is {today}.\n\nFetch request: {natural_language}"
        await agent.run(prompt)

        if not captured:
            logger.error("[data-collector] Claude did not call create_collection_request — defaulting to jira only")
            return CollectionRequest(sources=[DataSource.JIRA])

        return captured[0]
