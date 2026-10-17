"use client";

interface MetricItem {
  symbol: string;
  value: string;
  label: string;
}

const METRICS: MetricItem[] = [
  {
    symbol: "⬡",
    value: "FalkorDB",
    label: "Graph per company",
  },
  {
    symbol: "⚡",
    value: "Deterministic",
    label: "Bounded Cypher paths",
  },
  {
    symbol: "◈",
    value: "Provenance",
    label: "First-class source records",
  },
  {
    symbol: "◎",
    value: "Zero Guesses",
    label: "Rendered factual truth",
  },
];

interface StatisticsFooterProps {
  onItemClick?: (metric: MetricItem) => void;
}

export function StatisticsFooter({ onItemClick }: StatisticsFooterProps) {
  return (
    <footer className="footer-wrapper">
      <div className="footer-grid">
        {METRICS.map((metric, idx) => (
          <div
            key={metric.value}
            onClick={() => onItemClick?.(metric)}
            className={`metric-item animate-reveal ${
              onItemClick ? "cursor-pointer hover:opacity-90 transition-opacity" : ""
            }`}
            style={{
              "--stagger-delay": `${0.65 + idx * 0.08}s`,
            } as React.CSSProperties}
          >
            {/* Small display-font symbol & main value */}
            <div className="metric-value-row">
              <span className="metric-symbol">
                {metric.symbol}
              </span>
              <span className="metric-value">
                {metric.value}
              </span>
            </div>

            {/* Muted label aligned with value text */}
            <span className="metric-label pl-[22px]">
              {metric.label}
            </span>
          </div>
        ))}
      </div>
    </footer>
  );
}
