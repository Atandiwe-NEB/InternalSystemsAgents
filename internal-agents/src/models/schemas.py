"""Pydantic models for all data types flowing through the agent pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


class DateRange(BaseModel):
    start: date
    end: date


class Currency(str, Enum):
    ZAR = "ZAR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------


class JiraStoryStatus(str, Enum):
    BACKLOG = "Backlog"
    IN_PROGRESS = "In Progress"
    IN_REVIEW = "In Review"
    DONE = "Done"
    BLOCKED = "Blocked"


class JiraStory(BaseModel):
    key: str = Field(..., description="e.g. PROJ-123")
    summary: str
    status: JiraStoryStatus
    assignee: str | None = None
    story_points: float | None = None
    sprint_id: str | None = None
    project_key: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    labels: list[str] = Field(default_factory=list)
    epic_link: str | None = None


class JiraFeature(BaseModel):
    key: str = Field(..., description="Epic or feature key, e.g. PROJ-10")
    name: str
    status: str
    project_key: str = ""
    story_keys: list[str] = Field(default_factory=list)
    target_date: date | None = None


class JiraSprint(BaseModel):
    id: str
    name: str
    state: str = Field(..., description="active | closed | future")
    start_date: date | None = None
    end_date: date | None = None
    goal: str | None = None
    board_id: str | None = None
    completed_points: float | None = None
    total_points: float | None = None


class JiraBoard(BaseModel):
    id: str
    name: str
    type: str = Field(..., description="scrum | kanban | simple")
    project_key: str | None = None
    project_name: str | None = None


class JiraProject(BaseModel):
    key: str
    name: str
    project_type: str | None = None
    lead: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------


class DealStage(str, Enum):
    APPOINTMENT_SCHEDULED = "appointmentscheduled"
    QUALIFIED = "qualifiedtobuy"
    PRESENTATION = "presentationscheduled"
    DECISION = "decisionmakerboughtin"
    CONTRACT_SENT = "contractsent"
    CLOSED_WON = "closedwon"
    CLOSED_LOST = "closedlost"


class HubSpotDeal(BaseModel):
    id: str
    name: str
    stage: DealStage
    amount: Decimal | None = None
    currency: Currency = Currency.ZAR
    close_date: date | None = None
    owner: str | None = None
    associated_contact_ids: list[str] = Field(default_factory=list)
    associated_company_id: str | None = None
    pandadoc_contract_id: str | None = Field(
        None, description="Linked PandaDoc document ID if set as a deal property"
    )
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HubSpotLead(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str | None = None
    company: str | None = None
    lifecycle_stage: str | None = None
    lead_status: str | None = None
    created_at: datetime | None = None


class HubSpotInvoice(BaseModel):
    id: str
    deal_id: str | None = None
    amount: Decimal
    currency: Currency = Currency.ZAR
    status: str = Field(..., description="draft | sent | paid | overdue")
    due_date: date | None = None
    issued_at: datetime | None = None


class HubSpotPricing(BaseModel):
    product_id: str
    name: str
    unit_price: Decimal
    currency: Currency = Currency.ZAR
    description: str | None = None


# ---------------------------------------------------------------------------
# Xero
# ---------------------------------------------------------------------------


class XeroContractor(BaseModel):
    contact_id: str
    name: str
    email: str | None = None
    phone: str | None = None
    tax_number: str | None = None
    account_number: str | None = None
    is_supplier: bool = True
    currency: Currency = Currency.ZAR


class XeroSupplierInvoice(BaseModel):
    invoice_id: str
    invoice_number: str
    contact_id: str
    contact_name: str | None = None
    status: str = Field(..., description="DRAFT | SUBMITTED | AUTHORISED | PAID | VOIDED")
    amount_due: Decimal
    amount_paid: Decimal = Decimal("0")
    currency: Currency = Currency.ZAR
    due_date: date | None = None
    issue_date: date | None = None
    reference: str | None = None
    line_items: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


class HarvestTimesheet(BaseModel):
    id: str
    date: date
    user_id: str
    user_name: str
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    hours: float
    notes: str | None = None
    billable: bool = True
    jira_ticket_key: str | None = Field(
        None, description="Extracted from notes or external reference"
    )
    invoice_id: str | None = None


# ---------------------------------------------------------------------------
# PandaDoc
# ---------------------------------------------------------------------------


class PandaDocStatus(str, Enum):
    DRAFT = "document.draft"
    SENT = "document.sent"
    VIEWED = "document.viewed"
    WAITING_APPROVAL = "document.waiting_approval"
    APPROVED = "document.approved"
    REJECTED = "document.rejected"
    COMPLETED = "document.completed"
    EXPIRED = "document.expired"


class PandaDocContract(BaseModel):
    document_id: str
    name: str
    status: PandaDocStatus
    deal_id: str | None = Field(
        None, description="HubSpot deal ID stored as a custom field"
    )
    recipient_name: str | None = None
    recipient_email: str | None = None
    total_value: Decimal | None = None
    currency: Currency = Currency.ZAR
    created_at: datetime | None = None
    sent_at: datetime | None = None
    completed_at: datetime | None = None
    expiry_date: date | None = None


# ---------------------------------------------------------------------------
# Envelope / pipeline models
# ---------------------------------------------------------------------------


class DataSource(str, Enum):
    JIRA = "jira"
    HUBSPOT = "hubspot"
    XERO = "xero"
    HARVEST = "harvest"
    PANDADOC = "pandadoc"


class CollectionRequest(BaseModel):
    """Specifies what to fetch and from where."""

    sources: list[DataSource] = Field(
        ..., description="Which connectors to activate"
    )
    date_range: DateRange | None = None
    project_keys: list[str] = Field(
        default_factory=list, description="Jira project keys to scope the query"
    )
    sprint_id: str | None = None
    extra_filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Connector-specific extra filters passed through verbatim",
    )


class CollectionResult(BaseModel):
    """Raw, unprocessed data bundle returned by the Data Collector."""

    request: CollectionRequest
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Jira
    stories: list[JiraStory] = Field(default_factory=list)
    features: list[JiraFeature] = Field(default_factory=list)
    sprints: list[JiraSprint] = Field(default_factory=list)
    boards: list[JiraBoard] = Field(default_factory=list)
    projects: list[JiraProject] = Field(default_factory=list)

    # HubSpot
    deals: list[HubSpotDeal] = Field(default_factory=list)
    leads: list[HubSpotLead] = Field(default_factory=list)
    hubspot_invoices: list[HubSpotInvoice] = Field(default_factory=list)
    pricing: list[HubSpotPricing] = Field(default_factory=list)

    # Xero
    contractors: list[XeroContractor] = Field(default_factory=list)
    supplier_invoices: list[XeroSupplierInvoice] = Field(default_factory=list)

    # Harvest
    timesheets: list[HarvestTimesheet] = Field(default_factory=list)

    # PandaDoc
    contracts: list[PandaDocContract] = Field(default_factory=list)

    errors: dict[DataSource, str] = Field(
        default_factory=dict,
        description="Per-source error messages if a connector failed",
    )


# ---------------------------------------------------------------------------
# Processed dataset (output of Data Processor)
# ---------------------------------------------------------------------------


class StoryHours(BaseModel):
    """A Jira story enriched with its logged Harvest hours."""

    story: JiraStory
    timesheets: list[HarvestTimesheet] = Field(default_factory=list)
    total_hours: float = 0.0
    total_billable_hours: float = 0.0


class DealWithContract(BaseModel):
    """A HubSpot deal linked to its PandaDoc contract (if any)."""

    deal: HubSpotDeal
    contract: PandaDocContract | None = None
    has_signed_contract: bool = False


class ContractorCost(BaseModel):
    """A Xero contractor with their aggregated invoice totals."""

    contractor: XeroContractor
    invoices: list[XeroSupplierInvoice] = Field(default_factory=list)
    total_invoiced: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")


class ProcessedDataset(BaseModel):
    """Cleaned, joined, deduplicated dataset produced by the Data Processor."""

    source_result: CollectionResult
    processed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    story_hours: list[StoryHours] = Field(default_factory=list)
    deals_with_contracts: list[DealWithContract] = Field(default_factory=list)
    contractor_costs: list[ContractorCost] = Field(default_factory=list)

    unmatched_timesheets: list[HarvestTimesheet] = Field(default_factory=list)
    unmatched_contracts: list[PandaDocContract] = Field(default_factory=list)

    processing_notes: list[str] = Field(
        default_factory=list,
        description="Human-readable notes about fuzzy matches, dropped rows, etc.",
    )


# ---------------------------------------------------------------------------
# Analysis models
# ---------------------------------------------------------------------------


class AnalysisRequest(BaseModel):
    """The user's analytical question, paired with the processed data."""

    question: str = Field(..., description="Natural-language question to answer")
    focus_sources: list[DataSource] = Field(
        default_factory=list,
        description="Optionally narrow analysis to specific sources",
    )
    date_range: DateRange | None = None


class Metric(BaseModel):
    name: str
    value: float | Decimal | str
    unit: str | None = None
    context: str | None = None


class Anomaly(BaseModel):
    description: str
    severity: str = Field(..., description="low | medium | high")
    affected_entities: list[str] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """Numeric findings + narrative insights produced by the Data Analyst."""

    question: str
    metrics: list[Metric] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)
    insights: str = Field(..., description="Natural-language narrative of findings")
    confidence: str = Field(
        "medium", description="low | medium | high — analyst's self-assessed confidence"
    )
    analysed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


class ReportAudience(str, Enum):
    EXECUTIVE = "executive"
    TECHNICAL = "technical"
    FINANCE = "finance"
    OPERATIONS = "operations"


class ReportRequest(BaseModel):
    """Describes the desired output format and audience."""

    title: str
    audience: ReportAudience = ReportAudience.OPERATIONS
    include_raw_tables: bool = True
    include_charts_placeholder: bool = False
    extra_instructions: str | None = None


class ReportSection(BaseModel):
    heading: str
    body: str = Field(..., description="Markdown-formatted section body")


class ReportResult(BaseModel):
    """The final deliverable: a polished markdown report + structured summary."""

    title: str
    audience: ReportAudience
    tldr: str = Field(..., description="One-paragraph executive summary")
    sections: list[ReportSection] = Field(default_factory=list)
    markdown: str = Field(..., description="Full assembled markdown document")
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_analysis: AnalysisResult | None = None
