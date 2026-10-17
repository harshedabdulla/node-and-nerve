"use client";

interface PrimaryCTAProps {
  label?: string;
  onClick?: () => void;
  className?: string;
}

export function PrimaryCTA({
  label = "Explore Node & Nerve",
  onClick,
  className = "",
}: PrimaryCTAProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`cta-button focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 ${className}`}
    >
      <span>{label}</span>
      {/* Subtle right chevron micro-arrow */}
      <svg
        viewBox="0 0 16 16"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        className="w-3.5 h-3.5 transition-transform duration-200 group-hover:translate-x-0.5"
      >
        <path
          d="M6 3.5L10.5 8L6 12.5"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  );
}
