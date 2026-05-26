"""MCP Gateway — unified interface over all SaaS connectors.

Acts as the adapter layer between agents and individual SaaS APIs.
Designed to be swappable with a real MCP server later without changing
any agent code — agents only ever call MCPGateway.collect().
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from src.config import get_settings
from src.gateway.connectors.harvest import HarvestConnector
from src.gateway.connectors.hubspot import HubSpotConnector
from src.gateway.connectors.jira import JiraConnector
from src.gateway.connectors.pandadoc import PandaDocConnector
from src.gateway.connectors.xero import XeroConnector
from src.models.schemas import (
    CollectionRequest,
    CollectionResult,
    DataSource,
)


class MCPGateway:
    """Fans out a CollectionRequest to the relevant connectors in parallel."""

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        use_mock = settings.mock_mode if mock is None else mock

        self._jira = JiraConnector(mock=use_mock)
        self._hubspot = HubSpotConnector(mock=use_mock)
        self._xero = XeroConnector(mock=use_mock)
        self._harvest = HarvestConnector(mock=use_mock)
        self._pandadoc = PandaDocConnector(mock=use_mock)

    async def collect(self, request: CollectionRequest) -> CollectionResult:
        """Fan out to all requested sources in parallel and assemble a CollectionResult.

        Failures in individual connectors are caught individually so a partial
        dataset is always returned — a broken Xero token never kills a Jira fetch.
        """
        result = CollectionResult(request=request)

        fetch_map: dict[DataSource, Any] = {}
        if DataSource.JIRA in request.sources:
            fetch_map[DataSource.JIRA] = self._fetch_jira(request)
        if DataSource.HUBSPOT in request.sources:
            fetch_map[DataSource.HUBSPOT] = self._fetch_hubspot(request)
        if DataSource.XERO in request.sources:
            fetch_map[DataSource.XERO] = self._fetch_xero(request)
        if DataSource.HARVEST in request.sources:
            fetch_map[DataSource.HARVEST] = self._fetch_harvest(request)
        if DataSource.PANDADOC in request.sources:
            fetch_map[DataSource.PANDADOC] = self._fetch_pandadoc(request)

        # return_exceptions=True means individual failures don't cancel sibling tasks
        outcomes = await asyncio.gather(*fetch_map.values(), return_exceptions=True)

        for source, outcome in zip(fetch_map.keys(), outcomes):
            if isinstance(outcome, BaseException):
                logger.error(f"Connector {source.value} failed: {outcome}")
                result.errors[source] = str(outcome)
            else:
                self._merge(result, source, outcome)

        logger.info(
            f"MCPGateway.collect complete | sources={[s.value for s in request.sources]} "
            f"errors={list(result.errors.keys())}"
        )
        return result

    # ------------------------------------------------------------------
    # Per-source fetch helpers (each returns a plain dict of lists)
    # ------------------------------------------------------------------

    async def _fetch_jira(self, req: CollectionRequest) -> dict:
        logger.debug("Fetching Jira …")
        # return_exceptions=True so a 403 on stories (missing read:issue:jira) does not
        # cancel boards/projects/sprints which require only read:board-scope:jira-software
        # or read:project:jira.
        results = await asyncio.gather(
            self._jira.fetch_stories(
                project_keys=req.project_keys or None,
                sprint_id=req.sprint_id,
                date_range=req.date_range,
            ),
            self._jira.fetch_features(project_keys=req.project_keys or None),
            self._jira.fetch_sprints(
                board_id=req.extra_filters.get("jira_board_id"),
                state=req.extra_filters.get("jira_sprint_state"),
            ),
            self._jira.fetch_boards(
                project_key=(req.project_keys[0] if req.project_keys else None),
            ),
            self._jira.fetch_projects(
                project_keys=req.project_keys or None,
            ),
            return_exceptions=True,
        )
        stories, features, sprints, boards, projects = results

        data: dict = {}
        labels = ("stories", "features", "sprints", "boards", "projects")
        for label, outcome in zip(labels, results):
            if isinstance(outcome, BaseException):
                logger.warning(f"[jira] {label} fetch failed: {outcome}")
            else:
                data[label] = outcome

        if not data:
            raise RuntimeError(
                "All Jira endpoints failed. "
                + " | ".join(f"{l}: {r}" for l, r in zip(labels, results))
            )
        return data

    async def _fetch_hubspot(self, req: CollectionRequest) -> dict:
        logger.debug("Fetching HubSpot …")
        deals, leads, invoices, pricing = await asyncio.gather(
            self._hubspot.fetch_deals(date_range=req.date_range),
            self._hubspot.fetch_leads(date_range=req.date_range),
            self._hubspot.fetch_invoices(),
            self._hubspot.fetch_pricing(),
        )
        return {"deals": deals, "leads": leads, "hubspot_invoices": invoices, "pricing": pricing}

    async def _fetch_xero(self, req: CollectionRequest) -> dict:
        logger.debug("Fetching Xero …")
        contractors, invoices = await asyncio.gather(
            self._xero.fetch_contractors(),
            self._xero.fetch_supplier_invoices(date_range=req.date_range),
        )
        return {"contractors": contractors, "supplier_invoices": invoices}

    async def _fetch_harvest(self, req: CollectionRequest) -> dict:
        logger.debug("Fetching Harvest …")
        timesheets = await self._harvest.fetch_timesheets(date_range=req.date_range)
        return {"timesheets": timesheets}

    async def _fetch_pandadoc(self, req: CollectionRequest) -> dict:
        logger.debug("Fetching PandaDoc …")
        contracts = await self._pandadoc.fetch_contracts()
        return {"contracts": contracts}

    # ------------------------------------------------------------------
    # Merge helper
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(result: CollectionResult, source: DataSource, data: dict) -> None:
        """Write fetched lists into the correct CollectionResult fields."""
        for field, value in data.items():
            if hasattr(result, field):
                getattr(result, field).extend(value)
            else:
                logger.warning(f"MCPGateway._merge: unknown field '{field}' from {source.value}")
