"use client";

import { useEffect, useState } from "react";

/** Marks the header once the page has scrolled, so the nav can take Spindle's indigo pill. */
export function ScrollState({ children }: { children: React.ReactNode }) {
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const update = () => setScrolled(window.scrollY > 40);
    update();
    window.addEventListener("scroll", update, { passive: true });
    return () => window.removeEventListener("scroll", update);
  }, []);
  return <header className={`site-header${scrolled ? " is-scrolled" : ""}`}>{children}</header>;
}
