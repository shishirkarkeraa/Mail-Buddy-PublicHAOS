from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

TAXONOMY_VERSION = "2"
MODEL_NAME = "llama3.2:3b-instruct-q4_K_M"


class Category(StrEnum):
    SECURITY_OTP = "security_otp"
    SECURITY_PASSWORD_RESET = "security_password_reset"  # noqa: S105
    SECURITY_ACCOUNT_ALERT = "security_account_alert"
    FINANCE_BANK_TRANSACTION = "finance_bank_transaction"
    FINANCE_BILLS_UTILITIES = "finance_bills_utilities"
    FINANCE_RECEIPTS_INVOICES = "finance_receipts_invoices"
    PROMOTION_GENERAL = "promotion_general"
    COLLEGE_IMPORTANT = "college_important"
    COLLEGE_INTERNSHIP_OPPORTUNITY = "college_internship_opportunity"
    COLLEGE_PLACEMENT = "college_placement"
    COLLEGE_NOTICE = "college_notice"
    SUBSCRIPTION = "subscription"
    JOB_RELATED = "job_related"
    INTERNSHIP = "internship"
    SHOPPING_PROMOTION = "shopping_promotion"
    SHOPPING_ORDER_UPDATE = "shopping_order_update"
    SOCIAL = "social"
    DIRECT_PERSONAL = "direct_personal"
    TRAVEL = "travel"
    HEALTH_MEDICAL = "health_medical"
    EVENTS_TICKETS = "events_tickets"
    GOVERNMENT_LEGAL = "government_legal"
    OTHER = "other"


CATEGORY_LABELS: dict[Category, str] = {
    Category.SECURITY_OTP: "Security OTP",
    Category.SECURITY_PASSWORD_RESET: "Password Reset",
    Category.SECURITY_ACCOUNT_ALERT: "Account Alerts",
    Category.FINANCE_BANK_TRANSACTION: "Bank Transactions",
    Category.FINANCE_BILLS_UTILITIES: "Bills & Utilities",
    Category.FINANCE_RECEIPTS_INVOICES: "Receipts & Invoices",
    Category.PROMOTION_GENERAL: "General Promotions",
    Category.COLLEGE_IMPORTANT: "College Important",
    Category.COLLEGE_INTERNSHIP_OPPORTUNITY: "College Internship Opportunities",
    Category.COLLEGE_PLACEMENT: "College Placements",
    Category.COLLEGE_NOTICE: "College Notices",
    Category.SUBSCRIPTION: "Subscriptions",
    Category.JOB_RELATED: "Job Related",
    Category.INTERNSHIP: "Internship",
    Category.SHOPPING_PROMOTION: "Shopping Promotions",
    Category.SHOPPING_ORDER_UPDATE: "Order Updates",
    Category.SOCIAL: "Social",
    Category.DIRECT_PERSONAL: "Personal",
    Category.TRAVEL: "Travel",
    Category.HEALTH_MEDICAL: "Health & Medical",
    Category.EVENTS_TICKETS: "Events & Tickets",
    Category.GOVERNMENT_LEGAL: "Government & Legal",
    Category.OTHER: "Other",
}
ADDITIONAL_CATEGORIES = frozenset(
    {
        Category.FINANCE_BILLS_UTILITIES,
        Category.FINANCE_RECEIPTS_INVOICES,
        Category.TRAVEL,
        Category.HEALTH_MEDICAL,
        Category.EVENTS_TICKETS,
        Category.GOVERNMENT_LEGAL,
    }
)
NEEDS_REVIEW_LABEL = "Needs Review"

# Exact v1 names are retained only so GmailClient can rename existing labels in
# place. Renaming preserves label membership and avoids leaving duplicate
# Mail-Buddy/... labels in an already-authorized mailbox.
LEGACY_CATEGORY_LABELS: dict[Category, str] = {
    Category.SECURITY_OTP: "Mail-Buddy/Security/OTP",
    Category.SECURITY_PASSWORD_RESET: "Mail-Buddy/Security/Password Reset",
    Category.SECURITY_ACCOUNT_ALERT: "Mail-Buddy/Security/Account Alerts",
    Category.FINANCE_BANK_TRANSACTION: "Mail-Buddy/Finance/Bank Transactions",
    Category.FINANCE_BILLS_UTILITIES: "Mail-Buddy/Finance/Bills & Utilities",
    Category.FINANCE_RECEIPTS_INVOICES: "Mail-Buddy/Finance/Receipts & Invoices",
    Category.PROMOTION_GENERAL: "Mail-Buddy/Promotions/General",
    Category.COLLEGE_IMPORTANT: "Mail-Buddy/College/Important",
    Category.COLLEGE_INTERNSHIP_OPPORTUNITY: "Mail-Buddy/College/Internship Opportunities",
    Category.COLLEGE_PLACEMENT: "Mail-Buddy/College/Placements",
    Category.COLLEGE_NOTICE: "Mail-Buddy/College/Notices",
    Category.SUBSCRIPTION: "Mail-Buddy/Subscriptions",
    Category.JOB_RELATED: "Mail-Buddy/Career & Work/Job Related",
    Category.INTERNSHIP: "Mail-Buddy/Career & Work/Internship",
    Category.SHOPPING_PROMOTION: "Mail-Buddy/Shopping/Promotions",
    Category.SHOPPING_ORDER_UPDATE: "Mail-Buddy/Shopping/Order Updates",
    Category.SOCIAL: "Mail-Buddy/Social",
    Category.DIRECT_PERSONAL: "Mail-Buddy/Direct/Personal",
    Category.TRAVEL: "Mail-Buddy/Travel",
    Category.HEALTH_MEDICAL: "Mail-Buddy/Health & Medical",
    Category.EVENTS_TICKETS: "Mail-Buddy/Events & Tickets",
    Category.GOVERNMENT_LEGAL: "Mail-Buddy/Government & Legal",
    Category.OTHER: "Mail-Buddy/Other",
}
LEGACY_NEEDS_REVIEW_LABEL = "Mail-Buddy/Needs Review"


class DecisionSource(StrEnum):
    USER_RULE = "user_rule"
    RULE = "rule"
    LLAMA = "llama"
    PERSONALIZED_ENSEMBLE = "personalized_ensemble"
    MANUAL = "manual"
    FALLBACK = "fallback"


class ReasonCode(StrEnum):
    USER_OVERRIDE = "USER_OVERRIDE"
    OFFICIAL_COLLEGE_SENDER = "OFFICIAL_COLLEGE_SENDER"
    COLLEGE_SENDER = "COLLEGE_SENDER"
    PLACEMENT_INTENT = "PLACEMENT_INTENT"
    INTERNSHIP_INTENT = "INTERNSHIP_INTENT"
    NOTICE_INTENT = "NOTICE_INTENT"
    DEADLINE_PRESENT = "DEADLINE_PRESENT"
    PASSWORD_RESET_INTENT = "PASSWORD_RESET_INTENT"  # noqa: S105
    OTP_INTENT = "OTP_INTENT"
    ACCOUNT_ALERT_INTENT = "ACCOUNT_ALERT_INTENT"
    TRANSACTION_INTENT = "TRANSACTION_INTENT"
    ORDER_LIFECYCLE_INTENT = "ORDER_LIFECYCLE_INTENT"
    PROMOTIONAL_INTENT = "PROMOTIONAL_INTENT"
    SUBSCRIPTION_HEADERS = "SUBSCRIPTION_HEADERS"
    SOCIAL_PLATFORM_INTENT = "SOCIAL_PLATFORM_INTENT"
    PROFESSIONAL_INTENT = "PROFESSIONAL_INTENT"
    TWO_WAY_HISTORY = "TWO_WAY_HISTORY"
    BULK_MAIL = "BULK_MAIL"
    AUTHENTICATION_FAILURE = "AUTHENTICATION_FAILURE"
    CONFLICTING_SIGNALS = "CONFLICTING_SIGNALS"
    ATTACHMENT_SKIPPED = "ATTACHMENT_SKIPPED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_INVALID = "MODEL_INVALID"
    PERSONALIZED_AGREEMENT = "PERSONALIZED_AGREEMENT"
    PERSONALIZED_DISAGREEMENT = "PERSONALIZED_DISAGREEMENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    OTHER_INTENT = "OTHER_INTENT"


class MessageFlag(StrEnum):
    IMPORTANT = "important"
    ACTION_REQUIRED = "action_required"
    SENSITIVE = "sensitive"
    PROMOTIONAL = "promotional"
    BULK = "bulk"
    CONTAINS_OTP = "contains_otp"
    SUSPICIOUS = "suspicious"
    ATTACHMENT_SKIPPED = "attachment_skipped"


class MessageOrigin(StrEnum):
    LIVE = "live"
    BACKFILL = "backfill"


class MessageState(StrEnum):
    QUEUED = "queued"
    CLASSIFYING = "classifying"
    STAGED = "staged"
    NEEDS_REVIEW = "needs_review"
    READY_TO_APPLY = "ready_to_apply"
    APPLIED = "applied"
    ERROR = "error"
    GONE = "gone"


class JobKind(StrEnum):
    CLASSIFY = "classify"
    APPLY = "apply"
    UNDO = "undo"


class RuleKind(StrEnum):
    SENDER = "sender"
    SENDER_DOMAIN = "sender_domain"
    SIMILAR = "similar"


class BackfillState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    SCAN_COMPLETE = "scan_complete"
    COMPLETE = "complete"
    ERROR = "error"


class EmailMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str
    internal_date: int = 0
    sender: str = ""
    sender_domain: str = ""
    subject: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    label_ids: set[str] = Field(default_factory=set)
    snippet: str = ""
    had_inbox: bool = False
    two_way_history: bool = False


class ParsedEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: EmailMetadata
    body_text: str = ""
    attachment_text: str = ""
    attachment_skipped: bool = False

    @property
    def combined_text(self) -> str:
        sections = [self.metadata.subject, self.body_text, self.attachment_text]
        return "\n\n".join(section for section in sections if section).strip()


class ClassificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    taxonomy_version: str = TAXONOMY_VERSION
    primary_category: Category
    alternate_category: Category | None = None
    source: DecisionSource
    review_required: bool = False
    reason_codes: list[ReasonCode] = Field(default_factory=list, max_length=12)
    flags: list[MessageFlag] = Field(default_factory=list, max_length=12)
    model: str | None = None

    @model_validator(mode="after")
    def alternate_must_differ(self) -> ClassificationResult:
        if self.alternate_category == self.primary_category:
            self.alternate_category = None
        if self.source == DecisionSource.LLAMA and not self.model:
            self.model = MODEL_NAME
        return self


class DashboardStatus(BaseModel):
    connected: bool
    account_email: str | None = None
    account_status: str = "disconnected"
    last_sync_at: str | None = None
    history_id: str | None = None
    model_available: bool = False
    model_name: str = MODEL_NAME
    queue_depth: int = 0
    review_count: int = 0
    staged_count: int = 0
    applied_count: int = 0
    backfill_status: BackfillState = BackfillState.IDLE
    backfill_scanned: int = 0
    backfill_staged: int = 0
    disk_free_bytes: int = 0


NonEmptyString = Annotated[str, Field(min_length=1, max_length=512)]
