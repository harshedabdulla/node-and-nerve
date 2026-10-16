"""The graph data model: entity kinds, allowed relationships, and identifier rules.

This module is the single source of truth for what may be written to the graph.
Labels and relationship types can never come from caller input directly: FalkorDB
does not accept them as query parameters, so every label/type that appears in a
query is taken from the enums below.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Kind(StrEnum):
    PERSON = "person"
    TEAM = "team"
    PROJECT = "project"
    TICKET = "ticket"
    INCIDENT = "incident"
    DECISION = "decision"
    COMPONENT = "component"
    SOURCE = "source"
    # Cashflow Radar: business records of a small distributor.
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    INVOICE = "invoice"
    PAYMENT = "payment"
    SALES_ORDER = "salesorder"
    PURCHASE_ORDER = "purchaseorder"
    COMMITMENT = "commitment"
    # Contract costing: a small manufacturer's contracts, jobs and cost records.
    CONTRACT = "contract"
    JOB = "job"
    WORKER = "worker"
    PURCHASE = "purchase"
    WAGE = "wage"
    MACHINERY_COST = "machinerycost"
    EXPENSE = "expense"

    @property
    def label(self) -> str:
        return _LABELS.get(self.value, self.value.capitalize())


# Kind values cannot contain separators (they are id prefixes), so multi-word
# kinds get an explicit label.
_LABELS = {
    "salesorder": "SalesOrder",
    "purchaseorder": "PurchaseOrder",
    "machinerycost": "MachineryCost",
}


class Rel(StrEnum):
    MEMBER_OF = "MEMBER_OF"
    OWNS = "OWNS"
    PART_OF = "PART_OF"
    IN_COMPONENT = "IN_COMPONENT"
    BLOCKED_BY = "BLOCKED_BY"
    AFFECTS = "AFFECTS"
    RESOLVED = "RESOLVED"
    DECIDED_BY = "DECIDED_BY"
    SUPERSEDES = "SUPERSEDES"
    SUPPORTED_BY = "SUPPORTED_BY"
    MENTIONS = "MENTIONS"
    CONTRADICTS = "CONTRADICTS"
    AUTHORED_BY = "AUTHORED_BY"
    # Cashflow Radar
    BILLED_TO = "BILLED_TO"
    RECEIVED_FROM = "RECEIVED_FROM"
    ALLOCATED_TO = "ALLOCATED_TO"
    PLACED_BY = "PLACED_BY"
    PLACED_WITH = "PLACED_WITH"
    SOURCED_FROM = "SOURCED_FROM"
    FOR_ORDER = "FOR_ORDER"
    OWED_TO = "OWED_TO"
    DEPENDS_ON = "DEPENDS_ON"
    # Contract costing
    CHARGED_TO = "CHARGED_TO"
    NOTED_FOR = "NOTED_FOR"
    SUPPLIED_BY = "SUPPLIED_BY"
    EARNED_BY = "EARNED_BY"
    PAID_TO = "PAID_TO"


K = Kind
_ANY: frozenset[Kind] = frozenset(Kind)
# Business records that can carry their own evidence.
# Cost records of a manufacturer: each is one line in one of its books.
COST_KINDS = frozenset({K.PURCHASE, K.WAGE, K.MACHINERY_COST, K.EXPENSE})
_RECORDS = (
    frozenset(
        {
            K.INVOICE,
            K.PAYMENT,
            K.SALES_ORDER,
            K.PURCHASE_ORDER,
            K.COMMITMENT,
            K.CUSTOMER,
            K.SUPPLIER,
            K.CONTRACT,
            K.JOB,
            K.WORKER,
        }
    )
    | COST_KINDS
)

# relationship -> (allowed start kinds, allowed end kinds)
RELATIONSHIP_RULES: dict[Rel, tuple[frozenset[Kind], frozenset[Kind]]] = {
    Rel.MEMBER_OF: (frozenset({K.PERSON}), frozenset({K.TEAM})),
    Rel.OWNS: (
        frozenset({K.PERSON, K.TEAM}),
        frozenset({K.PROJECT, K.TICKET, K.COMPONENT}),
    ),
    Rel.PART_OF: (
        frozenset({K.TICKET, K.COMPONENT, K.JOB}),
        frozenset({K.PROJECT, K.CONTRACT}),
    ),
    Rel.IN_COMPONENT: (frozenset({K.TICKET, K.INCIDENT}), frozenset({K.COMPONENT})),
    Rel.BLOCKED_BY: (
        frozenset({K.TICKET, K.PROJECT}),
        frozenset({K.TICKET, K.COMPONENT, K.INCIDENT}),
    ),
    Rel.AFFECTS: (
        frozenset({K.INCIDENT, K.DECISION}),
        frozenset({K.TICKET, K.PROJECT, K.COMPONENT}),
    ),
    Rel.RESOLVED: (frozenset({K.PERSON}), frozenset({K.INCIDENT})),
    Rel.DECIDED_BY: (frozenset({K.DECISION}), frozenset({K.PERSON})),
    # Same-kind only (decision -> decision, commitment -> commitment); see check_relationship.
    Rel.SUPERSEDES: (frozenset({K.DECISION, K.COMMITMENT}), frozenset({K.DECISION, K.COMMITMENT})),
    Rel.SUPPORTED_BY: (
        frozenset({K.DECISION, K.INCIDENT, K.TICKET}) | _RECORDS,
        frozenset({K.SOURCE}),
    ),
    Rel.MENTIONS: (frozenset({K.SOURCE}), _ANY - {K.SOURCE}),
    Rel.CONTRADICTS: (
        frozenset({K.SOURCE}),
        frozenset({K.DECISION, K.INCIDENT, K.INVOICE, K.PAYMENT, K.COMMITMENT}) | COST_KINDS,
    ),
    Rel.AUTHORED_BY: (frozenset({K.SOURCE}), frozenset({K.PERSON})),
    # Cashflow Radar
    Rel.BILLED_TO: (frozenset({K.INVOICE}), frozenset({K.CUSTOMER})),
    Rel.RECEIVED_FROM: (frozenset({K.PAYMENT}), frozenset({K.CUSTOMER})),
    # Carries amount_paise: how much of the payment settles the invoice, bill or
    # contract. A payment settles a cost; it is never a second cost.
    Rel.ALLOCATED_TO: (
        frozenset({K.PAYMENT}),
        frozenset({K.INVOICE, K.CONTRACT, K.PURCHASE, K.MACHINERY_COST, K.EXPENSE}),
    ),
    Rel.PLACED_BY: (frozenset({K.SALES_ORDER, K.CONTRACT}), frozenset({K.CUSTOMER})),
    Rel.PLACED_WITH: (frozenset({K.PURCHASE_ORDER}), frozenset({K.SUPPLIER})),
    # The sales order waits on stock from this purchase order.
    Rel.SOURCED_FROM: (frozenset({K.SALES_ORDER}), frozenset({K.PURCHASE_ORDER})),
    Rel.FOR_ORDER: (
        frozenset({K.INVOICE, K.COMMITMENT}),
        frozenset({K.SALES_ORDER, K.PURCHASE_ORDER}),
    ),
    Rel.OWED_TO: (frozenset({K.COMMITMENT}), frozenset({K.SUPPLIER, K.CUSTOMER})),
    # A documented dependency: the commitment relies on collecting this invoice.
    # Never inferred; it must be recorded, ideally with the source that says so.
    Rel.DEPENDS_ON: (frozenset({K.COMMITMENT}), frozenset({K.INVOICE})),
    # Contract costing. PART_OF links a job to its contract (see above).
    # CHARGED_TO carries amount_paise: the part of the cost record borne by that
    # job or contract. It is the only way a cost reaches a contract.
    Rel.CHARGED_TO: (COST_KINDS, frozenset({K.JOB, K.CONTRACT})),
    # The book associates a cost with a job or contract but records no amount:
    # an allocation decision a person still has to make. Never counted as cost.
    Rel.NOTED_FOR: (COST_KINDS, frozenset({K.JOB, K.CONTRACT})),
    Rel.SUPPLIED_BY: (
        frozenset({K.PURCHASE, K.MACHINERY_COST, K.EXPENSE}),
        frozenset({K.SUPPLIER}),
    ),
    Rel.EARNED_BY: (frozenset({K.WAGE}), frozenset({K.WORKER})),
    Rel.PAID_TO: (frozenset({K.PAYMENT}), frozenset({K.SUPPLIER, K.WORKER})),
}

# Kind-specific properties a caller may set. Everything else is rejected.
COMMON_PROPERTIES = frozenset({"name", "description", "source_system", "source_record_id"})
KIND_PROPERTIES: dict[Kind, frozenset[str]] = {
    K.PERSON: frozenset({"title", "email_alias"}),
    K.TEAM: frozenset(),
    K.PROJECT: frozenset({"status", "due_date"}),
    K.TICKET: frozenset({"status", "due_date", "priority"}),
    K.INCIDENT: frozenset({"status", "severity", "occurred_at", "resolved_at"}),
    K.DECISION: frozenset({"status", "decided_at"}),
    K.COMPONENT: frozenset(),
    K.SOURCE: frozenset({"uri", "source_type", "excerpt", "status", "published_at"}),
    K.CUSTOMER: frozenset({"city"}),
    K.SUPPLIER: frozenset({"city"}),
    K.INVOICE: frozenset(
        {"status", "issue_date", "due_date", "amount_paise", "currency", "expected_payment_date"}
    ),
    K.PAYMENT: frozenset(
        {"status", "received_on", "paid_on", "amount_paise", "currency", "method", "reference"}
    ),
    K.SALES_ORDER: frozenset({"status", "order_date", "required_by", "amount_paise", "currency"}),
    K.PURCHASE_ORDER: frozenset(
        {"status", "order_date", "expected_delivery", "amount_paise", "currency"}
    ),
    K.COMMITMENT: frozenset({"status", "commitment_type", "due_date", "amount_paise", "currency"}),
    K.CONTRACT: frozenset(
        {
            "status",
            "contract_date",
            "expected_completion",
            "body_count",
            "body_type",
            "amount_paise",
            "currency",
        }
    ),
    K.JOB: frozenset({"status", "body_type", "chassis_ref", "started_on", "completed_on"}),
    K.WORKER: frozenset({"role", "daily_rate_paise"}),
    K.PURCHASE: frozenset(
        {
            "status",
            "paid_status",
            "purchase_date",
            "bill_ref",
            "material_type",
            "item",
            "quantity",
            "unit",
            "unit_price_paise",
            "amount_paise",
            "currency",
        }
    ),
    K.WAGE: frozenset(
        {
            "status",
            "paid_status",
            "work_date",
            "period_end",
            "days",
            "work_type",
            "amount_paise",
            "currency",
        }
    ),
    K.MACHINERY_COST: frozenset(
        {
            "status",
            "paid_status",
            "cost_date",
            "cost_type",
            "machine",
            "quantity",
            "unit",
            "amount_paise",
            "currency",
        }
    ),
    K.EXPENSE: frozenset(
        {"status", "paid_status", "expense_date", "category", "amount_paise", "currency"}
    ),
}

# Properties holding ISO dates (YYYY-MM-DD). Stored as strings so they compare lexically.
DATE_PROPERTIES = frozenset(
    {
        "due_date",
        "issue_date",
        "received_on",
        "order_date",
        "required_by",
        "expected_delivery",
        "expected_payment_date",
        "paid_on",
        "contract_date",
        "expected_completion",
        "started_on",
        "completed_on",
        "purchase_date",
        "work_date",
        "period_end",
        "cost_date",
        "expense_date",
    }
)
# Money properties: integer paise.
MONEY_PROPERTIES = frozenset({"amount_paise", "unit_price_paise", "daily_rate_paise"})
# Money is stored as integer paise to avoid floating point sums in Cypher.
CURRENCIES = frozenset({"INR"})
COMMITMENT_TYPES = frozenset({"supplier_payment", "customer_delivery", "other"})
# What a manufacturer's book says about whether a cost was paid. This is the
# book's claim; supplier payments are recorded separately and reconciled.
PAID_STATUSES = frozenset({"paid", "unpaid", "partly_paid"})
COST_TYPES = frozenset({"rental", "fuel", "maintenance", "usage"})
EXPENSE_CATEGORIES = frozenset({"electricity", "transport", "consumables", "rent", "other"})
MATERIAL_TYPES = frozenset({"steel", "paint", "welding", "hardware", "other"})

STATUS_VALUES: dict[Kind, frozenset[str]] = {
    K.PROJECT: frozenset({"planned", "active", "at_risk", "done", "cancelled"}),
    K.TICKET: frozenset({"open", "in_progress", "blocked", "done", "cancelled"}),
    K.INCIDENT: frozenset({"open", "mitigated", "resolved"}),
    K.DECISION: frozenset({"proposed", "accepted", "rejected"}),
    K.SOURCE: frozenset({"active", "withdrawn"}),
    K.INVOICE: frozenset(
        {"open", "partially_paid", "paid", "disputed", "written_off", "cancelled"}
    ),
    K.PAYMENT: frozenset({"cleared", "pending", "bounced"}),
    K.SALES_ORDER: frozenset({"open", "awaiting_stock", "dispatched", "delivered", "cancelled"}),
    K.PURCHASE_ORDER: frozenset({"draft", "placed", "partially_received", "received", "cancelled"}),
    K.COMMITMENT: frozenset({"open", "fulfilled", "cancelled"}),
    K.CONTRACT: frozenset({"confirmed", "in_production", "completed", "delivered", "cancelled"}),
    K.JOB: frozenset({"planned", "fabrication", "painting", "completed", "delivered"}),
    # Cost records are either valid entries or voided ones (struck out in the book).
    K.PURCHASE: frozenset({"recorded", "void"}),
    K.WAGE: frozenset({"recorded", "void"}),
    K.MACHINERY_COST: frozenset({"recorded", "void"}),
    K.EXPENSE: frozenset({"recorded", "void"}),
}

SOURCE_TYPES = frozenset(
    {
        "document",
        "conversation",
        "ticket_record",
        "postmortem",
        "meeting_notes",
        "email",
        "accounting_record",
        "bank_statement",
        "message",
        "spreadsheet",
        "book_entry",
        "other",
    }
)

ENTITY_ID_RE = re.compile(r"^(?P<kind>[a-z]+):(?P<slug>[a-z0-9][a-z0-9._-]{0,99})$")
COMPANY_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
GRAPH_PREFIX = "company_brain__"


def kind_of(entity_id: str) -> Kind:
    """Return the kind encoded in a stable entity id such as ``ticket:atlas-142``."""
    match = ENTITY_ID_RE.fullmatch(entity_id)
    if not match:
        raise ValueError(
            f"invalid entity id {entity_id!r}: expected '<kind>:<slug>' "
            "with a lowercase slug of letters, digits, '.', '_' or '-'"
        )
    try:
        return Kind(match["kind"])
    except ValueError:
        raise ValueError(f"unknown entity kind {match['kind']!r}") from None


def graph_name(company_id: str) -> str:
    """Derive the FalkorDB graph name for a company. The only way a graph is selected."""
    if not COMPANY_ID_RE.fullmatch(company_id):
        raise ValueError(
            f"invalid company id {company_id!r}: use 1-48 lowercase letters, digits or '-'"
        )
    return GRAPH_PREFIX + company_id.replace("-", "_")


def check_relationship(rel: Rel, from_kind: Kind, to_kind: Kind) -> None:
    starts, ends = RELATIONSHIP_RULES[rel]
    if from_kind not in starts or to_kind not in ends:
        raise ValueError(
            f"{rel.value} is not allowed from {from_kind.value} to {to_kind.value}; allowed: "
            f"{sorted(k.value for k in starts)} -> {sorted(k.value for k in ends)}"
        )
    if rel is Rel.SUPERSEDES and from_kind is not to_kind:
        raise ValueError(
            "SUPERSEDES must connect two records of the same kind, "
            f"not {from_kind.value} -> {to_kind.value}"
        )
