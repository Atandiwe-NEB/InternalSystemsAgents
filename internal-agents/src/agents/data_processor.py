"""Data Processor Agent — cleans, normalises, and joins the raw CollectionResult
into a single typed ProcessedDataset.

Processing happens in two ordered passes:
  1. Deterministic Python pass — exact-key joins (fast, zero LLM cost)
  2. Claude fuzzy pass — matches leftovers that have no exact key (e.g. a
     timesheet whose notes reference a story differently, or a contract whose
     deal_id field is blank but the name clearly maps to a deal)
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Callable

from loguru import logger

from src.agents.base import BaseAgent, ToolSpec
from src.models.schemas import (
    CollectionResult,
    ContractorCost,
    DealWithContract,
    HarvestTimesheet,
    HubSpotDeal,
    JiraStory,
    PandaDocContract,
    PandaDocStatus,
    ProcessedDataset,
    StoryHours,
    XeroContractor,
    XeroSupplierInvoice,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Data Processor for an internal business intelligence system.
You receive unmatched records — Harvest timesheets that couldn't be tied to a
Jira story, and PandaDoc contracts that have no HubSpot deal ID — and you
propose the most likely matches based on names, descriptions, and context.

Rules:
  - Only suggest a match when you are reasonably confident (>70%).
  - A timesheet may match at most ONE story; a contract may match at most ONE deal.
  - If nothing fits, leave the item unmatched — do not guess wildly.
  - Always call the `submit_fuzzy_matches` tool with your decisions.
    Do not answer in prose.
"""

# ---------------------------------------------------------------------------
# Fuzzy-match tool definition
# ---------------------------------------------------------------------------

_FUZZY_MATCH_TOOL: dict[str, Any] = {
    "name": "submit_fuzzy_matches",
    "description": (
        "Submit best-guess matches for unmatched timesheets and contracts. "
        "Call this tool once with all your decisions."
    ),
    "input_schema": {
        "type": "object",
        "required": ["timesheet_story_matches", "contract_deal_matches"],
        "properties": {
            "timesheet_story_matches": {
                "type": "array",
                "description": "Each item maps a timesheet id to the best Jira story key.",
                "items": {
                    "type": "object",
                    "required": ["timesheet_id", "story_key"],
                    "properties": {
                        "timesheet_id": {"type": "string"},
                        "story_key": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "contract_deal_matches": {
                "type": "array",
                "description": "Each item maps a contract document_id to the best HubSpot deal id.",
                "items": {
                    "type": "object",
                    "required": ["contract_id", "deal_id"],
                    "properties": {
                        "contract_id": {"type": "string"},
                        "deal_id": {"type": "string"},
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Signed statuses — used to determine has_signed_contract
# ---------------------------------------------------------------------------

_SIGNED_STATUSES = {PandaDocStatus.COMPLETED, PandaDocStatus.APPROVED}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DataProcessorAgent:
    """Joins and cleans a CollectionResult into a ProcessedDataset.

    Pass 1 (deterministic) runs in pure Python with zero LLM calls.
    Pass 2 (fuzzy) only runs if there are leftover unmatched records, keeping
    LLM usage proportional to the amount of messy data.
    """

    def __init__(self) -> None:
        self._agent = BaseAgent(
            name="data-processor",
            system_prompt=_SYSTEM_PROMPT,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def process(
        self,
        data: CollectionResult,
        progress: Callable[[str], None] | None = None,
    ) -> ProcessedDataset:
        """Transform a raw CollectionResult into a joined ProcessedDataset."""
        notes: list[str] = []

        # ---- Pass 1: deterministic joins ----
        story_hours, unmatched_timesheets = self._join_stories_timesheets(
            data.stories, data.timesheets, notes
        )
        deals_with_contracts, unmatched_contracts = self._join_deals_contracts(
            data.deals, data.contracts, notes
        )
        contractor_costs = self._aggregate_contractor_costs(
            data.contractors, data.supplier_invoices, notes
        )

        # ---- Pass 2: Claude fuzzy join (only if there's something to match) ----
        if unmatched_timesheets or unmatched_contracts:
            fuzzy_ts_matches, fuzzy_contract_matches = await self._fuzzy_match(
                unmatched_timesheets, data.stories,
                unmatched_contracts, data.deals,
                notes,
                progress=progress,
            )

            # Apply fuzzy timesheet matches
            story_index = {sh.story.key: sh for sh in story_hours}
            still_unmatched_ts: list[HarvestTimesheet] = []
            for ts in unmatched_timesheets:
                matched_key = fuzzy_ts_matches.get(ts.id)
                if matched_key and matched_key in story_index:
                    sh = story_index[matched_key]
                    sh.timesheets.append(ts)
                    sh.total_hours += ts.hours
                    sh.total_billable_hours += ts.hours if ts.billable else 0.0
                else:
                    still_unmatched_ts.append(ts)
            unmatched_timesheets = still_unmatched_ts

            # Apply fuzzy contract matches
            deal_index = {dwc.deal.id: dwc for dwc in deals_with_contracts}
            still_unmatched_contracts: list[PandaDocContract] = []
            for contract in unmatched_contracts:
                matched_deal_id = fuzzy_contract_matches.get(contract.document_id)
                if matched_deal_id and matched_deal_id in deal_index:
                    dwc = deal_index[matched_deal_id]
                    dwc.contract = contract
                    dwc.has_signed_contract = contract.status in _SIGNED_STATUSES
                else:
                    still_unmatched_contracts.append(contract)
            unmatched_contracts = still_unmatched_contracts

        logger.info(
            f"[data-processor] done | "
            f"story_hours={len(story_hours)} deals={len(deals_with_contracts)} "
            f"contractors={len(contractor_costs)} "
            f"unmatched_ts={len(unmatched_timesheets)} "
            f"unmatched_contracts={len(unmatched_contracts)}"
        )

        return ProcessedDataset(
            source_result=data,
            story_hours=story_hours,
            deals_with_contracts=deals_with_contracts,
            contractor_costs=contractor_costs,
            unmatched_timesheets=unmatched_timesheets,
            unmatched_contracts=unmatched_contracts,
            processing_notes=notes,
        )

    # ------------------------------------------------------------------
    # Pass 1 — deterministic joins
    # ------------------------------------------------------------------

    @staticmethod
    def _join_stories_timesheets(
        stories: list[JiraStory],
        timesheets: list[HarvestTimesheet],
        notes: list[str],
    ) -> tuple[list[StoryHours], list[HarvestTimesheet]]:
        """Exact-key join: Harvest timesheet.jira_ticket_key → JiraStory.key."""
        story_map: dict[str, StoryHours] = {
            s.key: StoryHours(story=s) for s in stories
        }

        unmatched: list[HarvestTimesheet] = []
        for ts in timesheets:
            key = ts.jira_ticket_key
            if key and key in story_map:
                sh = story_map[key]
                sh.timesheets.append(ts)
                sh.total_hours += ts.hours
                if ts.billable:
                    sh.total_billable_hours += ts.hours
            else:
                unmatched.append(ts)

        matched_count = len(timesheets) - len(unmatched)
        notes.append(
            f"Pass 1 — timesheets: {matched_count}/{len(timesheets)} matched by Jira key; "
            f"{len(unmatched)} need fuzzy matching."
        )
        logger.debug(f"[data-processor] timesheet exact-match: {matched_count}/{len(timesheets)}")
        return list(story_map.values()), unmatched

    @staticmethod
    def _join_deals_contracts(
        deals: list[HubSpotDeal],
        contracts: list[PandaDocContract],
        notes: list[str],
    ) -> tuple[list[DealWithContract], list[PandaDocContract]]:
        """Exact join using two possible link fields:
          1. deal.pandadoc_contract_id  == contract.document_id  (set in HubSpot)
          2. contract.deal_id           == deal.id               (set in PandaDoc metadata)
        """
        # Build lookup in both directions
        contract_by_doc_id: dict[str, PandaDocContract] = {
            c.document_id: c for c in contracts
        }
        contract_by_deal_id: dict[str, PandaDocContract] = {
            c.deal_id: c for c in contracts if c.deal_id
        }
        matched_contract_ids: set[str] = set()

        result: list[DealWithContract] = []
        for deal in deals:
            contract: PandaDocContract | None = None

            # Priority 1: HubSpot deal carries the PandaDoc document ID
            if deal.pandadoc_contract_id:
                contract = contract_by_doc_id.get(deal.pandadoc_contract_id)

            # Priority 2: PandaDoc contract carries the HubSpot deal ID
            if contract is None:
                contract = contract_by_deal_id.get(deal.id)

            if contract:
                matched_contract_ids.add(contract.document_id)

            result.append(
                DealWithContract(
                    deal=deal,
                    contract=contract,
                    has_signed_contract=(
                        contract is not None and contract.status in _SIGNED_STATUSES
                    ),
                )
            )

        unmatched = [c for c in contracts if c.document_id not in matched_contract_ids]
        matched_count = len(contracts) - len(unmatched)
        notes.append(
            f"Pass 1 — contracts: {matched_count}/{len(contracts)} matched to a deal; "
            f"{len(unmatched)} need fuzzy matching."
        )
        logger.debug(f"[data-processor] contract exact-match: {matched_count}/{len(contracts)}")
        return result, unmatched

    @staticmethod
    def _aggregate_contractor_costs(
        contractors: list[XeroContractor],
        invoices: list[XeroSupplierInvoice],
        notes: list[str],
    ) -> list[ContractorCost]:
        """Group Xero invoices by contractor (contact_id) and sum totals."""
        cost_map: dict[str, ContractorCost] = {
            c.contact_id: ContractorCost(contractor=c) for c in contractors
        }
        orphaned = 0
        for inv in invoices:
            if inv.contact_id in cost_map:
                cc = cost_map[inv.contact_id]
                cc.invoices.append(inv)
                cc.total_invoiced += inv.amount_due
                cc.total_paid += inv.amount_paid
            else:
                orphaned += 1
                logger.warning(
                    f"[data-processor] invoice {inv.invoice_number} has no matching "
                    f"contractor contact_id='{inv.contact_id}'"
                )

        if orphaned:
            notes.append(f"Pass 1 — {orphaned} supplier invoice(s) had no matching Xero contractor.")

        notes.append(
            f"Pass 1 — contractor costs: {len(cost_map)} contractors aggregated, "
            f"{sum(len(cc.invoices) for cc in cost_map.values())} invoices assigned."
        )
        return list(cost_map.values())

    # ------------------------------------------------------------------
    # Pass 2 — Claude fuzzy join
    # ------------------------------------------------------------------

    async def _fuzzy_match(
        self,
        unmatched_timesheets: list[HarvestTimesheet],
        stories: list[JiraStory],
        unmatched_contracts: list[PandaDocContract],
        deals: list[HubSpotDeal],
        notes: list[str],
        progress: Callable[[str], None] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Ask Claude to propose matches for leftover unmatched records.

        Returns:
          - timesheet_matches: {timesheet_id → story_key}
          - contract_matches:  {contract document_id → deal_id}
        """
        ts_matches: dict[str, str] = {}
        contract_matches: dict[str, str] = {}
        captured: list[dict] = []

        async def handle_submit_fuzzy_matches(inp: dict[str, Any]) -> str:
            captured.append(inp)
            return "Fuzzy matches recorded."

        spec = ToolSpec(
            definition=_FUZZY_MATCH_TOOL,
            handler=handle_submit_fuzzy_matches,
        )
        agent = BaseAgent(
            name="data-processor-fuzzy",
            system_prompt=_SYSTEM_PROMPT,
        ).with_tools([spec])
        if progress:
            agent.with_progress(progress)

        prompt = self._build_fuzzy_prompt(
            unmatched_timesheets, stories,
            unmatched_contracts, deals,
        )
        logger.info(
            f"[data-processor] fuzzy pass: "
            f"{len(unmatched_timesheets)} timesheets, {len(unmatched_contracts)} contracts"
        )
        await agent.run(prompt)

        if not captured:
            logger.warning("[data-processor] Claude did not call submit_fuzzy_matches")
            return ts_matches, contract_matches

        result = captured[0]

        for match in result.get("timesheet_story_matches", []):
            ts_id = match.get("timesheet_id", "")
            story_key = match.get("story_key", "")
            confidence = match.get("confidence", "low")
            reason = match.get("reason", "")
            if ts_id and story_key:
                ts_matches[ts_id] = story_key
                notes.append(
                    f"Pass 2 (fuzzy) — timesheet {ts_id} → story {story_key} "
                    f"[{confidence}] {reason}"
                )

        for match in result.get("contract_deal_matches", []):
            contract_id = match.get("contract_id", "")
            deal_id = match.get("deal_id", "")
            confidence = match.get("confidence", "low")
            reason = match.get("reason", "")
            if contract_id and deal_id:
                contract_matches[contract_id] = deal_id
                notes.append(
                    f"Pass 2 (fuzzy) — contract {contract_id} → deal {deal_id} "
                    f"[{confidence}] {reason}"
                )

        return ts_matches, contract_matches

    @staticmethod
    def _build_fuzzy_prompt(
        unmatched_timesheets: list[HarvestTimesheet],
        stories: list[JiraStory],
        unmatched_contracts: list[PandaDocContract],
        deals: list[HubSpotDeal],
    ) -> str:
        """Build the prompt for the fuzzy-matching pass."""
        sections: list[str] = []

        if unmatched_timesheets:
            ts_data = [
                {
                    "id": ts.id,
                    "date": str(ts.date),
                    "user": ts.user_name,
                    "project": ts.project_name,
                    "task": ts.task_name,
                    "hours": ts.hours,
                    "notes": ts.notes or "",
                }
                for ts in unmatched_timesheets
            ]
            story_data = [
                {
                    "key": s.key,
                    "summary": s.summary,
                    "assignee": s.assignee or "",
                    "labels": s.labels,
                }
                for s in stories
            ]
            sections.append(
                "## Unmatched Harvest Timesheets\n"
                + json.dumps(ts_data, indent=2)
                + "\n\n## Available Jira Stories\n"
                + json.dumps(story_data, indent=2)
            )

        if unmatched_contracts:
            contract_data = [
                {
                    "id": c.document_id,
                    "name": c.name,
                    "recipient": c.recipient_name or "",
                    "status": c.status.value,
                    "value": str(c.total_value or ""),
                }
                for c in unmatched_contracts
            ]
            deal_data = [
                {
                    "id": d.id,
                    "name": d.name,
                    "stage": d.stage.value,
                    "amount": str(d.amount or ""),
                    "owner": d.owner or "",
                }
                for d in deals
            ]
            sections.append(
                "## Unmatched PandaDoc Contracts\n"
                + json.dumps(contract_data, indent=2)
                + "\n\n## Available HubSpot Deals\n"
                + json.dumps(deal_data, indent=2)
            )

        return (
            "Match the unmatched records below to their most likely counterparts. "
            "Call `submit_fuzzy_matches` with your decisions.\n\n"
            + "\n\n".join(sections)
        )
