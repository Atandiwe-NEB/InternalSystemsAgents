"""Data Analyst Agent — computes metrics, detects anomalies, and generates
narrative insights from a ProcessedDataset.

Processing happens in two ordered phases:
  1. Deterministic Python phase — arithmetic metrics and rule-based anomaly
     detection (fast, zero LLM cost, fully reproducible)
  2. Claude insight phase — interprets the computed metrics in the context of
     the user's question and produces a narrative AnalysisResult
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from loguru import logger

from src.agents.base import BaseAgent
from src.models.schemas import (
    AnalysisRequest,
    AnalysisResult,
    Anomaly,
    ContractorCost,
    DealWithContract,
    JiraStoryStatus,
    Metric,
    PandaDocStatus,
    ProcessedDataset,
    StoryHours,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are the Data Analyst for an internal business intelligence system.
You receive pre-computed metrics and anomaly flags, plus the user's question,
and you write a concise analytical narrative.

Guidelines:
  - Lead with the direct answer to the user's question.
  - Reference specific metric values from the data — never make numbers up.
  - Call out the most important anomalies clearly.
  - Rate your confidence (high / medium / low) at the end of your response
    using the format: CONFIDENCE: <level>
  - Keep the narrative under 400 words. Bullet points are fine.
  - Do not suggest fetching more data or deferring to another system.
"""

# ---------------------------------------------------------------------------
# Anomaly thresholds
# ---------------------------------------------------------------------------

# Hours logged that exceed this multiple of story points are flagged
_HOURS_PER_POINT_WARN = 4.0
# Deals closed without a contract are high-severity
# Invoices unpaid past due date are medium-severity
# Stories with 0 hours but Done status are low-severity


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class DataAnalystAgent:
    """Analyses a ProcessedDataset and answers a natural-language question.

    Phase 1 computes all numeric metrics in pure Python.
    Phase 2 sends those metrics to Claude for narrative interpretation.
    """

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def analyse(
        self,
        dataset: ProcessedDataset,
        request: AnalysisRequest,
        progress: Callable[[str], None] | None = None,
    ) -> AnalysisResult:
        """Compute metrics, detect anomalies, then generate narrative insights."""
        logger.info(f"[data-analyst] question={request.question!r}")

        # Phase 1 — deterministic
        metrics = self._compute_metrics(dataset)
        anomalies = self._detect_anomalies(dataset)

        logger.info(
            f"[data-analyst] phase 1 done | "
            f"metrics={len(metrics)} anomalies={len(anomalies)}"
        )

        # Phase 2 — Claude narrative
        insights, confidence = await self._generate_insights(
            request, metrics, anomalies, dataset, progress=progress
        )

        return AnalysisResult(
            question=request.question,
            metrics=metrics,
            anomalies=anomalies,
            insights=insights,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Phase 1 — metric computation
    # ------------------------------------------------------------------

    def _compute_metrics(self, dataset: ProcessedDataset) -> list[Metric]:
        metrics: list[Metric] = []

        metrics.extend(self._sprint_velocity_metrics(dataset.story_hours))
        metrics.extend(self._hours_per_story_metrics(dataset.story_hours))
        metrics.extend(self._deal_metrics(dataset.deals_with_contracts))
        metrics.extend(self._contractor_cost_metrics(dataset.contractor_costs))
        metrics.extend(self._contract_coverage_metrics(dataset.deals_with_contracts))

        return metrics

    @staticmethod
    def _sprint_velocity_metrics(story_hours: list[StoryHours]) -> list[Metric]:
        """Completed story points and done-story rate."""
        total_points = sum(
            sh.story.story_points or 0.0 for sh in story_hours
        )
        completed_points = sum(
            sh.story.story_points or 0.0
            for sh in story_hours
            if sh.story.status == JiraStoryStatus.DONE
        )
        total_stories = len(story_hours)
        done_stories = sum(
            1 for sh in story_hours if sh.story.status == JiraStoryStatus.DONE
        )

        metrics = [
            Metric(
                name="total_story_points",
                value=round(total_points, 1),
                unit="points",
                context="All stories in scope",
            ),
            Metric(
                name="completed_story_points",
                value=round(completed_points, 1),
                unit="points",
                context="Stories with status Done",
            ),
        ]
        if total_stories:
            completion_pct = round(done_stories / total_stories * 100, 1)
            metrics.append(
                Metric(
                    name="story_completion_rate",
                    value=completion_pct,
                    unit="%",
                    context=f"{done_stories}/{total_stories} stories Done",
                )
            )
        if total_points:
            velocity_pct = round(completed_points / total_points * 100, 1)
            metrics.append(
                Metric(
                    name="point_completion_rate",
                    value=velocity_pct,
                    unit="%",
                    context=f"{completed_points}/{total_points} points completed",
                )
            )
        return metrics

    @staticmethod
    def _hours_per_story_metrics(story_hours: list[StoryHours]) -> list[Metric]:
        """Total logged hours, billable hours, and hours-per-story-point ratio."""
        total_hours = sum(sh.total_hours for sh in story_hours)
        billable_hours = sum(sh.total_billable_hours for sh in story_hours)
        total_points = sum(
            sh.story.story_points or 0.0
            for sh in story_hours
            if sh.story.story_points
        )

        metrics = [
            Metric(
                name="total_logged_hours",
                value=round(total_hours, 2),
                unit="hours",
                context="All Harvest timesheets matched to stories",
            ),
            Metric(
                name="billable_hours",
                value=round(billable_hours, 2),
                unit="hours",
            ),
        ]
        if total_hours:
            metrics.append(
                Metric(
                    name="billable_hours_rate",
                    value=round(billable_hours / total_hours * 100, 1),
                    unit="%",
                )
            )
        if total_points:
            metrics.append(
                Metric(
                    name="hours_per_story_point",
                    value=round(total_hours / total_points, 2),
                    unit="hours/point",
                    context="Lower is more efficient",
                )
            )
        return metrics

    @staticmethod
    def _deal_metrics(deals_with_contracts: list[DealWithContract]) -> list[Metric]:
        """Total pipeline value, closed-won value, and deal counts."""
        from src.models.schemas import DealStage

        all_deals = [dwc.deal for dwc in deals_with_contracts]
        closed_won = [d for d in all_deals if d.stage == DealStage.CLOSED_WON]

        total_pipeline = sum(d.amount or Decimal("0") for d in all_deals)
        closed_value = sum(d.amount or Decimal("0") for d in closed_won)

        metrics = [
            Metric(
                name="total_pipeline_value",
                value=str(total_pipeline),
                unit="ZAR",
                context=f"{len(all_deals)} deals",
            ),
            Metric(
                name="closed_won_value",
                value=str(closed_value),
                unit="ZAR",
                context=f"{len(closed_won)} deals closed-won",
            ),
        ]
        if all_deals:
            win_rate = round(len(closed_won) / len(all_deals) * 100, 1)
            metrics.append(
                Metric(name="deal_win_rate", value=win_rate, unit="%")
            )

        # Revenue per logged hour (cross-source metric)
        return metrics

    @staticmethod
    def _contractor_cost_metrics(contractor_costs: list[ContractorCost]) -> list[Metric]:
        """Total contractor spend, paid vs outstanding."""
        total_invoiced = sum(cc.total_invoiced for cc in contractor_costs)
        total_paid = sum(cc.total_paid for cc in contractor_costs)
        outstanding = total_invoiced - total_paid

        metrics = [
            Metric(
                name="total_contractor_invoiced",
                value=str(total_invoiced),
                unit="ZAR",
                context=f"{len(contractor_costs)} contractors",
            ),
            Metric(
                name="total_contractor_paid",
                value=str(total_paid),
                unit="ZAR",
            ),
            Metric(
                name="contractor_outstanding",
                value=str(outstanding),
                unit="ZAR",
                context="Invoiced but not yet paid",
            ),
        ]

        # Per-contractor breakdown
        for cc in contractor_costs:
            if cc.total_invoiced > Decimal("0"):
                metrics.append(
                    Metric(
                        name=f"contractor_cost_{cc.contractor.name.replace(' ', '_').lower()}",
                        value=str(cc.total_invoiced),
                        unit="ZAR",
                        context=f"{len(cc.invoices)} invoice(s)",
                    )
                )
        return metrics

    @staticmethod
    def _contract_coverage_metrics(deals_with_contracts: list[DealWithContract]) -> list[Metric]:
        """What fraction of deals have a signed contract."""
        total = len(deals_with_contracts)
        signed = sum(1 for dwc in deals_with_contracts if dwc.has_signed_contract)
        has_any_contract = sum(1 for dwc in deals_with_contracts if dwc.contract)

        metrics: list[Metric] = []
        if total:
            metrics += [
                Metric(
                    name="deals_with_signed_contract",
                    value=signed,
                    unit="deals",
                    context=f"Out of {total} total deals",
                ),
                Metric(
                    name="contract_coverage_rate",
                    value=round(has_any_contract / total * 100, 1),
                    unit="%",
                    context="Deals that have any PandaDoc document",
                ),
                Metric(
                    name="signed_contract_rate",
                    value=round(signed / total * 100, 1),
                    unit="%",
                    context="Deals with a completed/approved contract",
                ),
            ]
        return metrics

    # ------------------------------------------------------------------
    # Phase 1 — anomaly detection
    # ------------------------------------------------------------------

    def _detect_anomalies(self, dataset: ProcessedDataset) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        anomalies.extend(self._anomalies_stories(dataset.story_hours))
        anomalies.extend(self._anomalies_deals(dataset.deals_with_contracts))
        anomalies.extend(self._anomalies_contractors(dataset.contractor_costs))
        return anomalies

    @staticmethod
    def _anomalies_stories(story_hours: list[StoryHours]) -> list[Anomaly]:
        anomalies: list[Anomaly] = []
        for sh in story_hours:
            points = sh.story.story_points or 0.0

            # Done story with zero hours
            if sh.story.status == JiraStoryStatus.DONE and sh.total_hours == 0.0:
                anomalies.append(
                    Anomaly(
                        description=f"Story {sh.story.key} is Done but has no hours logged.",
                        severity="low",
                        affected_entities=[sh.story.key],
                    )
                )

            # Hours far exceed story point estimate
            if points and sh.total_hours > points * _HOURS_PER_POINT_WARN:
                ratio = round(sh.total_hours / points, 1)
                anomalies.append(
                    Anomaly(
                        description=(
                            f"Story {sh.story.key} logged {sh.total_hours}h against "
                            f"{points} points ({ratio}h/point — threshold is "
                            f"{_HOURS_PER_POINT_WARN}h/point)."
                        ),
                        severity="medium",
                        affected_entities=[sh.story.key],
                    )
                )

            # Blocked story with hours being logged
            if sh.story.status == JiraStoryStatus.BLOCKED and sh.total_hours > 0:
                anomalies.append(
                    Anomaly(
                        description=(
                            f"Story {sh.story.key} is Blocked but {sh.total_hours}h "
                            f"of work is still being logged."
                        ),
                        severity="medium",
                        affected_entities=[sh.story.key],
                    )
                )
        return anomalies

    @staticmethod
    def _anomalies_deals(deals_with_contracts: list[DealWithContract]) -> list[Anomaly]:
        from src.models.schemas import DealStage

        anomalies: list[Anomaly] = []
        for dwc in deals_with_contracts:
            # Closed-won deal with no signed contract
            if (
                dwc.deal.stage == DealStage.CLOSED_WON
                and not dwc.has_signed_contract
            ):
                anomalies.append(
                    Anomaly(
                        description=(
                            f"Deal '{dwc.deal.name}' is Closed-Won "
                            f"(value: {dwc.deal.amount} {dwc.deal.currency.value}) "
                            f"but has no completed PandaDoc contract."
                        ),
                        severity="high",
                        affected_entities=[dwc.deal.id],
                    )
                )

            # Contract sent but not yet signed and deal is close to close date
            if (
                dwc.contract
                and dwc.contract.status == PandaDocStatus.SENT
                and dwc.deal.close_date
            ):
                from datetime import date
                days_to_close = (dwc.deal.close_date - date.today()).days
                if days_to_close <= 7:
                    anomalies.append(
                        Anomaly(
                            description=(
                                f"Deal '{dwc.deal.name}' closes in {days_to_close} day(s) "
                                f"but contract '{dwc.contract.name}' is still unsigned."
                            ),
                            severity="high",
                            affected_entities=[dwc.deal.id, dwc.contract.document_id],
                        )
                    )
        return anomalies

    @staticmethod
    def _anomalies_contractors(contractor_costs: list[ContractorCost]) -> list[Anomaly]:
        from datetime import date

        anomalies: list[Anomaly] = []
        today = date.today()

        for cc in contractor_costs:
            # Contractor with no invoices in the dataset period
            if not cc.invoices:
                anomalies.append(
                    Anomaly(
                        description=(
                            f"Contractor '{cc.contractor.name}' has no invoices "
                            f"in the current dataset period."
                        ),
                        severity="low",
                        affected_entities=[cc.contractor.contact_id],
                    )
                )
                continue

            # Overdue unpaid invoices
            overdue = [
                inv for inv in cc.invoices
                if inv.due_date
                and inv.due_date < today
                and inv.amount_due > inv.amount_paid
                and inv.status not in ("PAID", "VOIDED")
            ]
            for inv in overdue:
                days_overdue = (today - inv.due_date).days
                anomalies.append(
                    Anomaly(
                        description=(
                            f"Invoice {inv.invoice_number} from '{cc.contractor.name}' "
                            f"is {days_overdue} day(s) overdue "
                            f"(outstanding: {inv.amount_due - inv.amount_paid} "
                            f"{inv.currency.value})."
                        ),
                        severity="medium",
                        affected_entities=[inv.invoice_id, cc.contractor.contact_id],
                    )
                )
        return anomalies

    # ------------------------------------------------------------------
    # Phase 2 — Claude narrative
    # ------------------------------------------------------------------

    async def _generate_insights(
        self,
        request: AnalysisRequest,
        metrics: list[Metric],
        anomalies: list[Anomaly],
        dataset: ProcessedDataset,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        """Ask Claude to interpret the computed metrics and answer the user's question.

        Returns (insights_text, confidence_level).
        """
        prompt = self._build_analysis_prompt(request, metrics, anomalies, dataset)
        agent = BaseAgent(name="data-analyst", system_prompt=_SYSTEM_PROMPT)
        if progress:
            agent.with_progress(progress)
        raw = await agent.run(prompt)

        # Extract confidence tag from Claude's response
        confidence = "medium"
        for line in reversed(raw.splitlines()):
            if line.strip().upper().startswith("CONFIDENCE:"):
                level = line.split(":", 1)[-1].strip().lower()
                if level in ("high", "medium", "low"):
                    confidence = level
                break

        # Strip the CONFIDENCE: line from the narrative
        insights = "\n".join(
            line for line in raw.splitlines()
            if not line.strip().upper().startswith("CONFIDENCE:")
        ).strip()

        return insights, confidence

    @staticmethod
    def _build_analysis_prompt(
        request: AnalysisRequest,
        metrics: list[Metric],
        anomalies: list[Anomaly],
        dataset: ProcessedDataset,
    ) -> str:
        metrics_json = json.dumps(
            [
                {
                    "name": m.name,
                    "value": str(m.value),
                    "unit": m.unit,
                    "context": m.context,
                }
                for m in metrics
            ],
            indent=2,
        )

        anomalies_json = json.dumps(
            [
                {
                    "description": a.description,
                    "severity": a.severity,
                    "entities": a.affected_entities,
                }
                for a in anomalies
            ],
            indent=2,
        )

        src = dataset.source_result
        project_lines = ""
        if src.projects:
            project_lines = "\n## Jira Projects\n" + "\n".join(
                f"- {p.key}: {p.name} (type: {p.project_type or '?'}, lead: {p.lead or '?'})"
                for p in src.projects
            )
        board_lines = ""
        if src.boards:
            board_lines = "\n## Jira Boards\n" + "\n".join(
                f"- ID {b.id}: {b.name} ({b.type})"
                + (f" — project {b.project_key}" if b.project_key else "")
                for b in src.boards
            )
        sprint_lines = ""
        if src.sprints:
            sprint_lines = "\n## Jira Sprints\n" + "\n".join(
                f"- {s.name} [{s.state}] board={s.board_id or '?'}"
                for s in src.sprints
            )

        data_summary = (
            f"Projects: {len(src.projects)} | "
            f"Stories: {len(dataset.story_hours)} | "
            f"Boards: {len(src.boards)} | "
            f"Sprints: {len(src.sprints)} | "
            f"Deals: {len(dataset.deals_with_contracts)} | "
            f"Contractors: {len(dataset.contractor_costs)} | "
            f"Unmatched timesheets: {len(dataset.unmatched_timesheets)} | "
            f"Unmatched contracts: {len(dataset.unmatched_contracts)}"
        )

        return f"""User question: {request.question}

Dataset summary: {data_summary}
{project_lines}
{board_lines}
{sprint_lines}

## Computed Metrics
{metrics_json}

## Detected Anomalies
{anomalies_json}

## Processing Notes
{chr(10).join(f"- {n}" for n in dataset.processing_notes) or "None"}

Answer the user's question using the data above. Call out the most important anomalies. End with CONFIDENCE: <high|medium|low>."""
