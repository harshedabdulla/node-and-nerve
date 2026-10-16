# Graph model

The source of truth is [`schema.py`](../src/company_brain/schema.py). `GET /v1/schema` returns the same rules as JSON.

## Nodes

Every node has two labels: `Entity` (for the unique id constraint and generic lookups) and one kind label.

| Kind | Label | Example id | Kind-specific properties |
| --- | --- | --- | --- |
| person | `Person` | `person:dev-patel` | `title`, `email_alias` |
| team | `Team` | `team:platform` | |
| project | `Project` | `project:atlas` | `status`, `due_date` |
| ticket | `Ticket` | `ticket:atlas-142` | `status`, `due_date`, `priority` |
| incident | `Incident` | `incident:inc-009` | `status`, `severity`, `occurred_at`, `resolved_at` |
| decision | `Decision` | `decision:dec-004` | `status`, `decided_at` |
| component | `Component` | `component:billing-gateway` | |
| source | `Source` | `source:postmortem-009` | `uri`, `source_type`, `excerpt`, `status`, `published_at` |
| customer | `Customer` | `customer:cust-001` | `city` |
| supplier | `Supplier` | `supplier:sup-001` | `city` |
| invoice | `Invoice` | `invoice:inv-1001` | `status`, `issue_date`, `due_date`, `amount_paise`, `currency`, `expected_payment_date` |
| payment | `Payment` | `payment:pay-501` | `status`, `received_on`, `amount_paise`, `currency`, `method`, `reference` |
| salesorder | `SalesOrder` | `salesorder:so-105` | `status`, `order_date`, `required_by`, `amount_paise`, `currency` |
| purchaseorder | `PurchaseOrder` | `purchaseorder:po-301` | `status`, `order_date`, `expected_delivery`, `amount_paise`, `currency` |
| commitment | `Commitment` | `commitment:com-201` | `status`, `commitment_type`, `due_date`, `amount_paise`, `currency` |

| contract | `Contract` | `contract:ct-104` | `status`, `contract_date`, `expected_completion`, `body_count`, `body_type`, `amount_paise`, `currency` |
| job | `Job` | `job:j-104a` | `status`, `body_type`, `chassis_ref`, `started_on`, `completed_on` |
| worker | `Worker` | `worker:w-01` | `role`, `daily_rate_paise` |
| purchase | `Purchase` | `purchase:pur-2047` | `status`, `paid_status`, `purchase_date`, `bill_ref`, `material_type`, `item`, `quantity`, `unit`, `unit_price_paise`, `amount_paise` |
| wage | `Wage` | `wage:wg-401` | `status`, `paid_status`, `work_date`, `period_end`, `days`, `work_type`, `amount_paise` |
| machinerycost | `MachineryCost` | `machinerycost:mc-401` | `status`, `paid_status`, `cost_date`, `cost_type`, `machine`, `quantity`, `unit`, `amount_paise` |
| expense | `Expense` | `expense:ex-401` | `status`, `paid_status`, `expense_date`, `category`, `amount_paise` |

Customer to commitment belong to [Cashflow Radar](cashflow-radar.md); contract to expense belong to the [contract cost investigation](contract-costing.md) (which also uses customer, supplier and payment). Money is integer paise (INR only); date properties are `YYYY-MM-DD` strings.

Common properties on every node: `id`, `kind`, `name`, `description`, `source_system`, `source_record_id`, `created_at`, `updated_at`.

Allowed status values:

- project: `planned`, `active`, `at_risk`, `done`, `cancelled`
- ticket: `open`, `in_progress`, `blocked`, `done`, `cancelled`
- incident: `open`, `mitigated`, `resolved`
- decision: `proposed`, `accepted`, `rejected`
- source: `active`, `withdrawn`
- invoice: `open`, `partially_paid`, `paid`, `disputed`, `written_off`, `cancelled`
- payment: `cleared`, `pending`, `bounced`
- salesorder: `open`, `awaiting_stock`, `dispatched`, `delivered`, `cancelled`
- purchaseorder: `draft`, `placed`, `partially_received`, `received`, `cancelled`
- commitment: `open`, `fulfilled`, `cancelled`

Source types: `document`, `conversation`, `ticket_record`, `postmortem`, `meeting_notes`, `email`, `accounting_record`, `bank_statement`, `message`, `spreadsheet`, `other`.

### Identifiers

`<kind>:<slug>`, slug `[a-z0-9][a-z0-9._-]{0,99}`. The kind prefix decides the label, so an id can never be written with two different kinds. A connector should derive ids deterministically from the upstream record (e.g. Jira key `ATLAS-142` → `ticket:atlas-142`) and also set `source_system` / `source_record_id`, so re-imports update the same node.

### Constraints and indexes

Per company graph, created idempotently on first write:

```
CREATE INDEX FOR (n:Entity) ON (n.id)
CREATE INDEX FOR (n:Entity) ON (n.kind)
GRAPH.CONSTRAINT CREATE <graph> UNIQUE NODE Entity PROPERTIES 1 id
```

## Relationships

| Type | From | To | Meaning |
| --- | --- | --- | --- |
| `MEMBER_OF` | person | team | |
| `OWNS` | person, team | project, ticket, component | Current owner when `valid_to` is absent |
| `PART_OF` | ticket, component | project | |
| `IN_COMPONENT` | ticket, incident | component | |
| `BLOCKED_BY` | ticket, project | ticket, component, incident | |
| `AFFECTS` | incident, decision | ticket, project, component | |
| `RESOLVED` | person | incident | Basis for expertise |
| `DECIDED_BY` | decision | person | Decision owner |
| `SUPERSEDES` | decision, commitment | same kind | Newer → older |
| `SUPPORTED_BY` | decision, incident, ticket, and every Cashflow Radar record | source | The source is evidence for the record |
| `MENTIONS` | source | any non-source | The source refers to it, but is not evidence for it |
| `CONTRADICTS` | source | decision, incident, invoice, payment, commitment | The source disputes the record |
| `AUTHORED_BY` | source | person | |
| `BILLED_TO` | invoice | customer | |
| `RECEIVED_FROM` | payment | customer | |
| `ALLOCATED_TO` | payment | invoice | `amount_paise` applied to that invoice |
| `PLACED_BY` | salesorder | customer | |
| `PLACED_WITH` | purchaseorder | supplier | |
| `SOURCED_FROM` | salesorder | purchaseorder | The sales order waits on this stock |
| `FOR_ORDER` | invoice, commitment | salesorder, purchaseorder | |
| `OWED_TO` | commitment | supplier, customer | |
| `DEPENDS_ON` | commitment | invoice | A recorded dependency on collecting the invoice; never inferred. `source_id` says who documented it |
| `CHARGED_TO` | purchase, wage, machinerycost, expense | job, contract | `amount_paise`: the part of the entry borne there. The only way a cost reaches a contract |
| `NOTED_FOR` | purchase, wage, machinerycost, expense | job, contract | The book links the entry to it without an amount: a pending decision, never cost |
| `SUPPLIED_BY` | purchase, machinerycost, expense | supplier | |
| `EARNED_BY` | wage | worker | |
| `PAID_TO` | payment | supplier, worker | |

`PART_OF` also links job → contract, `PLACED_BY` contract → customer, and `ALLOCATED_TO` payment → purchase, machinerycost, expense or contract. `SUPPORTED_BY` and `CONTRADICTS` accept the cost entries.

Every edge has `created_at`, `updated_at`, `valid_from`, and may have:

- `source_id`: the source record that asserted this edge (e.g. the ticket record that says ATLAS-142 is blocked by INT-88).
- `valid_to`, `closed_by_event`: set when an ownership change replaces the edge. Closed edges are kept for history and ignored by "current" queries and path discovery.

## Provenance: support vs. mention

A postmortem that says "falling back to v1 is not an option under DEC-004" *mentions* DEC-004; the architecture review where DEC-004 was made *supports* it. Keeping these as different edge types lets the service answer "what is the evidence for this decision?" without treating every passing reference as evidence, while still using mentions to connect an incident to a relevant decision.

## Tenancy

Each company has its own graph: `company_brain__<company_id>` with `-` replaced by `_`. Company ids are validated (`^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$`, no underscores, so the mapping cannot collide) before a graph name is derived.

Why separate graphs instead of one shared graph with a `company_id` property:

- Isolation does not depend on every query remembering a filter. A query physically cannot see another company's nodes.
- Ids only need to be unique within a company.
- Deleting a company is one `GRAPH.DELETE`.

Costs: cross-company analytics would need application-level fan-out, and very many tenants mean very many graphs (each with its own indexes). Neither matters for this service's scope.
