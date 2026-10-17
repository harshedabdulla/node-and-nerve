"use client";

interface TrustIndicatorProps {
  label?: string;
  className?: string;
  onClick?: () => void;
}

export function TrustIndicator({
  label = "Graph-powered company intelligence",
  className = "",
  onClick,
}: TrustIndicatorProps) {
  return (
    <div
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      className={`inline-flex items-center justify-center select-none cursor-default transition-transform duration-200 ${
        onClick ? "cursor-pointer hover:scale-[1.02]" : ""
      } ${className}`}
    >
      {/* Overlapping Avatars */}
      <div className="flex items-center -space-x-2 z-10 shrink-0">
        {/* Avatar 1: Graph Nodes */}
        <div
          className="relative flex items-center justify-center w-[30px] h-[30px] sm:w-[32px] sm:h-[32px] rounded-full bg-[#28282a] border-[1.5px] border-white/40 p-[2px] shadow-[0_2px_8px_rgba(0,0,0,0.4)]"
          title="FalkorDB Graph Nodes"
        >
          <div className="w-full h-full rounded-full bg-white flex items-center justify-center">
            <svg
              viewBox="0 0 16 16"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              className="w-3.5 h-3.5 text-[#28282a]"
            >
              <circle cx="4" cy="5" r="2" fill="currentColor" />
              <circle cx="12" cy="5" r="2" fill="currentColor" />
              <circle cx="8" cy="12" r="2" fill="currentColor" />
              <line x1="4" y1="5" x2="8" y2="12" stroke="currentColor" strokeWidth="1.2" />
              <line x1="12" y1="5" x2="8" y2="12" stroke="currentColor" strokeWidth="1.2" />
            </svg>
          </div>
        </div>

        {/* Avatar 2: Directed Synapse / Cypher Traversal */}
        <div
          className="relative flex items-center justify-center w-[30px] h-[30px] sm:w-[32px] sm:h-[32px] rounded-full bg-[#28282a] border-[1.5px] border-white/40 p-[2px] shadow-[0_2px_8px_rgba(0,0,0,0.4)]"
          title="Deterministic Cypher Traversal"
        >
          <div className="w-full h-full rounded-full bg-white flex items-center justify-center">
            <svg
              viewBox="0 0 16 16"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              className="w-3.5 h-3.5 text-[#28282a]"
            >
              <path
                d="M3 8h7m0 0l-3-3m3 3l-3 3"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <circle cx="13" cy="8" r="1.5" fill="currentColor" />
            </svg>
          </div>
        </div>

        {/* Avatar 3: Source Evidence / Provenance */}
        <div
          className="relative flex items-center justify-center w-[30px] h-[30px] sm:w-[32px] sm:h-[32px] rounded-full bg-[#28282a] border-[1.5px] border-white/40 p-[2px] shadow-[0_2px_8px_rgba(0,0,0,0.4)]"
          title="100% Provenance & Sources"
        >
          <div className="w-full h-full rounded-full bg-white flex items-center justify-center">
            <svg
              viewBox="0 0 16 16"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              className="w-3.5 h-3.5 text-[#28282a]"
            >
              <rect x="4" y="3" width="8" height="10" rx="1.5" stroke="currentColor" strokeWidth="1.2" />
              <line x1="6.5" y1="6" x2="9.5" y2="6" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
              <line x1="6.5" y1="8.5" x2="9.5" y2="8.5" stroke="currentColor" strokeWidth="1.1" strokeLinecap="round" />
            </svg>
          </div>
        </div>
      </div>

      {/* Trust Pill overlapping the final avatar */}
      <div className="-ml-2.5 pl-4 pr-3.5 h-[28px] sm:h-[30px] rounded-full bg-[#28282a] border border-white/35 shadow-[0_2px_10px_rgba(0,0,0,0.3)] z-0 flex items-center justify-center">
        <span className="text-[11.5px] sm:text-[12.5px] font-medium text-[#c4c2c3] tracking-tight whitespace-nowrap leading-none pt-[0.5px]">
          {label}
        </span>
      </div>
    </div>
  );
}
