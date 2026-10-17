import { sql } from "@/lib/db";
import { ask, audit, readable, requireWorkspace } from "@/lib/workspace";

export const metadata = { title: "Ask: Node & Nerve" };

export default async function Ask({ searchParams }: { searchParams: Promise<{ q?: string }> }) {
  const { workspace, userId } = await requireWorkspace();
  const question = (await searchParams).q?.trim().slice(0, 500) ?? "";
  let result: Awaited<ReturnType<typeof ask>> | undefined;
  let error: string | null = null;
  if (question) {
    try {
      result = await ask(workspace, question);
      await audit(workspace.id, userId, "question.asked", { question });
    } catch (e) {
      error = e instanceof Error ? e.message : "the investigation failed";
    }
  }
  const history = (await sql()`SELECT DISTINCT ON (detail->>'question') detail->>'question' AS q, at
    FROM audit_log WHERE workspace_id = ${workspace.id} AND action = 'question.asked'
    ORDER BY detail->>'question', at DESC`) as { q: string; at: string }[];
  history.sort((a, b) => b.at.localeCompare(a.at));

  return (
    <section className="app-section">
      <h1 className="app-title">Ask about {workspace.name}</h1>
      <p className="app-lede">
        Name a project, task or person. The answer is assembled from the graph by fixed queries, with
        the records it used and what is missing. No language model writes it.
      </p>
      <form className="ask-form" action="/app/ask">
        <input
          name="q"
          defaultValue={question}
          placeholder="Why is the Launch project late, and who owns the blockers?"
          aria-label="Your question"
          maxLength={500}
        />
        <button className="btn btn-primary" type="submit">
          Ask
        </button>
      </form>

      {error && <p className="error-panel">{error}</p>}
      {question && result === null && (
        <p className="error-panel">
          This workspace has no graph yet. Connect and sync a source first.
        </p>
      )}
      {result && (
        <div className="app-grid">
          <div className="panel wide">
            <h2>
              Matched:{" "}
              {result.matched_entities.length
                ? result.matched_entities.map((m) => m.name).join(", ")
                : "nothing in the graph matched the question's words"}
            </h2>
            <ul className="findings">
              {result.findings.map((f) => (
                <li key={f.statement}>{readable(f.statement)}</li>
              ))}
              {result.missing_information.map((m) => (
                <li key={m} style={{ "--kind-color": "var(--unknown)" } as React.CSSProperties}>
                  {readable(m)}
                </li>
              ))}
            </ul>
          </div>
          {result.sources.length > 0 && (
            <div className="panel wide">
              <h2>Records used</h2>
              <ul className="findings">
                {result.sources.map((s) => (
                  <li key={s.id}>
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
              <p className="note" style={{ marginTop: 10 }}>
                {result.queries_executed.length} graph queries: {result.queries_executed.join(", ")}.
              </p>
            </div>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div className="panel" style={{ marginTop: 20 }}>
          <h2>Asked before</h2>
          {history.slice(0, 8).map((h) => (
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
