import "server-only";

import { createHmac } from "node:crypto";

// Server-side access to the Node & Nerve API for the public demo. The API key and the
// trace-link secret never leave the server. Only the synthetic demo companies below can
// be read from here; real workspaces will go through authenticated, per-user access.

export const DEMO_COMPANIES = {
  manufacturing: "kaveri-body-works",
  engineering: "northstar",
} as const;
type DemoCompany = (typeof DEMO_COMPANIES)[keyof typeof DEMO_COMPANIES];

export const DEMO_AS_OF = "2026-10-16"; // reporting date of the synthetic records
const LINK_TTL_S = 7 * 24 * 3600;

export function apiBase(): string | null {
  return process.env.NN_API_URL?.replace(/\/$/, "") ?? null;
}

async function call<T>(company: DemoCompany, path: string, body: unknown): Promise<T> {
  const base = apiBase();
  const key = process.env.NN_API_KEY;
  if (!base || !key) throw new Error("the live API is not configured");
  const res = await fetch(`${base}/v1/companies/${company}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": key },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`the live API answered ${res.status}`);
  return (await res.json()) as T;
}

export function inr(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "not recorded";
  return `₹${Math.round(paise / 100).toLocaleString("en-IN")}`;
}

// Same signature as company_brain/trace.py: HMAC-SHA256 over company|contract|expiry.
export function traceLink(company: string, contract: string, focus: string[] = []): string | null {
  const base = apiBase();
  const secret = process.env.NN_TRACE_SECRET;
  if (!base || !secret) return null;
  const exp = Math.floor(Date.now() / 1000) + LINK_TTL_S;
  const sig = createHmac("sha256", secret)
    .update(`${company}|${contract.toLowerCase()}|${exp}`)
    .digest("hex")
    .slice(0, 32);
  const qs = new URLSearchParams({ exp: String(exp), sig, as_of: DEMO_AS_OF });
  if (focus.length) qs.set("focus", [...new Set(focus)].sort().join(","));
  return `${base}/trace/${company}/contracts/${contract}?${qs}`;
}

// ---------------------------------------------------------------- contract costing

export type FindingKind = "fact" | "calculation" | "unknown" | "inconsistency" | "decision";

export interface CostLine {
  record_id: string;
  ref: string;
  category: string;
  description: string | null;
  charged_to: string;
  charged_to_ref: string | null;
  charged_paise: number | null;
  record_amount_paise: number | null;
  shared_with: string[];
  counterparty: string | null;
  source_ids: string[];
}

export interface ContractInvestigation {
  as_of: string;
  contract: {
    ref: string;
    customer_name: string | null;
    status: string | null;
    body_type: string | null;
    body_count: number | null;
    value_paise: number | null;
  };
  cost_breakdown: {
    materials_paise: number;
    labour_paise: number;
    machinery_paise: number;
    other_paise: number;
    total_paise: number;
    entries: number;
    charges: number;
  };
  profitability: {
    provisional_profit_paise: number | null;
    provisional_margin_percent: string | null;
    pending_allocation_paise: number;
    profit_if_all_pending_charged_here_paise: number | null;
    label: string;
  };
  customer_account: { received_paise: number; outstanding_paise: number | null };
  cost_lines: CostLine[];
  findings: { kind: FindingKind; category: string; statement: string }[];
  missing_information: string[];
  warnings: string[];
  sources: { id: string; name: string; source_record_id: string | null }[];
  queries_executed: string[];
}

export function investigateContract(contract: string): Promise<ContractInvestigation> {
  return call(DEMO_COMPANIES.manufacturing, "/contracts/investigations", {
    contract,
    as_of: DEMO_AS_OF,
  });
}

// ---------------------------------------------------------------- engineering context

export interface AtlasInvestigation {
  question: string;
  findings: { category: string; statement: string; supported: boolean }[];
  missing_information: string[];
  warnings: string[];
  sources: { id: string; name: string; source_record_id: string | null }[];
  queries_executed: string[];
}

export const ATLAS_QUESTION =
  "Why is Project Atlas behind schedule, who owns the blockers, which previous decisions " +
  "are relevant, and what evidence supports the findings?";

export function investigateAtlas(): Promise<AtlasInvestigation> {
  return call(DEMO_COMPANIES.engineering, "/investigations", {
    question: ATLAS_QUESTION,
    as_of: DEMO_AS_OF,
  });
}

export async function attempt<T>(work: () => Promise<T>): Promise<{ data: T } | { error: string }> {
  try {
    return { data: await work() };
  } catch (err) {
    return { error: err instanceof Error ? err.message : "the live API is unreachable" };
  }
}
