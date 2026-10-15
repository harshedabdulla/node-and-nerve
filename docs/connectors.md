# Writing a connector

Company Brain has no built-in integrations yet. A connector is any process that reads an upstream system it is authorized to read and calls the write API. Because every write is an idempotent MERGE on a stable id, a connector can be a simple scheduled job that replays recent changes.

General rules:

- Derive ids deterministically from upstream keys, and set `source_system` and `source_record_id`.
- Register the upstream record itself as a `Source` (with a `uri` back to it), and pass it as `source_id` on relationships it asserts. This is what makes answers traceable.
- Send the upstream modification time as `observed_at` / `occurred_at`. Older writes are then ignored automatically, so out-of-order delivery is safe.
- Map upstream changes to events where one exists (`ticket_status_changed`, `ownership_changed`, …) rather than overwriting entities.
- Only ingest data you are permitted to process. Do not scrape login-protected pages.

## Jira / GitHub issues

| Upstream | Company Brain |
| --- | --- |
| Issue `ATLAS-142` | `ticket:atlas-142` (`status`, `due_date`, `priority`), plus `source:jira-atlas-142` (`ticket_record`, `uri` = issue URL) supporting it |
| Epic / project | `project:<key>` and `PART_OF` |
| Component / label | `component:<slug>` and `IN_COMPONENT` |
| Assignee | `ownership_changed` event |
| "is blocked by" link | `BLOCKED_BY` with `source_id` = the issue's source |
| Status transition | `ticket_status_changed` with the transition time |

## Slack and other conversations

Store a thread as one `Source` (`conversation`) with a permalink and a short excerpt. Link it with `supports` only when the thread is genuinely evidence (an incident channel), and `mentions` for references detected by ticket/decision keys in the text (`ATLAS-142`, `DEC-004`). Prefer exact key matching over fuzzy entity extraction.

## Documents (postmortems, ADRs, plans)

One `Source` per document with `uri`, `published_at` and authors (`AUTHORED_BY`). An ADR usually *supports* exactly one decision and may record `SUPERSEDES` (send `decision_superseded`). A postmortem *supports* its incident. A document being deleted or retracted upstream becomes `source_withdrawn`, which keeps the history but stops it counting as evidence.

## CRM and project-management systems

Accounts or deals can be modelled with the existing kinds only loosely; adding a kind (e.g. `Customer`) is a small change to `schema.py` (enum, allowed properties, relationship rules). Keep new relationship types few and explicit.

## Example: one Jira issue

```sh
curl -X POST $BASE/v1/companies/acme/sources -H 'Content-Type: application/json' -d '{
  "id": "source:jira-atlas-142", "name": "ATLAS-142", "source_type": "ticket_record",
  "uri": "https://jira.example.com/browse/ATLAS-142", "source_system": "jira", "source_record_id": "ATLAS-142",
  "supports": ["ticket:atlas-142"], "observed_at": "2026-10-16T08:00:00Z"}'

curl -X POST $BASE/v1/companies/acme/relationships -H 'Content-Type: application/json' -d '{
  "type": "BLOCKED_BY", "from_id": "ticket:atlas-142", "to_id": "ticket:int-88",
  "source_id": "source:jira-atlas-142"}'
```

(The ticket entities must exist first.)
