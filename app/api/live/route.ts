import { createHmac } from "node:crypto";
import { NextResponse } from "next/server";

// Live figures for the demo contract, fetched server-side from the Node & Nerve API.
// The API key and trace secret stay on the server; the browser only sees this summary.

const COMPANY = "kaveri-body-works";
const CONTRACT = "CT-104";
const AS_OF = "2026-10-16"; // reporting date of the synthetic books
const LINK_TTL_S = 7 * 24 * 3600;

export const dynamic = "force-dynamic"; // always the current graph, never a build-time copy

type Line = { charged_to_ref: string | null; charged_to: string; charged_paise: number | null };
type Finding = { kind: string; statement: string };

function inr(paise: number | null | undefined): string | null {
  if (paise === null || paise === undefined) return null;
  return `₹${Math.round(paise / 100).toLocaleString("en-IN")}`;
}

function traceLink(base: string, secret: string): string {
  const exp = Math.floor(Date.now() / 1000) + LINK_TTL_S;
  const sig = createHmac("sha256", secret)
    .update(`${COMPANY}|${CONTRACT.toLowerCase()}|${exp}`)
    .digest("hex")
    .slice(0, 32);
  const qs = new URLSearchParams({ exp: String(exp), sig, as_of: AS_OF });
  return `${base}/trace/${COMPANY}/contracts/${CONTRACT}?${qs}`;
}

export async function GET() {
  const base = process.env.NN_API_URL?.replace(/\/$/, "");
  const key = process.env.NN_API_KEY;
  if (!base || !key) {
    return NextResponse.json({ error: "live API is not configured" }, { status: 503 });
  }
  const started = Date.now();
  const res = await fetch(`${base}/v1/companies/${COMPANY}/contracts/investigations`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-api-key": key },
    body: JSON.stringify({ contract: CONTRACT, as_of: AS_OF }),
    cache: "no-store",
  }).catch(() => null);
  if (!res || !res.ok) {
    return NextResponse.json(
      { error: `live API unavailable (${res ? res.status : "unreachable"})` },
      { status: 502 },
    );
  }
  const r = await res.json();
  const byJob = new Map<string, number>();
  for (const ln of r.cost_lines as Line[]) {
    const ref = ln.charged_to_ref ?? ln.charged_to;
    byJob.set(ref, (byJob.get(ref) ?? 0) + (ln.charged_paise ?? 0));
  }
  const findings = r.findings as Finding[];
  const secret = process.env.NN_TRACE_SECRET;
  return NextResponse.json({
    company: COMPANY,
    contract: {
      ref: r.contract.ref,
      customer: r.contract.customer_name,
      bodies: `${r.contract.body_count} ${r.contract.body_type} bodies`,
      status: r.contract.status,
      value: inr(r.contract.value_paise),
    },
    as_of: r.as_of,
    cost: {
      materials: inr(r.cost_breakdown.materials_paise),
      labour: inr(r.cost_breakdown.labour_paise),
      machinery: inr(r.cost_breakdown.machinery_paise),
      other: inr(r.cost_breakdown.other_paise),
      total: inr(r.cost_breakdown.total_paise),
      entries: r.cost_breakdown.entries,
      charges: r.cost_breakdown.charges,
    },
    by_job: [...byJob.entries()].map(([ref, paise]) => ({ ref, total: inr(paise) })),
    profit: {
      label: r.profitability.label,
      amount: inr(r.profitability.provisional_profit_paise),
      margin: r.profitability.provisional_margin_percent,
      unsplit: inr(r.profitability.pending_allocation_paise),
      lower_bound: inr(r.profitability.profit_if_all_pending_charged_here_paise),
    },
    customer: {
      received: inr(r.customer_account.received_paise),
      to_receive: inr(r.customer_account.outstanding_paise),
    },
    decisions: findings.filter((f) => f.kind === "decision").map((f) => f.statement),
    to_verify: findings
      .filter((f) => f.kind === "unknown" || f.kind === "inconsistency")
      .map((f) => f.statement)
      .concat(r.missing_information),
    queries: r.queries_executed,
    trace_url: secret ? traceLink(base, secret) : null,
    api_url: base,
    fetched_ms: Date.now() - started,
  });
}
