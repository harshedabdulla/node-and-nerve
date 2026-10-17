import { EvidenceGraph, type GraphEdge, type GraphNode } from "@/components/EvidenceGraph";
import { sql } from "@/lib/db";
import { type AskResult, askWithModel, audit, readable, requireWorkspace } from "@/lib/workspace";

export const metadata = { title: "Ask: Node & Nerve" };

type Cites = Map<string, string | null>; // citation label -> link

/** The model's short markdown: bold, "- " bullets, paragraphs, [citation] chips. */
function Answer({ text, cites }: { text: string; cites: Cites }) {
  const inline = (line: string, key: string) =>
    line.split(/(\*\*[^*]+\*\*|\[[^\]]+\])/g).map((part, i) => {
      if (part.startsWith("**") && part.endsWith("**")) return <strong key={`${key}-${i}`}>{part.slice(2, -2)}</strong>;
      if (part.startsWith("[") && part.endsWith("]")) {
        const label = part.slice(1, -1);
        const href = cites.get(label);
        return href ? (
          <a key={`${key}-${i}`} className="cite" href={href} target="_blank" rel="noreferrer">
            {label}
          </a>
        ) : (
          <span key={`${key}-${i}`} className="cite">
            {label}
          </span>
        );
      }
      return part;
    });
  const blocks: React.ReactNode[] = [];
  let bullets: string[] = [];
  const flush = (k: number) => {
    if (bullets.length) blocks.push(<ul key={`ul-${k}`}>{bullets.map((b, i) => <li key={i}>{inline(b, `b${k}-${i}`)}</li>)}</ul>);
    bullets = [];
  };
  text.split("\n").forEach((raw, k) => {
    const line = raw.trim();
    if (line.startsWith("- ")) return void bullets.push(line.slice(2));
    flush(k);
    if (line) blocks.push(<p key={k}>{inline(line, `p${k}`)}</p>);
  });
  flush(-1);
  return <div className="ai-answer">{blocks}</div>;
}

function graphOf(r: AskResult): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const focus = new Set(r.investigations.flatMap((i) => i.matched_entities.map((m) => m.id)));
  const edges = new Map<string, GraphEdge>();
  for (const inv of r.investigations)
    for (const p of inv.evidence_paths)
      for (const e of p.edges)
        if (r.labels[e.from_id] && r.labels[e.to_id])
          edges.set(`${e.from_id}|${e.type}|${e.to_id}`, {
            from: e.from_id,
            to: e.to_id,
            type: e.type,
            source: e.source_id,
          });
  const labels = { ...r.labels };
  const ids = new Set([...edges.values()].flatMap((e) => [e.from, e.to]));
  for (const id of focus) if (labels[id]) ids.add(id);
  // Owners of the records on the paths, as the investigation reported them.
  for (const inv of r.investigations)
    for (const o of inv.current_owners ?? [])
      if (ids.has(o.entity_id)) {
        labels[o.owner_id] ??= { kind: o.owner_kind, ref: o.owner_name ?? o.owner_id, name: o.owner_name, status: null };
        ids.add(o.owner_id);
        edges.set(`${o.owner_id}|OWNS|${o.entity_id}`, { from: o.owner_id, to: o.entity_id, type: "OWNS", source: o.source_id });
      }
  const nodes = [...ids].map((id) => ({ id, ...labels[id], focus: focus.has(id) }));
  return { nodes, edges: [...edges.values()] };
}

export default async function Ask({ searchParams }: { searchParams: Promise<{ q?: string }> }) {
  const { workspace, userId } = await requireWorkspace();
  const question = (await searchParams).q?.trim().slice(0, 500) ?? "";
  let result: AskResult | null | undefined;
  let error: string | null = null;
  if (question) {
    try {
      result = await askWithModel(workspace, question);
      await audit(workspace.id, userId, "question.asked", { question });
    } catch (e) {
      error = e instanceof Error ? e.message : "the investigation failed";
    }
  }
  // Latest distinct questions, newest first. Ordered in SQL: timestamps arrive as Date objects.
  const history = (await sql()`SELECT q FROM (
      SELECT DISTINCT ON (detail->>'question') detail->>'question' AS q, at
      FROM audit_log WHERE workspace_id = ${workspace.id} AND action = 'question.asked'
      ORDER BY detail->>'question', at DESC
    ) latest ORDER BY at DESC LIMIT 8`) as { q: string }[];

  const sources = new Map<string, { name: string; uri: string | null; ref: string | null }>();
  const findings = new Map<string, string>();
  const missing = new Set<string>();
  for (const inv of result?.investigations ?? []) {
    for (const s of inv.sources) sources.set(s.id, { name: s.name, uri: s.uri, ref: s.source_record_id });
    for (const f of inv.findings) if (f.category !== "expertise") findings.set(f.statement, readable(f.statement));
    for (const m of inv.missing_information) missing.add(readable(m));
  }
  const cites: Cites = new Map([...sources.values()].filter((s) => s.ref).map((s) => [s.ref!, s.uri]));
  const graph = result ? graphOf(result) : null;
  const matched = [...new Set(result?.investigations.flatMap((i) => i.matched_entities.map((m) => m.name ?? m.id)) ?? [])];

  return (
    <section className="app-section">
      <h1 className="app-title">Ask about {workspace.name}</h1>
      <p className="app-lede">
        Ask in your own words. The answer is written from the records your question leads to, and
        every claim points back to the row it came from.
      </p>
      <form className="ask-form" action="/app/ask">
        <input
          name="q"
          defaultValue={question}
          placeholder="Why is the Kochi showroom late, and what should I chase first?"
          aria-label="Your question"
          maxLength={500}
        />
        <button className="btn btn-primary" type="submit">
          Ask
        </button>
      </form>

      {error && <p className="error-panel">{error}</p>}
      {question && result === null && (
        <p className="error-panel">This workspace has no graph yet. Connect and sync a source first.</p>
      )}
      {result && (
        <div className="app-grid">
          <div className="panel wide">
            {result.answer ? (
              <>
                <h2>Answer</h2>
                <Answer text={result.answer} cites={cites} />
                <p className="note" style={{ marginTop: 14 }}>
                  Written by AI ({result.model}) from the records below. It can only read; the
                  facts come from the graph.
                </p>
              </>
            ) : (
              <>
                <h2>
                  {matched.length ? `About ${matched.join(", ")}` : "Nothing in your records matched the question"}
                </h2>
                <p className="note">
                  Showing the records directly: {result.fallback_reason ?? "no written answer"}.
                </p>
              </>
            )}
          </div>

          {graph && graph.nodes.length > 0 && (
            <div className="panel wide">
              <h2>How the answer was found</h2>
              <EvidenceGraph key={question} nodes={graph.nodes} edges={graph.edges} />
            </div>
          )}

          {(findings.size > 0 || missing.size > 0) && (
            <div className="panel wide">
              <h2>Findings from the graph</h2>
              <ul className="findings">
                {[...findings.values()].map((f) => (
                  <li key={f}>{f}</li>
                ))}
                {[...missing].map((m) => (
                  <li key={m} style={{ "--kind-color": "var(--unknown)" } as React.CSSProperties}>
                    {m}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {sources.size > 0 && (
            <div className="panel wide">
              <h2>Records used</h2>
              <ul className="findings">
                {[...sources.entries()].map(([id, s]) => (
                  <li key={id}>
                    {s.uri ? (
                      <a href={s.uri} target="_blank" rel="noreferrer" style={{ color: "var(--paper)" }}>
                        {s.name}
                      </a>
                    ) : (
                      s.name
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div className="panel" style={{ marginTop: 20 }}>
          <h2>Asked before</h2>
          {history.map((h) => (
            <div className="row" key={h.q}>
              <a href={`/app/ask?q=${encodeURIComponent(h.q)}`} style={{ color: "var(--paper)" }}>
                {h.q}
              </a>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
