"""Jira connector — fetches stories, features (epics), and sprints.

Real API docs:
  Issues/Search : https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/
  Agile sprints : https://developer.atlassian.com/cloud/jira/software/rest/api-group-board/#api-rest-agile-1-0-board-boardid-sprint-get
"""

from __future__ import annotations

import asyncio
import base64
from datetime import date, datetime
from typing import Any

import httpx
from loguru import logger

from src.config import get_settings
from src.models.schemas import (
    DateRange,
    JiraBoard,
    JiraFeature,
    JiraProject,
    JiraSprint,
    JiraStory,
    JiraStoryStatus,
)

# ---------------------------------------------------------------------------
# Status mapping from Jira workflow names → our enum
# ---------------------------------------------------------------------------
_STATUS_MAP: dict[str, JiraStoryStatus] = {
    "backlog": JiraStoryStatus.BACKLOG,
    "to do": JiraStoryStatus.BACKLOG,
    "in progress": JiraStoryStatus.IN_PROGRESS,
    "in review": JiraStoryStatus.IN_REVIEW,
    "code review": JiraStoryStatus.IN_REVIEW,
    "done": JiraStoryStatus.DONE,
    "closed": JiraStoryStatus.DONE,
    "blocked": JiraStoryStatus.BLOCKED,
}


def _map_status(raw: str) -> JiraStoryStatus:
    return _STATUS_MAP.get(raw.lower(), JiraStoryStatus.IN_PROGRESS)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_simple_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

_MOCK_STORIES: list[JiraStory] = [
    JiraStory(
        key="PROJ-101",
        summary="Implement OAuth2 login flow",
        status=JiraStoryStatus.DONE,
        assignee="alice@nebula.co.za",
        story_points=5.0,
        sprint_id="sprint-42",
        project_key="PROJ",
        labels=["auth", "backend"],
        epic_link="PROJ-10",
    ),
    JiraStory(
        key="PROJ-102",
        summary="Design dashboard wireframes",
        status=JiraStoryStatus.IN_REVIEW,
        assignee="bob@nebula.co.za",
        story_points=3.0,
        sprint_id="sprint-42",
        project_key="PROJ",
        labels=["design"],
        epic_link="PROJ-11",
    ),
    JiraStory(
        key="PROJ-103",
        summary="Set up CI/CD pipeline",
        status=JiraStoryStatus.DONE,
        assignee="charlie@nebula.co.za",
        story_points=8.0,
        sprint_id="sprint-42",
        project_key="PROJ",
        labels=["devops"],
        epic_link="PROJ-10",
    ),
    JiraStory(
        key="PROJ-104",
        summary="Write unit tests for billing module",
        status=JiraStoryStatus.IN_PROGRESS,
        assignee="alice@nebula.co.za",
        story_points=5.0,
        sprint_id="sprint-42",
        project_key="PROJ",
        labels=["testing", "billing"],
    ),
    JiraStory(
        key="PROJ-105",
        summary="Integrate Xero invoicing API",
        status=JiraStoryStatus.BLOCKED,
        assignee="bob@nebula.co.za",
        story_points=13.0,
        sprint_id="sprint-42",
        project_key="PROJ",
        labels=["integration", "billing"],
        epic_link="PROJ-11",
    ),
]

_MOCK_FEATURES: list[JiraFeature] = [
    JiraFeature(
        key="PROJ-10",
        name="Platform Security",
        status="In Progress",
        project_key="PROJ",
        story_keys=["PROJ-101", "PROJ-103"],
        target_date=date(2026, 5, 31),
    ),
    JiraFeature(
        key="PROJ-11",
        name="Client Portal",
        status="In Progress",
        project_key="PROJ",
        story_keys=["PROJ-102", "PROJ-105"],
        target_date=date(2026, 6, 30),
    ),
]

_MOCK_BOARDS: list[JiraBoard] = [
    JiraBoard(id="1", name="Dev Board", type="scrum", project_key="PROJ", project_name="My Project"),
    JiraBoard(id="2", name="Ops Board", type="kanban", project_key="OPS", project_name="Operations"),
]

_MOCK_PROJECTS: list[JiraProject] = [
    JiraProject(key="PROJ", name="My Project", project_type="software", lead="alice@nebula.co.za"),
    JiraProject(key="OPS", name="Operations", project_type="business", lead="bob@nebula.co.za"),
]

_MOCK_SPRINTS: list[JiraSprint] = [
    JiraSprint(
        id="sprint-41",
        name="Sprint 41",
        state="closed",
        start_date=date(2026, 3, 17),
        end_date=date(2026, 3, 28),
        goal="Stabilise auth and improve test coverage",
        board_id="board-1",
        completed_points=32.0,
        total_points=37.0,
    ),
    JiraSprint(
        id="sprint-42",
        name="Sprint 42",
        state="active",
        start_date=date(2026, 3, 31),
        end_date=date(2026, 4, 11),
        goal="Ship client portal MVP and Xero integration",
        board_id="board-1",
        completed_points=13.0,
        total_points=34.0,
    ),
]


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class JiraConnector:
    """Thin async wrapper around the Jira REST API."""

    _BASE_REST = "/rest/api/3"
    _BASE_AGILE = "/rest/agile/1.0"

    def __init__(self, mock: bool | None = None) -> None:
        settings = get_settings()
        self._mock = settings.mock_mode if mock is None else mock
        self._base_url = settings.jira_url.rstrip("/")

        raw = f"{settings.jira_email}:{settings.jira_token}"
        token = base64.b64encode(raw.encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Execute an authenticated GET and return parsed JSON."""
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self._headers, params=params)
            response.raise_for_status()
            return response.json()

    async def _paginate_issues(self, jql: str, fields: list[str]) -> list[dict[str, Any]]:
        """Fetch all issues matching a JQL query, handling Jira's 100-item pages."""
        issues: list[dict[str, Any]] = []
        start = 0
        page_size = 100
        while True:
            data = await self._get(
                f"{self._BASE_REST}/search",
                params={
                    "jql": jql,
                    "fields": ",".join(fields),
                    "startAt": start,
                    "maxResults": page_size,
                },
            )
            batch = data.get("issues", [])
            issues.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
        return issues

    def _parse_issue_to_story(self, issue: dict[str, Any]) -> JiraStory:
        f = issue.get("fields", {})
        assignee_field = f.get("assignee") or {}
        sprint_list = f.get("customfield_10020") or []
        sprint_id: str | None = None
        if isinstance(sprint_list, list) and sprint_list:
            sprint_id = str(sprint_list[-1].get("id", ""))
        elif isinstance(sprint_list, dict):
            sprint_id = str(sprint_list.get("id", ""))

        return JiraStory(
            key=issue["key"],
            summary=f.get("summary", ""),
            status=_map_status((f.get("status") or {}).get("name", "")),
            assignee=(assignee_field.get("emailAddress") or assignee_field.get("displayName")),
            story_points=f.get("customfield_10016"),
            sprint_id=sprint_id,
            project_key=issue.get("key", "").split("-")[0],
            created_at=_parse_date(f.get("created")),
            updated_at=_parse_date(f.get("updated")),
            labels=f.get("labels", []),
            # customfield_10014 = Epic Link (classic projects)
            # parent.key = epic in next-gen/team-managed projects
            epic_link=(
                f.get("customfield_10014")
                or (f.get("parent") or {}).get("key")
            ),
        )

    def _parse_issue_to_feature(self, issue: dict[str, Any]) -> JiraFeature:
        f = issue.get("fields", {})
        return JiraFeature(
            key=issue["key"],
            name=f.get("summary", ""),
            status=(f.get("status") or {}).get("name", ""),
            project_key=issue.get("key", "").split("-")[0],
            target_date=_parse_simple_date(f.get("duedate")),
        )

    def _parse_sprint(self, raw: dict[str, Any]) -> JiraSprint:
        return JiraSprint(
            id=str(raw.get("id", "")),
            name=raw.get("name", ""),
            state=raw.get("state", ""),
            start_date=_parse_simple_date(raw.get("startDate")),
            end_date=_parse_simple_date(raw.get("endDate")),
            goal=raw.get("goal"),
            board_id=str(raw.get("originBoardId", "")) or None,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_stories(
        self,
        project_keys: list[str] | None = None,
        sprint_id: str | None = None,
        date_range: DateRange | None = None,
    ) -> list[JiraStory]:
        """Fetch stories (issue type Story) matching the given filters."""
        if self._mock:
            logger.debug("JiraConnector.fetch_stories → mock")
            stories = _MOCK_STORIES
            if sprint_id:
                stories = [s for s in stories if s.sprint_id == sprint_id]
            if project_keys:
                stories = [s for s in stories if s.project_key in project_keys]
            return stories

        clauses = ['issuetype = Story']
        if project_keys:
            keys_str = ", ".join(f'"{k}"' for k in project_keys)
            clauses.append(f"project in ({keys_str})")
        if sprint_id:
            clauses.append(f"sprint = {sprint_id}")
        if date_range:
            clauses.append(f'updated >= "{date_range.start}" AND updated <= "{date_range.end}"')

        jql = " AND ".join(clauses) + " ORDER BY updated DESC"
        fields = [
            "summary", "status", "assignee", "customfield_10016",
            "customfield_10020", "labels", "created", "updated",
            "customfield_10014", "parent",
        ]
        logger.debug(f"JiraConnector.fetch_stories JQL={jql!r}")
        issues = await self._paginate_issues(jql, fields)
        return [self._parse_issue_to_story(i) for i in issues]

    async def fetch_features(
        self,
        project_keys: list[str] | None = None,
    ) -> list[JiraFeature]:
        """Fetch epics (used as features) from Jira."""
        if self._mock:
            logger.debug("JiraConnector.fetch_features → mock")
            if project_keys:
                return [f for f in _MOCK_FEATURES if f.project_key in project_keys]
            return _MOCK_FEATURES

        clauses = ["issuetype = Epic"]
        if project_keys:
            keys_str = ", ".join(f'"{k}"' for k in project_keys)
            clauses.append(f"project in ({keys_str})")

        jql = " AND ".join(clauses) + " ORDER BY created DESC"
        fields = ["summary", "status", "duedate"]
        logger.debug(f"JiraConnector.fetch_features JQL={jql!r}")
        issues = await self._paginate_issues(jql, fields)
        features = [self._parse_issue_to_feature(i) for i in issues]

        # Backfill story_keys per epic via a second pass
        # TODO: use the Jira Epic Children endpoint for efficiency
        # https://developer.atlassian.com/cloud/jira/software/rest/api-group-epic/
        return features

    async def fetch_projects(
        self,
        project_keys: list[str] | None = None,
    ) -> list[JiraProject]:
        """Fetch projects visible to this account.

        Requires read:project:jira.
        """
        if self._mock:
            logger.debug("JiraConnector.fetch_projects → mock")
            if project_keys:
                return [p for p in _MOCK_PROJECTS if p.key in project_keys]
            return _MOCK_PROJECTS

        params: dict[str, Any] = {"maxResults": 50, "orderBy": "name"}
        data = await self._get(f"{self._BASE_REST}/project/search", params=params)
        projects = []
        for raw in data.get("values", []):
            lead_field = raw.get("lead") or {}
            projects.append(
                JiraProject(
                    key=raw["key"],
                    name=raw.get("name", ""),
                    project_type=raw.get("projectTypeKey"),
                    lead=lead_field.get("displayName") or lead_field.get("emailAddress"),
                    description=raw.get("description") or None,
                )
            )
        return projects

    async def fetch_boards(
        self,
        project_key: str | None = None,
    ) -> list[JiraBoard]:
        """Fetch all Scrum/Kanban boards visible to this account.

        Requires read:board-scope:jira-software.
        """
        if self._mock:
            logger.debug("JiraConnector.fetch_boards → mock")
            if project_key:
                return [b for b in _MOCK_BOARDS if b.project_key == project_key]
            return _MOCK_BOARDS

        params: dict[str, Any] = {"maxResults": 50}
        if project_key:
            params["projectKeyOrId"] = project_key

        data = await self._get(f"{self._BASE_AGILE}/board", params=params)
        boards = []
        for raw in data.get("values", []):
            location = raw.get("location") or {}
            boards.append(
                JiraBoard(
                    id=str(raw["id"]),
                    name=raw.get("name", ""),
                    type=raw.get("type", ""),
                    project_key=location.get("projectKey"),
                    project_name=location.get("projectName"),
                )
            )
        return boards

    async def _fetch_board_sprints(self, board_id: str, state: str | None) -> list[JiraSprint]:
        """Fetch sprints for a single board ID."""
        params: dict[str, Any] = {"maxResults": 50}
        if state:
            params["state"] = state
        data = await self._get(f"{self._BASE_AGILE}/board/{board_id}/sprint", params=params)
        return [self._parse_sprint(s) for s in data.get("values", [])]

    async def fetch_sprints(
        self,
        board_id: str | None = None,
        state: str | None = None,
    ) -> list[JiraSprint]:
        """Fetch sprints. If board_id is omitted, auto-discovers all scrum boards first."""
        if self._mock:
            logger.debug("JiraConnector.fetch_sprints → mock")
            if state:
                return [s for s in _MOCK_SPRINTS if s.state == state]
            return _MOCK_SPRINTS

        if board_id:
            return await self._fetch_board_sprints(board_id, state)

        # Auto-discover: fetch all boards then gather sprints for every scrum board
        boards = await self.fetch_boards()
        scrum_boards = [b for b in boards if b.type == "scrum"]
        if not scrum_boards:
            logger.warning("fetch_sprints: no scrum boards found — returning empty list")
            return []

        logger.debug(f"fetch_sprints: auto-discovered {len(scrum_boards)} scrum board(s)")
        results = await asyncio.gather(
            *[self._fetch_board_sprints(b.id, state) for b in scrum_boards],
            return_exceptions=True,
        )
        sprints: list[JiraSprint] = []
        for board, outcome in zip(scrum_boards, results):
            if isinstance(outcome, BaseException):
                logger.warning(f"[jira] sprints fetch failed for board {board.id}: {outcome}")
            else:
                sprints.extend(outcome)
        return sprints
