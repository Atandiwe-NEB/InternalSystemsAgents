"""Harvest connector — fetches time entries (timesheets).

Real API docs:
  Time entries : https://help.getharvest.com/api-v2/timesheets-api/timesheets/time-entries/

Auth: Personal Access Token via Bearer header + Harvest-Account-Id header.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.models.schemas import DateRange, HarvestTimesheet

_BASE = "https://api.harvestapp.com/v2"

# Matches patterns like "[PROJ-123]" or "PROJ-123:" in timesheet notes
_JIRA_KEY_RE = re.compile(r"\b([A-Z]+-\d+)\b")


def _extract_jira_key(notes: str | None) -> str | None:
    if not notes:
        return None
    match = _JIRA_KEY_RE.search(notes)
    return match.group(1) if match else None


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


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_MOCK_TIMESHEETS: list[HarvestTimesheet] = [
    HarvestTimesheet(
        id="ts-001",
        date=date(2026, 3, 31),
        user_id="u-alice",
        user_name="Alice Dev",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-dev",
        task_name="Development",
        hours=6.5,
        notes="[PROJ-101] OAuth2 login implementation",
        billable=True,
        jira_ticket_key="PROJ-101",
    ),
    HarvestTimesheet(
        id="ts-002",
        date=date(2026, 4, 1),
        user_id="u-alice",
        user_name="Alice Dev",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-dev",
        task_name="Development",
        hours=7.0,
        notes="[PROJ-104] Billing module unit tests",
        billable=True,
        jira_ticket_key="PROJ-104",
    ),
    HarvestTimesheet(
        id="ts-003",
        date=date(2026, 3, 30),
        user_id="u-bob",
        user_name="Bob Design",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-design",
        task_name="Design",
        hours=5.0,
        notes="[PROJ-102] Dashboard wireframes v2",
        billable=True,
        jira_ticket_key="PROJ-102",
    ),
    HarvestTimesheet(
        id="ts-004",
        date=date(2026, 4, 2),
        user_id="u-bob",
        user_name="Bob Design",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-dev",
        task_name="Development",
        hours=3.5,
        notes="[PROJ-105] Xero API spike — blocked on credentials",
        billable=True,
        jira_ticket_key="PROJ-105",
    ),
    HarvestTimesheet(
        id="ts-005",
        date=date(2026, 3, 31),
        user_id="u-charlie",
        user_name="Charlie Ops",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-devops",
        task_name="DevOps",
        hours=8.0,
        notes="[PROJ-103] CI/CD pipeline setup and docs",
        billable=True,
        jira_ticket_key="PROJ-103",
    ),
    HarvestTimesheet(
        id="ts-006",
        date=date(2026, 4, 3),
        user_id="u-charlie",
        user_name="Charlie Ops",
        project_id="hp-001",
        project_name="PROJ — Platform",
        task_id="task-devops",
        task_name="DevOps",
        hours=2.0,
        notes="Internal — team standup and planning",
        billable=False,
        jira_ticket_key=None,
    ),
]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class HarvestConnector:
    """Thin async wrapper around the Harvest v2 API."""

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        self._mock = settings.mock_mode if mock is None else mock
        self._headers = {
            "Authorization": f"Bearer {settings.harvest_token}",
            "Harvest-Account-Id": settings.harvest_account_id,
            "User-Agent": "InternalAgents/0.1 (BizOpsSVC@nebula.co.za)",
        }

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{_BASE}{path}", headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()

    async def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        """Iterate Harvest's page-based pagination."""
        entries: list[dict] = []
        base_params = dict(params or {})
        base_params["per_page"] = 100
        page = 1
        while True:
            base_params["page"] = page
            data = await self._get(path, params=base_params)
            batch = data.get("time_entries", [])
            entries.extend(batch)
            if not data.get("links", {}).get("next"):
                break
            page += 1
        return entries

    def _parse_entry(self, raw: dict) -> HarvestTimesheet:
        notes = raw.get("notes")
        return HarvestTimesheet(
            id=str(raw.get("id", "")),
            date=_parse_date(raw.get("spent_date")) or date.today(),
            user_id=str((raw.get("user") or {}).get("id", "")),
            user_name=(raw.get("user") or {}).get("name", ""),
            project_id=str((raw.get("project") or {}).get("id", "")),
            project_name=(raw.get("project") or {}).get("name", ""),
            task_id=str((raw.get("task") or {}).get("id", "")),
            task_name=(raw.get("task") or {}).get("name", ""),
            hours=float(raw.get("hours", 0.0)),
            notes=notes,
            billable=raw.get("billable", True),
            jira_ticket_key=_extract_jira_key(notes),
            invoice_id=str((raw.get("invoice") or {}).get("id", "")) or None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_timesheets(self, date_range: DateRange | None = None) -> list[HarvestTimesheet]:
        """Fetch all time entries, optionally scoped to a date range.

        Jira ticket keys are extracted from the notes field automatically.
        """
        if self._mock:
            logger.debug("HarvestConnector.fetch_timesheets → mock")
            if date_range:
                return [
                    t for t in _MOCK_TIMESHEETS
                    if date_range.start <= t.date <= date_range.end
                ]
            return _MOCK_TIMESHEETS

        params: dict[str, Any] = {}
        if date_range:
            params["from"] = str(date_range.start)
            params["to"] = str(date_range.end)

        logger.debug(f"HarvestConnector.fetch_timesheets params={params}")
        raw_entries = await self._paginate("/time_entries", params=params)
        return [self._parse_entry(e) for e in raw_entries]
