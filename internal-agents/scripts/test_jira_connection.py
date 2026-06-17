"""Quick smoke-test for the Jira connector.

Run from the project root:
    .venv\Scripts\python scripts\test_jira_connection.py

Checks:
  1. Auth  — GET /rest/api/3/myself
  2. Projects — lists all projects your account can see
  3. Boards   — lists your Scrum/Kanban boards (needed for sprint queries)
  4. Active sprints — fetches active stories via JQL
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

load_dotenv(override=True)

from src.config import get_settings
from src.gateway.connectors.jira import JiraConnector


async def main() -> None:
    settings = get_settings()

    print(f"\nJira URL  : {settings.jira_url}")
    print(f"Email     : {settings.jira_email}")
    print(f"Token     : {'*' * 8}{settings.jira_token[-4:] if len(settings.jira_token) > 4 else '(not set)'}")
    print(f"Mock mode : {settings.mock_mode}\n")

    if settings.mock_mode:
        print("ERROR: MOCK_MODE is still true — set MOCK_MODE=false in .env and re-run.")
        sys.exit(1)

    if "your-org" in settings.jira_url or not settings.jira_token or settings.jira_token == "your-jira-api-token":
        print("ERROR: Placeholder values still in .env — fill in JIRA_URL, JIRA_EMAIL, JIRA_TOKEN.")
        sys.exit(1)

    connector = JiraConnector(mock=False)

    # ---- 1. Auth check ----
    print("── 1. Auth check (GET /rest/api/3/myself) ──")
    try:
        me = await connector._get("/rest/api/3/myself")
        print(f"   ✓ Authenticated as: {me.get('displayName')} <{me.get('emailAddress')}>")
        print(f"     Account ID : {me.get('accountId')}")
        print(f"     Time zone  : {me.get('timeZone')}")
    except Exception as exc:
        print(f"   ✗ Auth FAILED: {exc}")
        print("     Check JIRA_URL, JIRA_EMAIL, and JIRA_TOKEN in .env")
        sys.exit(1)

    # ---- 2. Projects ----
    print("\n── 2. Accessible projects ──")
    try:
        projects_data = await connector._get(
            "/rest/api/3/project/search",
            params={"maxResults": 20, "orderBy": "name"},
        )
        projects = projects_data.get("values", [])
        if not projects:
            print("   (no projects returned — check account permissions)")
        for p in projects:
            print(f"   {p['key']:12} {p['name']}")
        print(f"\n   Total visible: {projects_data.get('total', len(projects))}")
    except Exception as exc:
        print(f"   ✗ Project list failed: {exc}")

    # ---- 3. Boards ----
    print("\n── 3. Boards (needed for sprint queries) ──")
    try:
        boards_data = await connector._get(
            "/rest/agile/1.0/board",
            params={"maxResults": 20},
        )
        boards = boards_data.get("values", [])
        if not boards:
            print("   (no boards — Jira Software may not be enabled for this site)")
        for b in boards:
            print(f"   ID {b['id']:6}  [{b['type']:7}]  {b['name']}")
        print()
        if boards:
            board_id = boards[0]["id"]
            print(f"   (Use board ID {board_id} as JIRA_BOARD_ID in extra_filters when asking about sprints)")
    except Exception as exc:
        print(f"   ✗ Board list failed: {exc}")

    # ---- 4. Active stories sample ----
    print("\n── 4. Active stories (JQL: issuetype = Story AND sprint in openSprints()) ──")
    sample_issue_key: str | None = None
    try:
        issues = await connector._paginate_issues(
            "issuetype = Story AND sprint in openSprints() ORDER BY updated DESC",
            ["summary", "status", "assignee", "customfield_10016"],
        )
        if not issues:
            print("   (no stories in open sprints — try a different JQL if your board uses a different sprint field)")
        for i in issues[:5]:
            status_name = (i.get("fields", {}).get("status") or {}).get("name", "?")
            summary = i.get("fields", {}).get("summary", "")[:60]
            print(f"   {i['key']:12} [{status_name:14}] {summary}")
        if len(issues) > 5:
            print(f"   ... and {len(issues) - 5} more")
        if issues:
            sample_issue_key = issues[0]["key"]
    except Exception as exc:
        print(f"   ✗ Story fetch failed: {exc}")

    # ---- 5. Raw field inspection — confirm custom field IDs ----
    print("\n── 5. Custom field verification (raw fields on one issue) ──")
    if not sample_issue_key:
        # Fall back to any issue
        try:
            any_issues = await connector._paginate_issues(
                "issuetype = Story ORDER BY updated DESC",
                ["summary"],
            )
            if any_issues:
                sample_issue_key = any_issues[0]["key"]
        except Exception:
            pass

    if sample_issue_key:
        try:
            raw = await connector._get(
                f"/rest/api/3/issue/{sample_issue_key}",
                params={"fields": "customfield_10016,customfield_10014,customfield_10020,parent,status,summary"},
            )
            f = raw.get("fields", {})
            print(f"   Issue: {sample_issue_key}  —  {f.get('summary', '')[:60]}")
            print(f"   customfield_10016 (story points) : {f.get('customfield_10016')!r}")
            print(f"   customfield_10014 (epic link)    : {f.get('customfield_10014')!r}")
            print(f"   customfield_10020 (sprint)       : {str(f.get('customfield_10020'))[:120]!r}")
            print(f"   parent                           : {(f.get('parent') or {}).get('key')!r}")

            # Warn if story points field is None — might be wrong custom field ID
            if f.get("customfield_10016") is None:
                print("\n   WARNING: customfield_10016 returned None.")
                print("   Story points may use a different custom field ID on this instance.")
                print("   Run this to find the correct ID:")
                print("   GET /rest/api/3/field  → look for 'Story Points' or 'Story point estimate'")
        except Exception as exc:
            print(f"   ✗ Raw field fetch failed: {exc}")
    else:
        print("   (skipped — no issues found to inspect)")

    print("\n── Done ──\n")


if __name__ == "__main__":
    asyncio.run(main())
