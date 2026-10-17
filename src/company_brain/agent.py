"""A small tool-calling agent that explains contract investigations.

The model (any OpenAI-compatible endpoint; OpenRouter by default) never sees the
database and never computes figures. It can only call the tools below, which run the
deterministic Node & Nerve investigations in-process, and then explain their results.
The system prompt requires it to cite sources, keep "provisional", and report unknowns
and decisions instead of resolving them.

Standard library only (``urllib``), so the engine gains no dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from company_brain import queries as q
from company_brain.cashflow import inr
from company_brain.contract_cost import (
    ContractCostInvestigator,
    ContractCostRequest,
    ContractCostResponse,
)
from company_brain.ingest import EntityNotFound

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_ROUNDS = 6

SYSTEM_PROMPT = """\
You answer questions about a small manufacturer's contracts. You have tools that call
Node & Nerve, which investigates the business's recorded books in a graph database.

Rules:

1. Every amount, date, status and allocation you state must come from a tool result.
   Never calculate, estimate or fill in a figure yourself.
2. Call any figure about profit "provisional profit based on recorded direct costs". Never
   call it profit, final profit or net profit.
3. Cite the book pages behind each point using the short labels in the tool result's
   `cite` fields, in square brackets, e.g. [MPB p.14]. Never write raw ids such as
   "source:mpb-p14", and do not comment on citations that are absent.
4. Report decisions required, unknown and inconsistent findings, and missing information
   plainly. Do not resolve them yourself:
   - do not split a shared cost between contracts,
   - do not assign unassigned labour to a job,
   - do not assume an unconfirmed or unrecorded payment was made,
   - do not decide which bill an unapplied payment settles.
5. Keep payments separate from costs: supplier payments and customer receipts settle
   amounts owed; they are never extra cost or extra revenue.
6. If a tool returns an error, say so. Do not answer from memory.
7. End with the result's warnings (for example that the contract is still in production
   and that the figures are not statutory accounts).
8. Be brief: a business owner reads this on a phone. Format:
   - a one-line direct answer first,
   - then at most four short sections, each a line in **bold** followed by "- " bullets,
   - rupee amounts exactly as given, record references such as PUR-2058 as given,
   - no tables, no headings with #, no emoji, no closing offers of further help.
   A link to the evidence graph is added after your answer automatically; do not
   mention it.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "investigate_contract",
            "description": "Recorded direct cost (by category, by job and per book entry), "
            "provisional profit, money owed, and missing or conflicting information for one "
            "manufacturing contract, with sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contract": {
                        "type": "string",
                        "description": "Contract reference, e.g. CT-104.",
                    },
                    "as_of": {"type": "string", "description": "Reporting date YYYY-MM-DD."},
                },
                "required": ["contract"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contracts",
            "description": "All contracts with value, recorded cost, provisional profit, amount "
            "still to receive, and which have incomplete cost records.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_unallocated_costs",
            "description": "Recorded costs not charged to any contract (overheads, unassigned "
            "labour, costs awaiting a split) and supplier payments not applied to any bill.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_changes_since",
            "description": "Cost entries, payments and allocations added or changed after a "
            "timestamp.",
            "parameters": {
                "type": "object",
                "properties": {"since": {"type": "string", "description": "ISO timestamp."}},
                "required": ["since"],
            },
        },
    },
]

Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class AgentError(Exception):
    """The model endpoint failed. Messages never include the API key."""


def http_post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: float
) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed https URLs only
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            result: dict[str, Any] = json.loads(response.read())
            return result
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise AgentError(f"model endpoint returned HTTP {exc.code}: {detail}") from None
    except OSError as exc:  # URLError, timeouts, dropped or reset connections
        raise AgentError(f"model endpoint unreachable: {type(exc).__name__}: {exc}") from None
    except ValueError:
        raise AgentError("model endpoint returned an unreadable reply") from None


# ---------------------------------------------------------------- tool results


def compact_contract(r: ContractCostResponse) -> dict[str, Any]:
    """The parts of an investigation an agent needs, without graph paths or raw ids."""
    label = {s.id: str(s.source_record_id or s.id) for s in r.sources}

    def cite(ids: list[str] | set[str]) -> list[str]:
        return sorted({label.get(i, i) for i in ids})

    record_sources: dict[str, set[str]] = {}
    by_job: dict[str, dict[str, int]] = {}
    for line in r.cost_lines:
        record_sources.setdefault(line.record_id, set()).update(line.source_ids)
        job = by_job.setdefault(line.charged_to_ref or line.charged_to, defaultdict(int))
        job[line.category] += line.charged_paise or 0
        job["total"] += line.charged_paise or 0
    return {
        "contract": r.contract.model_dump(exclude={"jobs"})
        | {"jobs": [f"{j.ref} ({j.status})" for j in r.contract.jobs]},
        "as_of": r.as_of.isoformat(),
        "cost_breakdown_inr": {
            "materials": inr(r.cost_breakdown.materials_paise),
            "labour": inr(r.cost_breakdown.labour_paise),
            "machinery": inr(r.cost_breakdown.machinery_paise),
            "other_direct": inr(r.cost_breakdown.other_paise),
            "total_recorded_direct_cost": inr(r.cost_breakdown.total_paise),
            "book_entries": r.cost_breakdown.entries,
            "charges": r.cost_breakdown.charges,
            "excluded_void": r.cost_breakdown.excluded_void,
        },
        # Totals per job (or the contract itself), summed by Node & Nerve.
        "cost_by_job_inr": {
            ref: {k: inr(v) for k, v in sorted(totals.items())} for ref, totals in by_job.items()
        },
        "cost_entries": [
            {
                "entry": ln.ref,
                "category": ln.category,
                "charged_to": ln.charged_to_ref or ln.charged_to,
                "amount_charged_here": inr(ln.charged_paise),
                "entry_total": inr(ln.record_amount_paise),
                "description": ln.description,
                "from": ln.counterparty,
                "cite": cite(ln.source_ids),
            }
            for ln in r.cost_lines
        ],
        "profitability_inr": {
            "label": r.profitability.label,
            "contract_value": inr(r.profitability.contract_value_paise),
            "recorded_direct_cost": inr(r.profitability.recorded_direct_cost_paise),
            "provisional_profit": inr(r.profitability.provisional_profit_paise),
            "provisional_margin_percent": r.profitability.provisional_margin_percent,
            "unsplit_costs_noted_for_it": inr(r.profitability.pending_allocation_paise),
            "provisional_profit_if_all_unsplit_charged_here": inr(
                r.profitability.profit_if_all_pending_charged_here_paise
            ),
        },
        "customer_account_inr": {
            "received": inr(r.customer_account.received_paise),
            "still_to_receive": inr(r.customer_account.outstanding_paise),
        },
        "supplier_bills_with_balance": [
            {
                "bill": b.ref,
                "supplier": b.supplier,
                "outstanding": inr(b.outstanding_paise),
                "book_says": b.book_paid_status,
                "reconciliation": b.reconciliation,
                "shared_with": b.shared_with,
                "cite": cite(record_sources.get(b.record_id, set())),
            }
            for b in r.supplier_bills
            if b.outstanding_paise
        ],
        "findings": [
            {
                "kind": f.kind,
                "category": f.category,
                "statement": f.statement,
                "cite": cite(f.source_ids),
            }
            for f in r.findings
        ],
        "decisions_required": r.decisions_required,
        "missing_information": r.missing_information,
        "warnings": r.warnings,
        "calculation_rules": r.calculation_rules,
        "sources": {label[s.id]: s.name for s in r.sources},
        "graph_queries_run": r.queries_executed,
    }


@dataclass
class ToolBox:
    """Executes tool calls against the deterministic investigator."""

    investigator: ContractCostInvestigator
    as_of: date | None = None
    contracts: list[str] = field(default_factory=list)

    def _labels(self, ids: list[str]) -> dict[str, str]:
        if not ids:
            return {}
        rows = self.investigator.store.run(self.investigator.company_id, q.MF_LABELS, {"ids": ids})
        return {r["id"]: str(r["ref"]) for r in rows}

    def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "investigate_contract":
                as_of = arguments.get("as_of")
                response = self.investigator.investigate(
                    ContractCostRequest(
                        contract=str(arguments["contract"]),
                        as_of=date.fromisoformat(as_of) if as_of else self.as_of,
                    )
                )
                self.contracts.append(response.contract.ref)
                return compact_contract(response)
            if name == "list_contracts":
                return self.investigator.overview().model_dump(mode="json")
            if name == "list_unallocated_costs":
                unallocated = self.investigator.unallocated()
                labels = self._labels(sorted({i for x in unallocated.items for i in x.source_ids}))
                result = unallocated.model_dump(mode="json")
                for item, raw in zip(result["items"], unallocated.items, strict=True):
                    item.pop("source_ids")
                    item["cite"] = sorted(labels.get(i, i) for i in raw.source_ids)
                return result
            if name == "list_changes_since":
                return {"changes": self.investigator.changed_since(str(arguments["since"]))}
            return {"error": f"unknown tool {name!r}"}
        # GraphError propagates: with the records unreachable there is nothing for the model
        # to word, and the caller says so plainly.
        except EntityNotFound as exc:
            return {"error": f"not found: {', '.join(exc.missing)}"}
        except (ValueError, KeyError) as exc:
            return {"error": f"invalid arguments: {exc}"}


# ---------------------------------------------------------------- the loop


@dataclass
class AgentAnswer:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    contracts: list[str] = field(default_factory=list)  # contracts investigated


class Tools(Protocol):
    """What the loop needs from a tool box: run a tool, and the contracts it looked at."""

    contracts: list[str]
    as_of: date | None

    def run(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ContractAgent:
    """A small tool-calling loop. Defaults are the contract tools; other tool boxes and
    prompts (for example the workspace agent) reuse the same loop."""

    tools: Tools
    api_key: str
    model: str
    url: str = OPENROUTER_URL
    transport: Transport = http_post_json
    timeout_s: float = 90.0
    system_prompt: str = SYSTEM_PROMPT
    tool_specs: list[dict[str, Any]] = field(default_factory=lambda: TOOLS)

    def ask(self, question: str) -> AgentAnswer:
        if not self.api_key:
            raise AgentError("no model API key configured (OPEN_ROUTER_API_KEY)")
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]
        calls: list[dict[str, Any]] = []
        self.tools.contracts = []
        for _ in range(MAX_TOOL_ROUNDS):
            reply = self._complete(messages)
            message = reply["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                text = message.get("content") or ""
                if reply["choices"][0].get("finish_reason") == "length":
                    text += "\n\n(Answer cut short. Ask a narrower question.)"
                return AgentAnswer(
                    text=text,
                    tool_calls=calls,
                    model=self.model,
                    contracts=list(dict.fromkeys(self.tools.contracts)),
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                name = call["function"]["name"]
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                result = self.tools.run(name, arguments)
                calls.append({"tool": name, "arguments": arguments, "error": result.get("error")})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        raise AgentError(f"no answer after {MAX_TOOL_ROUNDS} tool rounds")

    def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": self.tool_specs,
            "temperature": 0,
            "max_tokens": 2500,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "X-Title": "Node & Nerve",
        }
        reply = self.transport(self.url, headers, body, self.timeout_s)
        if "error" in reply:
            raise AgentError(f"model endpoint error: {str(reply['error'])[:300]}")
        if not reply.get("choices"):
            raise AgentError("model endpoint returned no choices")
        return reply
