"""Trace pages: the evidence graph behind a contract investigation, in the browser.

A trace link points at ``/trace/{company}/contracts/{contract}``. The page re-runs the
deterministic investigation against FalkorDB (nothing is cached or stored), draws the
traversed records and links, and lists the figures, decisions and gaps with the book
pages that support them. Items named in an answer can be highlighted with ``focus``.

Links are opened from chat apps, so they cannot carry an API key. Instead they carry an
HMAC signature over company, contract and expiry, made with ``TRACE_LINK_SECRET``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import time
import urllib.parse
from datetime import date
from typing import Any

from company_brain import queries as q
from company_brain.cashflow import inr
from company_brain.contract_cost import ContractCostResponse
from company_brain.graph import GraphStore

LINK_TTL_S = 7 * 24 * 3600
_REF_RE = re.compile(r"\b[A-Z]{1,4}-\d{2,5}[A-Z]?(?:-\d+)?\b")


def _signature(secret: str, company: str, contract: str, expires: int) -> str:
    message = f"{company}|{contract.lower()}|{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:32]


def trace_link(
    base_url: str,
    secret: str,
    company: str,
    contract: str,
    *,
    as_of: date | None = None,
    focus: list[str] | None = None,
    now: float | None = None,
) -> str:
    expires = int((now or time.time()) + LINK_TTL_S)
    params: dict[str, str] = {
        "exp": str(expires),
        "sig": _signature(secret, company, contract, expires),
    }
    if as_of:
        params["as_of"] = as_of.isoformat()
    if focus:
        params["focus"] = ",".join(sorted(set(focus)))
    path = f"/trace/{urllib.parse.quote(company)}/contracts/{urllib.parse.quote(contract)}"
    return f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"


def verify(
    secret: str, company: str, contract: str, expires: int, signature: str, now: float | None = None
) -> bool:
    if not secret or expires < (now or time.time()):
        return False
    return hmac.compare_digest(_signature(secret, company, contract, expires), signature)


def refs_in(text: str) -> list[str]:
    """Business references such as PUR-2058, SP-309 or CT-104 mentioned in a text."""
    return sorted(set(_REF_RE.findall(text)))


# ---------------------------------------------------------------- graph data

_KIND_STYLE = {
    "contract": ("#1d4ed8", "Contract"),
    "job": ("#0891b2", "Job"),
    "purchase": ("#b45309", "Purchase"),
    "wage": ("#7c3aed", "Wage"),
    "machinerycost": ("#be185d", "Machinery"),
    "expense": ("#4d7c0f", "Expense"),
    "supplier": ("#475569", "Supplier"),
    "worker": ("#6d28d9", "Worker"),
    "customer": ("#0f766e", "Customer"),
    "source": ("#64748b", "Book page"),
    "payment": ("#15803d", "Payment"),
}


def trace_graph(store: GraphStore, company: str, r: ContractCostResponse) -> dict[str, Any]:
    """Nodes and edges for the page: the paths the investigation traversed, the book
    pages that support each entry, pending decisions and settling payments."""
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(kind: str, a: str, b: str, **extra: Any) -> None:
        edges.setdefault((kind, a, b), {"type": kind, "source": a, "target": b, **extra})

    c = r.contract
    if c.customer_id:
        add("PLACED_BY", c.id, c.customer_id)
    for path in r.evidence_paths:
        for e in path.edges:
            add(e.type, e.from_id, e.to_id, asserted_by=e.source_id)
    for line in r.cost_lines:
        for sid in line.source_ids:
            add("SUPPORTED_BY", line.record_id, sid)
        for other in line.shared_with:
            add("SHARED_WITH", line.record_id, other, dashed=True)
    for item in r.pending_allocations:
        for target in item.noted_for:
            add("NOTED_FOR", item.record_id, target, dashed=True)
        for sid in item.source_ids:
            add("SUPPORTED_BY", item.record_id, sid)
    for receipt in r.customer_account.receipts:
        add("ALLOCATED_TO", receipt["id"], c.id)
    for sid in c.source_ids:
        add("SUPPORTED_BY", c.id, sid)

    ids = sorted({e["source"] for e in edges.values()} | {e["target"] for e in edges.values()})
    labels = {row["id"]: row for row in store.run(company, q.MF_LABELS, {"ids": ids})}
    flagged: dict[str, str] = {}
    for f in r.findings:
        if f.kind in {"decision", "unknown", "inconsistency"}:
            for eid in f.entity_ids:
                flagged.setdefault(eid, f.kind)
    nodes = []
    for eid in ids:
        row = labels.get(eid, {"kind": eid.split(":", 1)[0], "ref": eid, "name": None})
        kind = str(row.get("kind") or "")
        color, kind_label = _KIND_STYLE.get(kind, ("#64748b", kind))
        ref = str(row.get("ref") or eid)
        nodes.append(
            {
                "id": eid,
                "ref": ref,
                "label": ref,
                "kind": kind,
                "kind_label": kind_label,
                "name": row.get("name"),
                "status": row.get("status"),
                "amount": inr(row["amount_paise"]) if row.get("amount_paise") is not None else None,
                "color": color,
                **({"flag": flagged[eid]} if eid in flagged and kind != "contract" else {}),
            }
        )
    return {"nodes": nodes, "edges": list(edges.values())}


# ---------------------------------------------------------------- page


def _source_label(r: ContractCostResponse) -> dict[str, str]:
    return {s.id: str(s.source_record_id or s.id) for s in r.sources}


def render_trace_page(
    store: GraphStore, company: str, r: ContractCostResponse, focus: list[str]
) -> str:
    graph = trace_graph(store, company, r)
    src = _source_label(r)
    p, b, c = r.profitability, r.cost_breakdown, r.contract

    def cites(ids: list[str]) -> str:
        return " ".join(f'<span class="cite">{html.escape(src.get(i, i))}</span>' for i in ids)

    def finding_list(kinds: set[str]) -> str:
        items = [f for f in r.findings if f.kind in kinds]
        if not items:
            return '<p class="muted">None.</p>'
        return (
            "<ul>"
            + "".join(f"<li>{html.escape(f.statement)} {cites(f.source_ids)}</li>" for f in items)
            + "</ul>"
        )

    figures = [
        ("Contract value", inr(p.contract_value_paise)),
        ("Materials", inr(b.materials_paise)),
        ("Labour", inr(b.labour_paise)),
        ("Machinery", inr(b.machinery_paise)),
        ("Other direct", inr(b.other_paise)),
        ("Recorded direct cost", inr(b.total_paise)),
        (
            "Provisional profit",
            f"{inr(p.provisional_profit_paise)} ({p.provisional_margin_percent}%)",
        ),
        ("Customer paid", inr(r.customer_account.received_paise)),
        ("Still to receive", inr(r.customer_account.outstanding_paise)),
    ]
    rows = "".join(
        f'<tr><td>{html.escape(k)}</td><td class="num">{html.escape(v)}</td></tr>'
        for k, v in figures
    )
    focus_ids = [n["id"] for n in graph["nodes"] if n["ref"] in set(focus)]
    data = json.dumps({"graph": graph, "focus": focus_ids}).replace("</", "<\\/")
    title = f"{c.ref} trace"
    legend = "".join(
        f'<span class="key"><i style="background:{color}"></i>{html.escape(label)}</span>'
        for color, label in _KIND_STYLE.values()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#f8fafc; --panel:#ffffff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
  --decision:#d97706; --unknown:#2563eb; --inconsistency:#dc2626; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b1220; --panel:#111a2e; --ink:#e2e8f0;
  --muted:#94a3b8; --line:#1e293b; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:var(--bg); color:var(--ink); }}
header {{ padding:16px 20px; border-bottom:1px solid var(--line); background:var(--panel); }}
h1 {{ margin:0; font-size:18px; }} h2 {{ font-size:14px; margin:18px 0 6px; }}
.muted {{ color:var(--muted); }}
main {{ display:grid; grid-template-columns:minmax(0,1fr) 380px; height:calc(100vh - 74px); }}
#graph {{ min-height:420px; }}
aside {{ overflow:auto; padding:4px 18px 24px; border-left:1px solid var(--line);
  background:var(--panel); }}
table {{ width:100%; border-collapse:collapse; }}
td {{ padding:3px 0; border-bottom:1px solid var(--line); }} .num {{ text-align:right;
  font-variant-numeric:tabular-nums; }}
ul {{ padding-left:18px; margin:4px 0; }} li {{ margin:4px 0; }}
.cite {{ display:inline-block; font-size:11px; padding:0 5px; border:1px solid var(--line);
  border-radius:4px; color:var(--muted); white-space:nowrap; }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 12px; font-size:12px; color:var(--muted);
  margin-top:6px; }}
.key i {{ display:inline-block; width:10px; height:10px; border-radius:50%;
  margin-right:4px; vertical-align:-1px; }}
#detail {{ font-size:13px; min-height:40px; }}
.badge {{ font-size:11px; font-weight:600; text-transform:uppercase; }}
@media (max-width: 860px) {{ main {{ grid-template-columns:1fr; height:auto; }}
  #graph {{ height:60vh; }} aside {{ border-left:0; border-top:1px solid var(--line); }} }}
</style></head>
<body>
<header>
  <h1>{html.escape(c.ref)} &middot; {html.escape(c.customer_name or "")}
    <span class="muted">&middot; trace as of {r.as_of.isoformat()}</span></h1>
  <div class="muted">Live from the company graph: every node and link below was traversed by
    the investigation. Highlighted: items in the answer. Rings: amber decision, blue unknown,
    red inconsistency. Dashed: shared or not yet split.</div>
  <div class="legend">{legend}</div>
</header>
<main>
  <div id="graph" role="img" aria-label="Evidence graph for {html.escape(c.ref)}"></div>
  <aside>
    <h2>Selected</h2><div id="detail" class="muted">Tap a node.</div>
    <h2>Figures <span class="muted">(provisional, recorded direct costs only)</span></h2>
    <table>{rows}</table>
    <h2>Decisions needed</h2>{finding_list({"decision"})}
    <h2>Unknown or conflicting</h2>{finding_list({"unknown", "inconsistency"})}
    <h2>Warnings</h2><ul>{"".join(f"<li>{html.escape(w)}</li>" for w in r.warnings)}</ul>
    <p class="muted">Graph queries: {html.escape(", ".join(r.queries_executed))}</p>
  </aside>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"></script>
<script>
const DATA = {data};
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const focus = new Set(DATA.focus);
const flagColor = {{decision: css('--decision'), unknown: css('--unknown'),
  inconsistency: css('--inconsistency')}};
const cy = cytoscape({{
  container: document.getElementById('graph'),
  elements: [
    ...DATA.graph.nodes.map(n => ({{ data: n }})),
    ...DATA.graph.edges.map((e, i) => ({{ data: {{ id: 'e' + i, ...e }} }})),
  ],
  style: [
    {{ selector: 'node', style: {{ 'background-color': 'data(color)', label: 'data(label)',
      color: css('--ink'), 'font-size': 9, 'text-valign': 'bottom', 'text-margin-y': 3,
      width: 18, height: 18 }} }},
    {{ selector: 'node[kind = "contract"]', style: {{ width: 34, height: 34, 'font-size': 12,
      'font-weight': 'bold' }} }},
    {{ selector: 'node[kind = "source"]', style: {{ shape: 'round-rectangle', width: 22,
      height: 14 }} }},
    {{ selector: 'node[?flag]', style: {{ 'border-width': 4,
      'border-color': (n) => flagColor[n.data('flag')] }} }},
    {{ selector: 'node.focus', style: {{ width: 30, height: 30, 'font-size': 12,
      'font-weight': 'bold', 'overlay-color': '#f59e0b', 'overlay-opacity': 0.18,
      'overlay-padding': 6 }} }},
    {{ selector: 'edge', style: {{ width: 1.2, 'line-color': css('--line'),
      'target-arrow-color': css('--muted'), 'target-arrow-shape': 'triangle',
      'arrow-scale': 0.7, 'curve-style': 'bezier', 'font-size': 7,
      color: css('--muted'), 'text-rotation': 'autorotate' }} }},
    {{ selector: 'edge.show', style: {{ label: 'data(type)', width: 2,
      'line-color': css('--muted') }} }},
    {{ selector: 'edge[?dashed]', style: {{ 'line-style': 'dashed', 'line-color': '#d97706' }} }},
    {{ selector: '.dim', style: {{ opacity: 0.15 }} }},
  ],
  layout: {{ name: 'cose', animate: false, nodeRepulsion: 9000, idealEdgeLength: 70,
    padding: 20 }},
}});
cy.nodes().filter(n => focus.has(n.id())).addClass('focus');
if (focus.size) {{
  cy.fit(cy.nodes('.focus').closedNeighborhood(), 60);
}}
if (cy.zoom() > 1.1) {{ cy.zoom(1.1); cy.center(focus.size ? cy.nodes('.focus') : undefined); }}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;',
  '>':'&gt;','"':'&quot;'}}[c]));
cy.on('tap', 'node', (ev) => {{
  const n = ev.target.data();
  const links = ev.target.connectedEdges().map(e => {{
    const d = e.data(); const other = d.source === n.id ? d.target : d.source;
    const o = cy.getElementById(other).data();
    return `<li>${{esc(d.type)}} ${{d.source === n.id ? '&rarr;' : '&larr;'}}
      ${{esc(o.ref)}}${{d.asserted_by ? ' <span class="cite">per ' +
      esc(cy.getElementById(d.asserted_by).data('ref') || d.asserted_by) + '</span>' : ''}}</li>`;
  }}).join('');
  document.getElementById('detail').innerHTML =
    `<b>${{esc(n.ref)}}</b> <span class="muted">${{esc(n.kind_label)}}</span>` +
    (n.flag ? ` <span class="badge" style="color:${{flagColor[n.flag]}}">` +
      `${{esc(n.flag)}}</span>` : '') +
    `<div>${{esc(n.name)}}</div>` +
    (n.amount ? `<div>Amount ${{esc(n.amount)}}</div>` : '') +
    (n.status ? `<div class="muted">Status: ${{esc(n.status)}}</div>` : '') +
    `<ul>${{links}}</ul>`;
  cy.elements().removeClass('dim show');
  cy.elements().not(ev.target.closedNeighborhood()).addClass('dim');
  ev.target.connectedEdges().addClass('show');
}});
cy.on('tap', (ev) => {{ if (ev.target === cy) cy.elements().removeClass('dim show'); }});
</script>
</body></html>"""
