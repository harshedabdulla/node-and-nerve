"use client";

import { useEffect, useState } from "react";
import { NavTab } from "./Header";

interface ProductModalProps {
  view: NavTab | "signin" | "explore" | null;
  onClose: () => void;
}

const REPO = "https://github.com/harshedabdulla/node-and-nerve";
const BOT = "https://t.me/node_and_nerve_bot";

// Shape returned by /api/live (a server-side summary of the real API response).
interface Live {
  company: string;
  contract: { ref: string; customer: string; bodies: string; status: string; value: string };
  as_of: string;
  cost: {
    materials: string;
    labour: string;
    machinery: string;
    other: string;
    total: string;
    entries: number;
    charges: number;
  };
  by_job: { ref: string; total: string }[];
  profit: { label: string; amount: string; margin: string; unsplit: string; lower_bound: string };
  customer: { received: string; to_receive: string };
  decisions: string[];
  to_verify: string[];
  queries: string[];
  trace_url: string | null;
  api_url: string;
  fetched_ms: number;
}

type LiveState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; data: Live };

function useLive(enabled: boolean): LiveState {
  const [state, setState] = useState<LiveState>({ status: "loading" });
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setState({ status: "loading" });
    fetch("/api/live")
      .then(async (res) => {
        const body = await res.json();
        if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
        return body as Live;
      })
      .then((data) => !cancelled && setState({ status: "ok", data }))
      .catch((err: Error) => !cancelled && setState({ status: "error", message: err.message }));
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return state;
}

const box = "bg-black/60 rounded-2xl p-4 border border-white/10 text-xs";
const muted = "text-[#8e8e8e]";

function Row({ label, value, strong }: { label: string; value: string | null; strong?: boolean }) {
  return (
    <div className={`flex justify-between gap-4 ${strong ? "text-white font-semibold" : ""}`}>
      <span className={strong ? "" : muted}>{label}</span>
      <span className="font-mono text-right">{value ?? "not recorded"}</span>
    </div>
  );
}

function LiveStatus({ state }: { state: LiveState }) {
  if (state.status === "loading") {
    return <div className={`${box} ${muted}`}>Querying FalkorDB through the live API…</div>;
  }
  if (state.status === "error") {
    return (
      <div className={`${box} text-amber-300`}>
        Live API unavailable: {state.message}. No figures are shown rather than stale ones.
      </div>
    );
  }
  return null;
}

function LiveContract({ data }: { data: Live }) {
  return (
    <div className="space-y-3">
      <div className={`${box} space-y-1.5`}>
        <div className="flex justify-between text-white/50">
          <span>
            {data.contract.ref} · {data.contract.customer}
          </span>
          <span className="uppercase">{data.contract.status.replace("_", " ")}</span>
        </div>
        <div className={muted}>
          {data.contract.bodies} · contract value {data.contract.value} · as of {data.as_of}
        </div>
        <div className="pt-2 space-y-1">
          <Row label="Materials" value={data.cost.materials} />
          <Row label="Labour" value={data.cost.labour} />
          <Row label="Machinery" value={data.cost.machinery} />
          <Row label="Other direct" value={data.cost.other} />
          <Row label="Recorded direct cost" value={data.cost.total} strong />
          <div className={muted}>
            {data.cost.entries} book entries, {data.cost.charges} charges (split bills count per body)
          </div>
        </div>
        <div className="pt-2 border-t border-white/10 space-y-1">
          <Row
            label="Provisional profit"
            value={`${data.profit.amount} (${data.profit.margin}%)`}
            strong
          />
          <div className={muted}>
            {data.profit.label}. If the {data.profit.unsplit} not yet split all lands here:{" "}
            {data.profit.lower_bound}.
          </div>
          <Row
            label="Customer paid / to receive"
            value={`${data.customer.received} / ${data.customer.to_receive}`}
          />
        </div>
      </div>

      <div className={`${box} space-y-1`}>
        <div className="text-white/50 mb-1">Cost by job (CHARGED_TO → Job → PART_OF → Contract)</div>
        {data.by_job.map((j) => (
          <Row key={j.ref} label={j.ref} value={j.total} />
        ))}
      </div>

      <div className={`${box} space-y-2`}>
        <div className="text-white font-medium">Needs a decision ({data.decisions.length})</div>
        <ul className={`list-disc pl-4 space-y-1 ${muted}`}>
          {data.decisions.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
        <div className="text-white font-medium pt-1">To verify ({data.to_verify.length})</div>
        <ul className={`list-disc pl-4 space-y-1 ${muted}`}>
          {data.to_verify.map((d) => (
            <li key={d}>{d}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export function ProductModal({ view, onClose }: ProductModalProps) {
  const [activeTab, setActiveTab] = useState<"atlas" | "manufacturing">("manufacturing");
  const live = useLive(
    view === "explore" || view === "signin" || (view === "cases" && activeTab === "manufacturing"),
  );

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && view) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [view, onClose]);

  if (!view || view === "home") return null;

  const apiUrl = live.status === "ok" ? live.data.api_url : "https://node-and-nerve-api.vercel.app";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
      className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:p-6 overflow-y-auto"
    >
      {/* Backdrop */}
      <div
        onClick={onClose}
        className="fixed inset-0 bg-black/80 backdrop-blur-md transition-opacity"
        aria-hidden="true"
      />

      {/* Modal Surface */}
      <div className="relative z-10 w-full max-w-2xl bg-[#121214] border border-white/15 rounded-3xl p-6 sm:p-8 shadow-[0_20px_60px_rgba(0,0,0,0.8)] text-white overflow-hidden animate-reveal my-auto select-text">
        {/* Top Header */}
        <div className="flex items-center justify-between pb-4 mb-5 border-b border-white/10">
          <div className="flex items-center gap-2.5">
            <span className="font-display text-white/50 text-sm">⬡</span>
            <span
              id="modal-title"
              className="font-sans-ui text-sm font-semibold tracking-wide uppercase text-white/90"
            >
              {view === "product" && "Engine & Architecture"}
              {view === "cases" && "Case Studies (synthetic data)"}
              {view === "contact" && "Project & Links"}
              {view === "signin" && "API Access"}
              {view === "explore" && "Live Investigation"}
            </span>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close dialog"
            className="w-8 h-8 rounded-full bg-white/5 hover:bg-white/15 text-white/70 hover:text-white flex items-center justify-center text-sm transition-colors cursor-pointer"
          >
            ✕
          </button>
        </div>

        {/* Modal Body Content */}
        {view === "product" && (
          <div className="space-y-4 text-sm text-[#c8c8c8] font-sans-ui leading-relaxed">
            <p>
              <strong className="text-white">Node & Nerve</strong> is a graph-powered company context engine built on{" "}
              <a
                href="https://www.falkordb.com"
                target="_blank"
                rel="noreferrer"
                className="text-white underline underline-offset-2 hover:text-white/80"
              >
                FalkorDB
              </a>
              . Records become nodes, how they relate becomes typed relationships, and every answer is a traversal
              you can trace back to the book page or document that asserted it.
            </p>

            <div className="bg-black/60 rounded-2xl p-4 border border-white/10 font-mono text-xs text-white/85 space-y-2">
              <div className="text-white/40">// Relationships the investigations traverse</div>
              <div>Purchase -CHARGED_TO {"{amount}"}-&gt; Job -PART_OF-&gt; Contract</div>
              <div>Payment -ALLOCATED_TO {"{amount}"}-&gt; Purchase -SUPPLIED_BY-&gt; Supplier</div>
              <div>Ticket -BLOCKED_BY-&gt; Ticket &lt;-AFFECTS- Incident</div>
              <div>Decision -SUPERSEDES-&gt; Decision · Record -SUPPORTED_BY-&gt; Source</div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2">
              <div className="p-3.5 rounded-xl bg-white/[0.03] border border-white/10">
                <div className="text-white font-medium mb-1">Fixed, parameterized Cypher</div>
                <div className="text-xs text-[#8e8e8e]">
                  Investigations run read-only query templates with bounded traversals. Figures are computed by the
                  engine, never by the language model.
                </div>
              </div>
              <div className="p-3.5 rounded-xl bg-white/[0.03] border border-white/10">
                <div className="text-white font-medium mb-1">Gaps are reported, not filled</div>
                <div className="text-xs text-[#8e8e8e]">
                  Unsplit costs, unconfirmed payments, books that disagree and entries without a source are listed
                  as decisions or unknowns.
                </div>
              </div>
            </div>
          </div>
        )}

        {view === "cases" && (
          <div className="space-y-4 text-sm text-[#c8c8c8] font-sans-ui leading-relaxed">
            {/* Case selector tabs */}
            <div className="flex gap-2 p-1 bg-black/50 rounded-xl border border-white/10">
              <button
                type="button"
                onClick={() => setActiveTab("manufacturing")}
                className={`flex-1 py-1.5 px-3 rounded-lg text-xs font-medium transition-all ${
                  activeTab === "manufacturing"
                    ? "bg-white text-black font-semibold shadow-sm"
                    : "text-white/60 hover:text-white"
                }`}
              >
                Manufacturing: Truck Body Costing
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("atlas")}
                className={`flex-1 py-1.5 px-3 rounded-lg text-xs font-medium transition-all ${
                  activeTab === "atlas"
                    ? "bg-white text-black font-semibold shadow-sm"
                    : "text-white/60 hover:text-white"
                }`}
              >
                Engineering: Project Atlas
              </button>
            </div>

            {activeTab === "manufacturing" ? (
              <div className="space-y-3 pt-1">
                <p>
                  Kaveri Body Works, a fictional lorry-body builder whose purchases, wages, machinery costs and
                  payments sit in separate paper books: &ldquo;How much has CT-104 cost so far, and what is
                  missing?&rdquo;
                </p>
                <LiveStatus state={live} />
                {live.status === "ok" && (
                  <div className={`${box} space-y-1`}>
                    <Row
                      label={`${live.data.contract.ref} recorded direct cost`}
                      value={live.data.cost.total}
                      strong
                    />
                    <Row
                      label="Provisional profit"
                      value={`${live.data.profit.amount} (${live.data.profit.margin}%)`}
                    />
                    <Row
                      label="Decisions needed / items to verify"
                      value={`${live.data.decisions.length} / ${live.data.to_verify.length}`}
                    />
                    <div className={`pt-1 ${muted}`}>
                      Open &ldquo;Explore Node &amp; Nerve&rdquo; for the full live investigation.
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="space-y-3 pt-1">
                <p>
                  Northstar Systems, a fictional software company: &ldquo;Why is Project Atlas behind schedule, who
                  owns the blockers, and which decisions are relevant?&rdquo;
                </p>
                <div className={`${box} space-y-2 font-mono`}>
                  <div className="text-white/50">// What the graph returns</div>
                  <div>ATLAS-142 is blocked and overdue (due 2026-10-01), owned by Dev Patel</div>
                  <div>ATLAS-142 BLOCKED_BY INT-88 (v2 webhook signing), owned by Sam Okafor</div>
                  <div>INC-009 (v2 signature failures) AFFECTS the blocker chain</div>
                  <div>DEC-004 &ldquo;v2 only&rdquo; SUPERSEDES DEC-002; the old decision is excluded</div>
                  <div>Expert: Lena Ortiz (resolved INC-007 in the same component)</div>
                  <div className={`pt-2 border-t border-white/10 ${muted} font-sans-ui`}>
                    <strong className="text-white">Missing:</strong> ATLAS-147 is overdue but no blocker, incident
                    or owner is recorded.
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {view === "contact" && (
          <div className="space-y-4 text-sm text-[#c8c8c8] font-sans-ui leading-relaxed">
            <p>
              Node & Nerve is an open-source prototype built for FalkorDB Graph Hacks by the founder of Spindle. All
              data in the demos is synthetic.
            </p>
            <div className="p-4 rounded-2xl bg-black/60 border border-white/10 space-y-2 text-xs">
              <div className="flex items-center justify-between gap-4">
                <span className={muted}>Source code:</span>
                <a
                  href={REPO}
                  target="_blank"
                  rel="noreferrer"
                  className="text-white font-mono underline underline-offset-2"
                >
                  github.com/harshedabdulla/node-and-nerve
                </a>
              </div>
              <div className="flex items-center justify-between gap-4">
                <span className={muted}>Telegram bot (private, allow-listed chats):</span>
                <a
                  href={BOT}
                  target="_blank"
                  rel="noreferrer"
                  className="text-white font-mono underline underline-offset-2"
                >
                  @node_and_nerve_bot
                </a>
              </div>
              <div className="flex items-center justify-between gap-4">
                <span className={muted}>Database engine:</span>
                <span className="text-white font-medium">FalkorDB</span>
              </div>
            </div>
          </div>
        )}

        {view === "signin" && (
          <div className="space-y-4 text-sm text-[#c8c8c8] font-sans-ui leading-relaxed">
            <p>
              Each company gets its own FalkorDB graph. API keys are scoped to companies; a key for one company gets
              403 for every other.
            </p>
            <div className="bg-black/60 rounded-2xl p-4 border border-white/10 font-mono text-xs space-y-2 break-all">
              <div className="text-white/50">// Contract investigation</div>
              <div className="text-white">
                curl -X POST {apiUrl}/v1/companies/kaveri-body-works/contracts/investigations \<br />
                &nbsp;&nbsp;-H &quot;X-API-Key: $NN_API_KEY&quot; -H &quot;Content-Type: application/json&quot; \
                <br />
                &nbsp;&nbsp;-d &apos;{JSON.stringify({ contract: "CT-104", as_of: "2026-10-16" })}&apos;
              </div>
            </div>
            <p className="text-xs text-[#8e8e8e]">
              OpenAPI reference:{" "}
              <a
                href={`${apiUrl}/docs`}
                target="_blank"
                rel="noreferrer"
                className="text-white underline underline-offset-2"
              >
                {apiUrl}/docs
              </a>
            </p>
          </div>
        )}

        {view === "explore" && (
          <div className="space-y-4 text-sm text-[#c8c8c8] font-sans-ui leading-relaxed">
            <p>
              This is the live investigation, run now against FalkorDB. Every figure below comes from relationships
              in the graph; nothing is generated by a language model.
            </p>
            <LiveStatus state={live} />
            {live.status === "ok" && (
              <>
                <LiveContract data={live.data} />
                <div className={`${box} font-mono space-y-1`}>
                  <div className="text-emerald-400">
                    ✓ {live.data.queries.length} graph queries, answered in {live.data.fetched_ms} ms
                  </div>
                  <div className="text-white/70 break-words">{live.data.queries.join(" · ")}</div>
                </div>
              </>
            )}

            <div className="flex flex-wrap justify-end gap-2 pt-2">
              {live.status === "ok" && live.data.trace_url && (
                <a
                  href={live.data.trace_url}
                  target="_blank"
                  rel="noreferrer"
                  className="border border-white/30 text-white font-semibold text-xs px-5 py-2.5 rounded-full hover:bg-white/10 transition-colors"
                >
                  Open evidence graph
                </a>
              )}
              <button
                type="button"
                onClick={onClose}
                className="bg-white text-black font-semibold text-xs px-5 py-2.5 rounded-full hover:bg-white/90 transition-colors"
              >
                Close
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
