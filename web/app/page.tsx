import Link from "next/link";
import { SiteFooter, SiteHeader } from "@/components/SiteChrome";
import {
  attempt,
  type ContractInvestigation,
  type FindingKind,
  inr,
  investigateContract,
  traceLink,
} from "@/lib/nn";

export const dynamic = "force-dynamic"; // always the current graph

const VIDEO =
  "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260809_012548_ef22562c-c0ae-4816-ad9d-f8922af4e6a7.mp4";

const KINDS: { kind: FindingKind; name: string; meaning: string }[] = [
  { kind: "fact", name: "Fact", meaning: "Recorded, and backed by a source." },
  { kind: "calculation", name: "Calculation", meaning: "Derived from facts by a stated rule." },
  { kind: "unknown", name: "Unknown", meaning: "Not recorded. Never filled in as zero." },
  { kind: "inconsistency", name: "Inconsistency", meaning: "Two records disagree." },
  { kind: "decision", name: "Decision", meaning: "A person has to decide before the figure is complete." },
];

const CONNECTORS = [
  {
    name: "Telegram",
    what: "Ask questions and get answers, with evidence links, in a private chat.",
    state: "live",
    label: "Live, allow-listed chats",
  },
  {
    name: "CSV files",
    what: "Import exports from accounting tools or spreadsheets. Every row is checked before anything is written.",
    state: "live",
    label: "Live, API and CLI",
  },
  {
    name: "Your own systems",
    what: "Send records, relationships and sources to the API. Re-sending the same record changes nothing.",
    state: "live",
    label: "Live, HTTP API",
  },
  {
    name: "Google Sheets",
    what: "Read the sheets you choose after signing in with Google, through Composio.",
    state: "building",
    label: "In development",
  },
  {
    name: "Zoho Books",
    what: "The account connects through Composio, but no read actions are available for it yet.",
    state: "blocked",
    label: "Waiting on connector",
  },
] as const;

function HeroTrace({ r }: { r: ContractInvestigation }) {
  const lines = r.cost_lines.filter((l) => l.ref === "PUR-2047");
  const line = lines[0];
  const cite = new Map(r.sources.map((s) => [s.id, s.source_record_id ?? s.name]));
  if (!line) return null;
  const inContract = lines.map((l) => l.charged_to_ref ?? l.charged_to);
  const elsewhere = line.shared_with.map((id) => id.split(":")[1]?.toUpperCase() ?? id);
  const link = traceLink("kaveri-body-works", "CT-104", ["PUR-2047"]);
  return (
    <aside className="trace" aria-label="A real traversal from the demo graph">
      <p className="trace-question">
        <span>Asked of a fictional truck-body builder&rsquo;s books</span>
        How much of the square-tube bill belongs to contract {r.contract.ref}?
      </p>
      <ol className="trace-path">
        <li className="trace-node">
          <span className="trace-title">
            {line.ref}: {line.description}
          </span>
          <span className="trace-meta num">
            {line.counterparty}, bill {inr(line.record_amount_paise)}. Source:{" "}
            {line.source_ids.map((s) => cite.get(s)).join(", ")}
          </span>
        </li>
        <li className="trace-node">
          <span className="trace-edge num">
            CHARGED_TO {inr(line.charged_paise)} each
          </span>
          <span className="trace-title">{inContract.join(" and ")}</span>
          <span className="trace-meta">two of the three bodies being built</span>
        </li>
        <li className="trace-node">
          <span className="trace-edge">PART_OF</span>
          <span className="trace-title">
            {r.contract.ref}, {r.contract.customer_name}
          </span>
        </li>
        {elsewhere.length > 0 && (
          <li className="trace-node is-shared">
            <span className="trace-edge num">CHARGED_TO {inr(line.charged_paise)}</span>
            <span className="trace-title">{elsewhere.join(", ")}</span>
            <span className="trace-meta">a different contract, so not counted here</span>
          </li>
        )}
      </ol>
      <div className="trace-result">
        <span>Counted for {r.contract.ref}</span>
        <strong className="num">{inr((line.charged_paise ?? 0) * lines.length)}</strong>
      </div>
      <p className="trace-foot">
        One bill, split three ways, counted once. Read live from FalkorDB
        {link ? (
          <>
, or <a href={link}>open the evidence graph</a>
          </>
        ) : null}
        .
      </p>
    </aside>
  );
}

function TraceUnavailable({ error }: { error: string }) {
  return (
    <aside className="trace">
      <p className="trace-question">
        <span>Live trace</span>
        The demo graph could not be read just now ({error}). No figures are shown rather than old
        ones.
      </p>
    </aside>
  );
}

export default async function Home() {
  const live = await attempt(() => investigateContract("CT-104"));
  const r = "data" in live ? live.data : null;
  const example = (kind: FindingKind) =>
    r?.findings.find((f) => f.kind === kind)?.statement ??
    (kind === "unknown" ? r?.missing_information[0] : undefined);

  return (
    <>
      <SiteHeader current="home" />
      <main>
        <section className="hero">
          <video className="hero-video" src={VIDEO} autoPlay muted loop playsInline aria-hidden="true" />
          <div className="hero-shade" aria-hidden="true" />
          <div className="wrap hero-grid">
            <div>
              <h1 className="display">Answers that show their working</h1>
              <p className="lede">
                Node &amp; Nerve links the records your business already keeps into one graph, then
                answers questions with the entries, links and sources behind every figure, and says
                what is still missing.
              </p>
              <div className="hero-actions">
                <Link href="/demo" className="btn btn-primary">
                  Open the live demo
                </Link>
                <a href="#how" className="btn btn-quiet">
                  See how it works
                </a>
              </div>
              <p className="hero-note">
                Accounts and company workspaces are being built next. Today you can explore two
                fictional companies.
              </p>
            </div>
            {r ? <HeroTrace r={r} /> : <TraceUnavailable error={"error" in live ? live.error : ""} />}
          </div>
        </section>

        <section className="section" id="how">
          <div className="wrap">
            <div className="section-head">
              <h2 className="h2">Your records already describe the business. They just aren&rsquo;t connected.</h2>
              <p>
                A purchase sits in one book, the payment in another, the job it was for in a third.
                Node &amp; Nerve stores each record once and keeps how they relate, so a question can
                follow those links instead of guessing.
              </p>
            </div>
            <ol className="steps">
              <li className="step">
                <h3>Connect what you use</h3>
                <p>
                  Bring in records from the tools and files you already keep. Each one keeps a link to
                  where it came from.
                </p>
              </li>
              <li className="step">
                <h3>Records become a graph</h3>
                <p>
                  Jobs, bills, payments, people, tasks and decisions become nodes, joined by what the
                  records actually say. Nothing is inferred to fill a gap.
                </p>
              </li>
              <li className="step">
                <h3>Ask, and get told</h3>
                <p>
                  Ask in plain language or set what should notify you. Each answer lists the records
                  it used and what is uncertain.
                </p>
              </li>
            </ol>
          </div>
        </section>

        <section className="section" id="integrations">
          <div className="wrap">
            <div className="section-head">
              <h2 className="h2">Integrations</h2>
              <p>
                What works today, and what doesn&rsquo;t yet. A connection is only shown as working once
                records from it have reached the graph.
              </p>
            </div>
            <div className="connectors">
              {CONNECTORS.map((c) => (
                <div className="connector" key={c.name}>
                  <span className="connector-name">{c.name}</span>
                  <p>{c.what}</p>
                  <span className={`state state-${c.state}`}>{c.label}</span>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="section">
          <div className="wrap">
            <div className="section-head">
              <h2 className="h2">Every finding says what kind of claim it is</h2>
              <p>
                Figures are computed by the engine from the graph, never by a language model. Each
                finding is labelled, so you can tell a recorded fact from a gap.
              </p>
            </div>
            <div className="kinds">
              {KINDS.map((k) => {
                const sample = example(k.kind);
                return (
                  <div className={`kind kind-${k.kind}`} key={k.kind}>
                    <span className="kind-name">{k.name}</span>
                    <p>
                      {k.meaning}
                      {sample && <small>From the demo: {sample}</small>}
                    </p>
                  </div>
                );
              })}
            </div>
          </div>
        </section>

        <section className="section">
          <div className="wrap split">
            <div className="section-head">
              <h2 className="h2">Answers in the chat you already use</h2>
              <p>
                The Telegram bot answers the owner&rsquo;s questions from the same graph and links to the
                evidence. It only replies to chats the owner has allowed, and it never messages
                customers or suppliers.
              </p>
              <ul className="plain-list">
                <li>Commands answer straight from the graph, without AI.</li>
                <li>Questions in plain language go to an agent that can only call the investigations.</li>
                <li>Scheduled alerts for what matters to you come with workspaces.</li>
              </ul>
            </div>
            {r && (
              <div className="chat" aria-label="Example Telegram exchange using live demo figures">
                <div className="chat-bubble chat-out">/contract {r.contract.ref}</div>
                <div className="chat-bubble num">
                  <b>
                    {r.contract.ref} · {r.contract.customer_name}
                  </b>
                  <br />
                  Recorded direct cost (provisional): <b>{inr(r.cost_breakdown.total_paise)}</b>
                  <br />
                  Provisional profit <b>{inr(r.profitability.provisional_profit_paise)}</b> (
                  {r.profitability.provisional_margin_percent}%)
                  <ul>
                    <li>{r.findings.filter((f) => f.kind === "decision").length} items need your decision</li>
                    <li>
                      {r.findings.filter((f) => f.kind === "unknown" || f.kind === "inconsistency")
                        .length + r.missing_information.length}{" "}
                      items to verify
                    </li>
                  </ul>
                  <a href="/demo">Evidence graph for {r.contract.ref}</a>
                </div>
              </div>
            )}
          </div>
        </section>

        <section className="section">
          <div className="wrap">
            <div className="section-head">
              <h2 className="h2">Try it on two fictional companies</h2>
              <p>The same engine, two very different businesses. Both run live against FalkorDB.</p>
            </div>
            <div className="scenarios">
              <Link href="/demo" className="scenario">
                <h3>A truck-body builder&rsquo;s contract costs</h3>
                <p>
                  Purchases, wages, machinery and payments from separate paper books. What has
                  contract CT-104 cost so far, and what is missing?
                </p>
                <span className="go">Open the contract investigation</span>
              </Link>
              <Link href="/demo?case=engineering" className="scenario">
                <h3>A software team&rsquo;s late project</h3>
                <p>
                  Tickets, incidents, owners and decisions. Why is Project Atlas late, who owns the
                  blockers, and which decisions still apply?
                </p>
                <span className="go">Open the project investigation</span>
              </Link>
            </div>
          </div>
        </section>
      </main>
      <SiteFooter />
    </>
  );
}
