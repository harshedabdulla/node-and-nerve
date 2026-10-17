import Link from "next/link";
import { redirect } from "next/navigation";
import { connectionFor, SHEETS, sourcesFor } from "@/lib/integrations";
import { listSpreadsheets, readSheet, sheetNames } from "@/lib/sheets";
import { requireWorkspace } from "@/lib/workspace";
import { saveAndSync, syncNow } from "../actions";

export const metadata = { title: "Google Sheets: Node & Nerve" };

const FIELDS: { key: string; label: string; required?: boolean; hint: RegExp }[] = [
  { key: "id", label: "Task id (stays the same between syncs)", required: true, hint: /\b(id|key|ref|#|no\.?)\b/i },
  { key: "name", label: "Task title", required: true, hint: /task|title|name|summary|item/i },
  { key: "status", label: "Status", hint: /status|state/i },
  { key: "owner", label: "Owner", hint: /owner|assignee|assigned|responsible|who/i },
  { key: "due", label: "Due date", hint: /due|deadline|target/i },
  { key: "project", label: "Project", hint: /project|epic|workstream/i },
  { key: "blocked_by", label: "Waits on (ids of other tasks)", hint: /block|depend|waiting|waits/i },
  { key: "priority", label: "Priority", hint: /priority|prio/i },
];

function guess(header: string[], hint: RegExp, taken: Set<string>): string {
  const hit = header.find((h) => h && hint.test(h) && !taken.has(h));
  if (hit) taken.add(hit);
  return hit ?? "";
}

function Problem({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  return <p className="error-panel">Google Sheets could not be read: {message}</p>;
}

export default async function GoogleSheets({
  searchParams,
}: {
  searchParams: Promise<{ spreadsheet?: string; name?: string; sheet?: string; error?: string }>;
}) {
  const { workspace, role } = await requireWorkspace();
  const connection = await connectionFor(workspace.id, SHEETS);
  if (connection?.status !== "connected") redirect("/app/integrations");
  const admin = role === "org:admin";
  const q = await searchParams;
  const sources = await sourcesFor(workspace.id);

  let step: React.ReactNode = null;
  if (!admin) {
    step = <p className="note">Only workspace admins can add sheets.</p>;
  } else if (!q.spreadsheet) {
    try {
      const files = await listSpreadsheets(workspace.id, connection.id);
      step = files.length ? (
        <ul className="pick">
          {files.map((f) => (
            <li key={f.id}>
              <Link href={`?spreadsheet=${encodeURIComponent(f.id)}&name=${encodeURIComponent(f.name)}`}>
                <span>{f.name}</span>
                <span className="note">{f.modified ? new Date(f.modified).toLocaleDateString("en-IN") : ""}</span>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="note">No spreadsheets were found in this Google account.</p>
      );
    } catch (e) {
      step = <Problem error={e} />;
    }
  } else if (!q.sheet) {
    try {
      const tabs = await sheetNames(workspace.id, connection.id, q.spreadsheet);
      step = (
        <ul className="pick">
          {tabs.map((t) => (
            <li key={t}>
              <Link
                href={`?spreadsheet=${encodeURIComponent(q.spreadsheet!)}&name=${encodeURIComponent(q.name ?? "")}&sheet=${encodeURIComponent(t)}`}
              >
                <span>{t}</span>
              </Link>
            </li>
          ))}
        </ul>
      );
    } catch (e) {
      step = <Problem error={e} />;
    }
  } else {
    try {
      const [header = [], ...rows] = await readSheet(workspace.id, connection.id, q.spreadsheet, q.sheet, 9);
      const columns = header.filter(Boolean);
      const taken = new Set<string>();
      const defaults = Object.fromEntries(FIELDS.map((f) => [f.key, guess(header, f.hint, taken)]));
      step = columns.length ? (
        <>
          <div className="preview">
            <table>
              <thead>
                <tr>
                  {header.map((h, i) => (
                    <th key={i}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    {header.map((_, j) => (
                      <td key={j}>{r[j] ?? ""}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <form action={saveAndSync}>
            <input type="hidden" name="spreadsheet_id" value={q.spreadsheet} />
            <input type="hidden" name="spreadsheet_name" value={q.name ?? q.spreadsheet} />
            <input type="hidden" name="sheet" value={q.sheet} />
            <p className="note" style={{ marginTop: 18 }}>
              Tell Node &amp; Nerve which column holds what. Suggestions come from the column names;
              check them. Leave a field empty if the sheet does not have it.
            </p>
            <div className="mapping">
              {FIELDS.map((f) => (
                <label key={f.key}>
                  {f.label}
                  {f.required ? "" : " (optional)"}
                  <select name={f.key} defaultValue={defaults[f.key]} required={f.required}>
                    <option value="">{f.required ? "Choose a column" : "Not in this sheet"}</option>
                    {columns.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
            <p style={{ marginTop: 20 }}>
              <button className="btn btn-primary" type="submit">
                Save and sync
              </button>
            </p>
          </form>
        </>
      ) : (
        <p className="note">The first row of this sheet is empty. Put column names in row 1.</p>
      );
    } catch (e) {
      step = <Problem error={e} />;
    }
  }

  return (
    <section className="app-section">
      <p className="note">
        <Link href="/app/integrations">Integrations</Link> / Google Sheets
      </p>
      <h1 className="app-title">Google Sheets</h1>
      <p className="app-lede">
        Each row becomes a task in {workspace.name}&rsquo;s graph, linked to its owner, project and
        the tasks it waits on, with the row kept as its source. Values that can&rsquo;t be read
        safely are reported, not guessed.
      </p>
      {q.error && <p className="error-panel">{q.error}</p>}

      {sources.length > 0 && (
        <div className="panel" style={{ marginTop: 28 }}>
          <h2>Synced sheets</h2>
          {sources.map((s) => (
            <div key={s.id} style={{ padding: "12px 0", borderTop: "1px solid var(--rule)" }}>
              <div className="row">
                <span>
                  {s.spreadsheet_name} / {s.sheet}
                </span>
                {admin && (
                  <form action={syncNow}>
                    <input type="hidden" name="source_id" value={s.id} />
                    <button className="btn btn-quiet" type="submit">
                      Sync now
                    </button>
                  </form>
                )}
              </div>
              {s.last_status === "succeeded" && s.last_report ? (
                <p className="note">
                  Last synced {new Date(s.last_at!).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST:{" "}
                  {s.last_report.rows_read} rows read, {s.last_report.tasks} tasks, {s.last_report.people}{" "}
                  people, {s.last_report.projects} projects, {s.last_report.links} links.
                </p>
              ) : s.last_status === "failed" ? (
                <p className="note" style={{ color: "var(--inconsistency)" }}>
                  Last sync failed: {s.last_error}
                </p>
              ) : (
                <p className="note">{s.last_status === "running" ? "Syncing now." : "Not synced yet."}</p>
              )}
              {s.last_report && s.last_report.warnings.length > 0 && (
                <ul className="warnings">
                  {s.last_report.warnings.slice(0, 12).map((w) => (
                    <li key={w}>{w}</li>
                  ))}
                  {s.last_report.warnings.length > 12 && (
                    <li>and {s.last_report.warnings.length - 12} more</li>
                  )}
                </ul>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="panel" style={{ marginTop: 20 }}>
        <h2>
          {!q.spreadsheet
            ? "Add a sheet: choose a spreadsheet"
            : !q.sheet
              ? `Add a sheet: choose a tab in ${q.name ?? "the spreadsheet"}`
              : `Add a sheet: ${q.name ?? ""} / ${q.sheet}`}
        </h2>
        {step}
      </div>
    </section>
  );
}
