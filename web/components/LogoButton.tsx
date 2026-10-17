"use client";

import Image from "next/image";

interface LogoButtonProps {
  onClick?: () => void;
  className?: string;
  size?: number;
}

export function LogoButton({ onClick, className = "", size = 42 }: LogoButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label="Node & Nerve Home"
      className={`group relative flex items-center justify-center rounded-full bg-white transition-transform duration-200 ease-out hover:scale-[1.04] active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/80 shadow-[0_4px_14px_rgba(0,0,0,0.16)] cursor-pointer ${className}`}
      style={{ width: `${size}px`, height: `${size}px` }}
    >
      <div className="relative flex items-center justify-center w-full h-full p-[7px]">
        {/* Crisp Node & Nerve Emblem */}
        <svg
          viewBox="0 0 32 32"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          className="w-full h-full text-black transition-transform duration-300 group-hover:rotate-12"
        >
          {/* Central hub and connecting nerve pathways */}
          <circle cx="16" cy="16" r="4.5" fill="currentColor" />
          <circle cx="7" cy="10" r="2.8" fill="currentColor" />
          <circle cx="25" cy="10" r="2.8" fill="currentColor" />
          <circle cx="10" cy="24" r="2.8" fill="currentColor" />
          <circle cx="22" cy="24" r="2.8" fill="currentColor" />

          {/* Nerve connection synapses */}
          <line x1="16" y1="16" x2="7" y2="10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <line x1="16" y1="16" x2="25" y2="10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <line x1="16" y1="16" x2="10" y2="24" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <line x1="16" y1="16" x2="22" y2="24" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          <line x1="7" y1="10" x2="25" y2="10" stroke="currentColor" strokeWidth="0.9" strokeDasharray="1.5 2" opacity="0.6" />
        </svg>
      </div>
    </button>
  );
}
