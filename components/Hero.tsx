"use client";

import { TrustIndicator } from "./TrustIndicator";
import { PrimaryCTA } from "./PrimaryCTA";

interface HeroProps {
  onExplore?: () => void;
  onTrustClick?: () => void;
}

export function Hero({ onExplore, onTrustClick }: HeroProps) {
  return (
    <section className="hero-wrapper">
      {/* 1. Trust Indicator */}
      <div className="mb-4 sm:mb-5 animate-reveal" style={{ "--stagger-delay": "0.05s" } as React.CSSProperties}>
        <TrustIndicator onClick={onTrustClick} />
      </div>

      {/* 2. Headline - Exactly Two Lines */}
      <h1 className="headline-display">
        <span className="headline-line-1">
          Company Intelligence
        </span>
        <span className="headline-line-2">
          Connected By Design
        </span>
      </h1>

      {/* 3. Supporting Text */}
      <p
        className="hero-supporting-text animate-reveal"
        style={{ "--stagger-delay": "0.42s" } as React.CSSProperties}
      >
        Connect business context, operational records and decisions into a graph that helps AI agents investigate how everything relates.
      </p>

      {/* 4. Primary CTA */}
      <div className="animate-reveal" style={{ "--stagger-delay": "0.55s" } as React.CSSProperties}>
        <PrimaryCTA onClick={onExplore} />
      </div>
    </section>
  );
}
