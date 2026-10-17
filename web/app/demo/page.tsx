import Link from "next/link";
import { SiteFooter, SiteHeader } from "@/components/SiteChrome";
import {
  ATLAS_QUESTION,
  type AtlasInvestigation,
  attempt,
  type ContractInvestigation,
  type FindingKind,
  inr,
  investigateAtlas,
  investigateContract,
  traceLink,
} from "@/lib/nn";

export const dynamic = "force-dynamic"; // always the current graph

export const metadata = { title: "Live demo: Node & Nerve" };

const KIND_COLOR: Record<FindingKind, string> = {
  fact: "var(--fact)",
  calculation: "var(--calculation)",
  unknown: "var(--unknown)",
  inconsistency: "var(--inconsistency)",
  decision: "var(--decision)",
};

function Findings({ items }: { items: { kind: FindingKind; text: string }[] }) {
  return (
    <ul className="findings">
      {items.map((f) => (
        <li key={f.text} style={{ "--kind-color": KIND_COLOR[f.kind] } as React.CSSProperties}>
          {f.text}
        </li>
      ))}
    </ul>
  );
}

function Queries({ names }: { names: string[] }) {
  return (
    <p className="queries">
      {names.length} parameterized graph queries ran for this answer:{" "}
      {names.map((n, i) => (
        <span key={n}>
          {i > 0 && ", "}
          <code>{n}</code>
        </span>
      ))}
      .
    </p>
  );
}

function Contract({ r }: { r: ContractInvestigation }) {
  const b = r.cost_breakdown;
  const p = r.profitability;
  const byJob = new Map<string, number>();
  for (const l of r.cost_lines) {
    const ref = l.charged_to_ref ?? l.charged_to;
    byJob.set(ref, (byJob.get(ref) ?? 0) + (l.charged_paise ?? 0));
  }
  const toVerify = [
    ...r.findings
      .filter((f) => f.kind === "unknown" || f.kind === "inconsistency")
      .map((f) => ({ kind: f.kind, text: f.statement })),
    ...r.missing_information.map((m) => ({ kind: "unknown" as const, text: m })),
  ];
  const link = traceLink("kaveri-body-works", r.contract.ref);
  return (
    <div className="ledger">
      <section className="panel">
        <h2>Recorded direct cost, as of {r.as_of}</h2>
        <div className="row num">
          <span>Materials</span>
          <span>{inr(b.materials_paise)}</span>
        </div>
        <div className="row num">
          <span>Labour</span>
          <span>{inr(b.labour_paise)}</span>
        </div>
        <div className="row num">
          <span>Machinery</span>
          <span>{inr(b.machinery_paise)}</span>
        </div>
        <div className="row num">
          <span>Other direct</span>
          <span>{inr(b.other_paise)}</span>
        </div>
        <div className="row total num">
          <span>Total</span>
          <span>{inr(b.total_paise)}</span>
        </div>
        <p className="note">
          {b.entries} book entries, {b.charges} charges. A bill split across bodies counts once per
          body, for its share only.
        </p>
      </section>

      <section className="panel">
        <h2>Provisional profit</h2>
        <div className="big num">
          {inr(p.provisional_profit_paise)}{" "}
          <span style={{ fontSize: 18, color: "var(--muted)" }}>
            {p.provisional_margin_percent}% of {inr(r.contract.value_paise)}
          </span>
        </div>
        <p className="note">
          {p.label}. If the {inr(p.pending_allocation_paise)} not yet split were all charged here, it
          would be {inr(p.profit_if_all_pending_charged_here_paise)}.
        </p>
        <div className="row num" style={{ marginTop: 14 }}>
          <span>Customer has paid</span>
          <span>{inr(r.customer_account.received_paise)}</span>
        </div>
        <div className="row num">
          <span>Still to receive</span>
          <span>{inr(r.customer_account.outstanding_paise)}</span>
        </div>
      </section>

      <section className="panel">
        <h2>Cost by job (purchase, CHARGED_TO job, PART_OF contract)</h2>
        {[...byJob.entries()].map(([ref, paise]) => (
          <div className="row num" key={ref}>
            <span>{ref === r.contract.ref ? `${ref} directly` : ref}</span>
            <span>{inr(paise)}</span>
          </div>
        ))}
      </section>

      <section className="panel">
        <h2>Needs a decision</h2>
        <Findings
          items={r.findings
            .filter((f) => f.kind === "decision")
            .map((f) => ({ kind: f.kind, text: f.statement }))}
        />
      </section>

      <section className="panel wide">
        <h2>To verify</h2>
        <Findings items={toVerify} />
      </section>

      <section className="panel wide">
        <Queries names={r.queries_executed} />
        <p className="note">{r.warnings.join(" ")}</p>
        {link && (
          <p style={{ marginTop: 16 }}>
            <a className="btn btn-primary" href={link}>
              Open the evidence graph
            </a>
          </p>
        )}
      </section>
    </div>
  );
}

function Atlas({ r }: { r: AtlasInvestigation }) {
  const cite = new Map(r.sources.map((s) => [s.id, s.source_record_id ?? s.name]));
  return (
    <div className="ledger">
      <section className="panel wide">
        <h2>Findings</h2>
        <Findings
          items={r.findings.map((f) => ({
            kind: f.supported ? "fact" : "unknown",
            text: f.statement,
          }))}
        />
      </section>
      <section className="panel">
        <h2>Missing information</h2>
        <Findings items={r.missing_information.map((m) => ({ kind: "unknown", text: m }))} />
      </section>
      <section className="panel">
        <h2>Sources used</h2>
        <ul className="findings">
          {r.sources.map((s) => (
            <li key={s.id}>
              {s.name}
              {cite.get(s.id) !== s.name ? ` (${cite.get(s.id)})` : ""}
            </li>
          ))}
        </ul>
      </section>
      <section className="panel wide">
        <Queries names={r.queries_executed} />
        {r.warnings.length > 0 && <p className="note">{r.warnings.join(" ")}</p>}
      </section>
    </div>
  );
}

export default async function Demo({
  searchParams,
}: {
  searchParams: Promise<{ case?: string }>;
}) {
  const engineering = (await searchParams).case === "engineering";
  const result = engineering
    ? await attempt(investigateAtlas)
    : await attempt(() => investigateContract("CT-104"));

  return (
    <>
      <SiteHeader current="demo" />
      <main className="wrap page-top">
        <p className="demo-banner">
          Demo mode. These companies, people, amounts and documents are fictional. Nothing here comes
          from a real business, and no sign-in is needed.
        </p>
        <nav className="tabs" aria-label="Demo scenario">
          <Link href="/demo" aria-current={engineering ? undefined : "page"}>
            Truck-body builder
          </Link>
          <Link href="/demo?case=engineering" aria-current={engineering ? "page" : undefined}>
            Software team
          </Link>
        </nav>
        <h1 className="demo-title">
          {engineering
            ? "Why is Project Atlas behind schedule?"
            : "What has contract CT-104 cost so far?"}
        </h1>
        <p className="demo-question">
          {engineering
            ? `Northstar Systems. Asked: “${ATLAS_QUESTION}”`
            : "Kaveri Body Works, Namakkal: three closed container bodies for Pon Logistics. Every figure is traversed from the books' records in FalkorDB."}
        </p>
        {"error" in result ? (
          <p className="error-panel">
            The live graph could not be read ({result.error}). Try again in a minute; no stored or
            sample figures are shown in its place.
          </p>
        ) : engineering ? (
          <Atlas r={result.data as AtlasInvestigation} />
        ) : (
          <Contract r={result.data as ContractInvestigation} />
        )}
      </main>
      <div style={{ height: 96 }} />
      <SiteFooter />
    </>
  );
}
